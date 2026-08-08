"""RTL generation agent + inner repair loop (F4.3).

For one module in a :class:`DesignPlan`, the agent:

1. Asks the :class:`ModelRouter` for RTL with ``TaskType.RTL_GEN``.
2. Stages a new :class:`RTLArtifact` version into the store (the AC: every
   attempt is a new artifact version).
3. Runs :class:`Linter` (verible-class) and, if lint passes, :class:`Elaborator`
   (verilator-class). Both checks are deterministic and run model-free.
4. If either gate fails, asks the router with ``TaskType.RTL_REPAIR`` —
   which the F3.2 policy routes to the LOCAL inner-loop model — feeding the
   previous source and the failing violations into the prompt. Loops until
   both gates pass or the budget is exhausted.

The agent never decides ``advance/escalate``; it produces an
:class:`RTLGenerationOutcome` and the control graph (later) chooses the
transition. ``passed=False`` with ``attempts == max_attempts`` is the
escalation signal: ``decide_transition`` will read ``last_failure`` and
walk INNER -> OUTER on the next pass.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from chip_agent.design_state import (
    Artifact,
    ArtifactRef,
    DesignPlan,
    LintResult,
    ModelInvocation,
    ModelRouter,
    ModuleDecl,
    Port,
    Provenance,
    RTLArtifact,
    Spec,
    Stage,
    TaskType,
    Violation,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = [
    "Elaborator",
    "Linter",
    "RTLGenerationAgent",
    "RTLGenerationError",
    "RTLGenerationOutcome",
]


_GEN_SYSTEM_PROMPT = """\
You are a synthesisable Verilog/SystemVerilog generator. Given a module
specification (name, ports, and a brief description), produce ONE module's
RTL source. Output ONLY the RTL — no preamble, no prose, no markdown fences.

Constraints:
* Honour the port list exactly (name, direction, width).
* No latches: every always_ff must register a value on every path, every
  always_comb must drive every output on every path.
* Use synchronous reset when a reset is requested unless asynchronous is
  explicitly required.
* No `initial` blocks for synthesisable logic; testbench constructs are not
  in scope.
* Keep the module self-contained — no $display, no $finish, no `include`.
""".strip()


_REPAIR_SYSTEM_PROMPT = """\
You are a Verilog/SystemVerilog repair tool. The previous RTL failed the
inner-loop checks (lint / elaborate). Produce a corrected version of the
module. Do not change the port list. Output ONLY the RTL.

Read the failing violations carefully and target the root cause.
""".strip()


# Strip ```verilog ... ``` or bare ``` fences around the RTL.
_FENCE_RE = re.compile(
    r"^\s*```(?:verilog|systemverilog|sv|v)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Search-anywhere variant for prose-then-fence shapes the model emits
# in its inner-loop generation when it ignores the system-prompt
# instruction to output Verilog only.
_FENCE_SEARCH_RE = re.compile(
    r"```(?:verilog|systemverilog|sv|v)?\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

# Last-resort extractor: prose + bare Verilog with no fences. Start
# anchor must be start-of-line so the word "module" in the prose
# doesn't false-match; ``endmodule`` end anchor accepts mid-line too so
# single-line module bodies (common in test fixtures) work. The token
# "endmodule" is vanishingly rare outside Verilog source, so the looser
# end anchor is safe.
_MODULE_RE = re.compile(
    r"(?m)^\s*module\s+\w+\b.*?\bendmodule\b",
    re.DOTALL,
)


class Linter(Protocol):
    """The verible-like seam: an :class:`RTLArtifact` in, a :class:`LintResult` out."""

    def lint(self, rtl: RTLArtifact) -> LintResult: ...


class Elaborator(Protocol):
    """The verilator-like seam, same shape — separate method name for clarity."""

    def elaborate(self, rtl: RTLArtifact) -> LintResult: ...


class RTLGenerationError(ValueError):
    """The router produced no usable text or the plan was malformed."""


@dataclass(frozen=True)
class RTLGenerationOutcome:
    """Result of one ``generate_module`` call.

    ``passed`` is True iff both lint and elaborate gates closed. ``attempts``
    counts every router call (initial + repairs). ``versions`` lists the
    artifact version numbers produced (one per attempt). ``last_failure``
    points at the verification artifact that blocked progress when
    ``passed`` is False.
    """

    passed: bool
    rtl: RTLArtifact
    rtl_ref: ArtifactRef
    lint: LintResult
    elaborate: LintResult | None
    attempts: int
    versions: list[int]
    last_failure: ArtifactRef | None


class RTLGenerationAgent:
    """RTL generation + bounded inner repair loop for one module at a time."""

    GEN_SYSTEM_PROMPT = _GEN_SYSTEM_PROMPT
    REPAIR_SYSTEM_PROMPT = _REPAIR_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        store: SqliteArtifactStore,
        linter: Linter,
        elaborator: Elaborator,
        design_id: str,
        max_attempts: int = 3,
        language: str = "verilog",
        agent_name: str = "rtl_specialist",
    ) -> None:
        if not design_id:
            raise ValueError("design_id must be non-empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.router = router
        self.store = store
        self.linter = linter
        self.elaborator = elaborator
        self.design_id = design_id
        self.max_attempts = max_attempts
        self.language = language
        self.agent_name = agent_name

    def generate_module(
        self,
        plan: DesignPlan,
        module_id: str,
        *,
        spec: Spec | None = None,
    ) -> RTLGenerationOutcome:
        """Drive the inner loop for one module; return when it passes or budget is gone."""
        if plan.design_id != self.design_id:
            raise RTLGenerationError(
                f"plan.design_id {plan.design_id!r} != agent.design_id {self.design_id!r}"
            )
        module = _find_module(plan, module_id)
        # F20.2: parent + siblings for cross-module context.
        # ``_gen_prompt`` is called every attempt that's a fresh
        # generation (not a repair); compute the context once outside
        # the loop so the prompt is stable across retries.
        parent = _find_parent(plan, module_id)
        siblings = _sibling_modules(plan, module_id)
        plan_ref = plan.ref() if plan.content_hash else _hashed(plan).ref()
        spec_ref = (spec.ref() if spec and spec.content_hash else
                    (_hashed(spec).ref() if spec else None))

        versions: list[int] = []
        previous_source: str | None = None
        previous_violations: list[Violation] = []
        last_rtl_ref: ArtifactRef | None = None
        attempts = 0

        while attempts < self.max_attempts:
            attempts += 1
            is_repair = previous_source is not None
            task = TaskType.RTL_REPAIR if is_repair else TaskType.RTL_GEN
            user_prompt = (
                _repair_prompt(module, previous_source or "", previous_violations)
                if is_repair
                else _gen_prompt(
                    module, spec, parent=parent, siblings=siblings,
                )
            )
            system_prompt = (
                self.REPAIR_SYSTEM_PROMPT if is_repair else self.GEN_SYSTEM_PROMPT
            )

            result = self.router.generate(
                task,
                context={"prompt": user_prompt, "system": system_prompt},
            )

            # F20.5: rank multi-candidate router output by lint +
            # elaborate + LOC. Single-candidate paths (n=1, the demo
            # default) take the existing route and produce a byte-
            # identical RTLArtifact.
            ranked_scores: list[dict[str, Any]] | None = None
            if len(result.candidates) > 1:
                source, ranked_scores = _rank_candidates(
                    result.candidates,
                    module=module, store=self.store,
                    linter=self.linter, elaborator=self.elaborator,
                    language=self.language, design_id=self.design_id,
                    agent_name=self.agent_name,
                )
                source = source.rstrip("\n")
            else:
                source = _strip_fences(result.chosen)
            if not source:
                raise RTLGenerationError(
                    f"router returned empty RTL on attempt {attempts}"
                )

            rtl, rtl_ref = self._persist(
                module=module,
                source=source,
                attempt=attempts,
                invocation=result.invocation,
                plan_ref=plan_ref,
                spec_ref=spec_ref,
                previous_rtl_ref=last_rtl_ref,
                candidate_scores=ranked_scores,
            )
            versions.append(rtl_ref.version)
            last_rtl_ref = rtl_ref

            lint = self.linter.lint(rtl)
            self.store.put(lint)
            if not lint.gate_ok:
                previous_source = source
                previous_violations = list(lint.violations)
                continue

            elaborate = self.elaborator.elaborate(rtl)
            self.store.put(elaborate)
            if not elaborate.gate_ok:
                previous_source = source
                previous_violations = list(elaborate.violations)
                # last_failure tracks the elaborate result on this path.
                continue

            # Both gates closed — the inner loop converged.
            return RTLGenerationOutcome(
                passed=True,
                rtl=rtl,
                rtl_ref=rtl_ref,
                lint=lint,
                elaborate=elaborate,
                attempts=attempts,
                versions=versions,
                last_failure=None,
            )

        # Budget exhausted. Replay the final attempt's checks for the outcome.
        assert last_rtl_ref is not None  # we ran at least one attempt
        final_rtl = self.store.get(last_rtl_ref)
        assert isinstance(final_rtl, RTLArtifact)
        final_lint = self.linter.lint(final_rtl)
        self.store.put(final_lint)
        final_elab: LintResult | None
        if final_lint.gate_ok:
            final_elab = self.elaborator.elaborate(final_rtl)
            self.store.put(final_elab)
            last_failure_ref = final_elab.ref() if not final_elab.gate_ok else None
        else:
            final_elab = None
            last_failure_ref = final_lint.ref()
        return RTLGenerationOutcome(
            passed=False,
            rtl=final_rtl,
            rtl_ref=last_rtl_ref,
            lint=final_lint,
            elaborate=final_elab,
            attempts=attempts,
            versions=versions,
            last_failure=last_failure_ref,
        )

    # ----------------------------------------------------------------- internals
    def _persist(
        self,
        *,
        module: ModuleDecl,
        source: str,
        attempt: int,
        invocation: ModelInvocation,
        plan_ref: ArtifactRef,
        spec_ref: ArtifactRef | None,
        previous_rtl_ref: ArtifactRef | None,
        candidate_scores: list[dict[str, Any]] | None = None,
    ) -> tuple[RTLArtifact, ArtifactRef]:
        # Ensure exactly one trailing newline. Verible's "posix-eof" rule
        # treats a missing newline as a style violation, which trips its
        # non-zero exit code even when nothing else is wrong.
        normalized = source.rstrip("\n") + "\n"
        blob = self.store.put_blob(normalized.encode("utf-8"), media_type="text/x-verilog")
        inputs: list[ArtifactRef] = [plan_ref]
        if spec_ref is not None:
            inputs.append(spec_ref)
        if previous_rtl_ref is not None:
            inputs.append(previous_rtl_ref)
        # F20.5: persist the per-candidate ranking on the winner's
        # provenance. ``Provenance.config`` is in ``_NON_CONTENT_FIELDS``
        # transitively (the parent ``Artifact._NON_CONTENT_FIELDS``
        # excludes the whole ``provenance`` field) so the winner RTL's
        # content hash is unchanged for byte-identical source — demo
        # goldens stay green.
        provenance_config: dict[str, Any] = {}
        if candidate_scores is not None:
            provenance_config["candidate_scores"] = candidate_scores
        rtl = RTLArtifact(
            artifact_id=f"{self.design_id}.{module.module_id}.rtl",
            design_id=self.design_id,
            module_id=module.module_id,
            top_module=module.name,
            language=self.language,
            synthesizable=True,
            source=blob,
            submodule_ids=list(module.depends_on),
            provenance=Provenance(
                produced_by=Stage.RTL,
                agent=self.agent_name,
                model=invocation,
                inputs=inputs,
                notes=f"attempt {attempt}",
                config=provenance_config,
            ),
        )
        ref = self.store.put(rtl)
        # The store assigns the version on put; reload so the returned object
        # carries the canonical ref/version.
        loaded = self.store.get(ref)
        assert isinstance(loaded, RTLArtifact)
        return loaded, ref


def _find_module(plan: DesignPlan, module_id: str) -> ModuleDecl:
    for m in plan.modules:
        if m.module_id == module_id:
            return m
    known = [m.module_id for m in plan.modules]
    raise RTLGenerationError(
        f"module_id {module_id!r} not in plan; known modules: {known}"
    )


def _find_parent(
    plan: DesignPlan, module_id: str,
) -> ModuleDecl | None:
    """F20.2: the (single) parent of ``module_id``.

    A parent is any module whose ``depends_on`` lists ``module_id``
    as a submodule (per the ``ModuleDecl.depends_on # submodule_ids``
    convention that ``_module_order`` uses for topological sort).
    Returns the FIRST match; tree-shaped plans have at most one
    parent per module, but a shared helper module could legally have
    multiple — same first-wins convention the topo sort applies.
    """
    for candidate in plan.modules:
        if module_id in candidate.depends_on:
            return candidate
    return None


def _sibling_modules(
    plan: DesignPlan, module_id: str,
) -> list[ModuleDecl]:
    """F20.2: the parent's other children, in declaration order.

    Empty list when the module has no parent (it's the top), or when
    the parent has no other children. ``_find_parent``'s first-wins
    tie-break applies (multi-parent siblings reflect the FIRST
    parent's other children).
    """
    parent = _find_parent(plan, module_id)
    if parent is None:
        return []
    sibling_ids = [sid for sid in parent.depends_on if sid != module_id]
    by_id = {m.module_id: m for m in plan.modules}
    return [by_id[sid] for sid in sibling_ids if sid in by_id]


def _render_inferred_instantiation(module: ModuleDecl) -> str:
    """F20.2: templated Verilog instantiation line the parent is
    likely to emit. Helps the model see how port names will be wired
    up so it can align its own port names with the parent's conventions
    (e.g. clk → clk, rst_n → rst_n, data buses kept by name).
    """
    if not module.ports:
        return f"{module.name} u_{module.module_id} ();"
    port_lines = [f"    .{p.name}({p.name})" for p in module.ports]
    return (
        f"{module.name} u_{module.module_id} (\n"
        + ",\n".join(port_lines)
        + "\n);"
    )


def _hashed(art: Artifact) -> Artifact:
    """Fill in a content_hash for an artifact that hasn't been put yet so we can `.ref()` it."""
    art.content_hash = art.compute_content_hash()
    return art


def _strip_fences(text: str) -> str:
    """Extract Verilog from a model response — mirrors ``rtl_stage._strip_fences``.

    Three layers most-specific first: strict full-string fence, then a
    search-anywhere fence (handles prose-then-fence), then a bare
    ``module ... endmodule`` span (handles no-fence prose-and-code).
    """
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group("body").strip()
    m = _FENCE_SEARCH_RE.search(stripped)
    if m:
        return m.group("body").strip()
    m = _MODULE_RE.search(stripped)
    if m:
        return m.group(0).strip()
    return stripped


def _rank_candidates(
    candidates: Sequence[str],
    *,
    module: ModuleDecl,
    store: SqliteArtifactStore,
    linter: Linter,
    elaborator: Elaborator,
    language: str,
    design_id: str,
    agent_name: str,
) -> tuple[str, list[dict[str, Any]]]:
    """F20.5: rank candidates by lint + elaborate + LOC.

    Each candidate is staged as a transient :class:`RTLArtifact`
    (NOT persisted to the artifact table — only the winner is). The
    source blob IS put via ``store.put_blob`` because the linter +
    elaborator dereference it; blob storage is content-addressed so
    identical bytes dedupe naturally.

    Returns ``(winning_source, scores)`` where ``scores`` is a list
    of dicts ordered rank-ascending. Rank 1 is the chosen winner.

    Survivors (both gates pass) get ranks ``1..K`` ordered by LOC
    ascending; non-survivors get ranks ``K+1..N`` in their original
    candidate-list order so the audit trail captures every
    alternative without losing the dropped ones.

    Raises :class:`RTLGenerationError` when no candidate passes both
    inner gates — the caller's retry budget handles escalation.
    """
    # Each row: (original_idx, lines, source, lint_ok, elab_ok).
    records: list[tuple[int, int, str, bool, bool]] = []
    for idx, raw in enumerate(candidates):
        source = _strip_fences(raw).rstrip("\n") + "\n"
        if not source.strip():
            records.append((idx, 0, source, False, False))
            continue
        blob = store.put_blob(
            source.encode("utf-8"), media_type="text/x-verilog",
        )
        rtl = RTLArtifact(
            artifact_id=f"{design_id}.{module.module_id}.rtl",
            design_id=design_id,
            module_id=module.module_id,
            top_module=module.name,
            language=language,
            synthesizable=True,
            source=blob,
            submodule_ids=list(module.depends_on),
            provenance=Provenance(
                produced_by=Stage.RTL,
                agent=agent_name,
            ),
        )
        lint = linter.lint(rtl)
        lint_ok = bool(lint.gate_ok)
        elab_ok = False
        if lint_ok:
            elab = elaborator.elaborate(rtl)
            elab_ok = bool(elab.gate_ok)
        lines = len(source.strip().splitlines())
        records.append((idx, lines, source, lint_ok, elab_ok))

    survivors = [r for r in records if r[3] and r[4]]
    if not survivors:
        raise RTLGenerationError(
            f"all {len(candidates)} candidates failed lint or elaborate; "
            f"escalating to retry budget",
        )

    # Stable sort: ties resolved by original index (Python sort is stable).
    survivors_sorted = sorted(survivors, key=lambda r: (r[1], r[0]))
    losers = [r for r in records if not (r[3] and r[4])]

    scores: list[dict[str, Any]] = []
    rank = 0
    for row in survivors_sorted:
        rank += 1
        scores.append({
            "rank": rank, "lines": row[1],
            "lint_ok": row[3], "elab_ok": row[4],
        })
    for row in losers:
        rank += 1
        scores.append({
            "rank": rank, "lines": row[1],
            "lint_ok": row[3], "elab_ok": row[4],
        })

    winner_source = survivors_sorted[0][2]
    return winner_source, scores


def _gen_prompt(
    module: ModuleDecl,
    spec: Spec | None,
    *,
    parent: ModuleDecl | None = None,
    siblings: Sequence[ModuleDecl] = (),
) -> str:
    parts = [
        f"Module: {module.name} (id={module.module_id})",
        f"Description: {module.description}",
        "",
        "Ports:",
    ]
    parts.extend(_format_port(p) for p in module.ports)
    if module.params:
        parts += ["", "Parameters:"]
        parts.extend(f"  - {k} = {v}" for k, v in module.params.items())
    if module.depends_on:
        parts += ["", f"Submodules to instantiate: {', '.join(module.depends_on)}"]

    # F20.2 — cross-module context. Empty defaults keep the prompt
    # byte-identical to pre-F20.2 callers (top modules + single-module
    # plans) so demo goldens stay unchanged.
    if parent is not None:
        parts += [
            "",
            f"Parent module instantiation (parent: {parent.name}):",
            "```verilog",
            _render_inferred_instantiation(module),
            "```",
        ]
    if siblings:
        parts += ["", "Sibling modules (ports you may share signal names with):"]
        for sib in siblings:
            parts.append(f"  - {sib.name} (id={sib.module_id}):")
            for p in sib.ports:
                parts.append(f"    {_format_port(p).lstrip()}")

    if spec is not None:
        parts += ["", "Source spec (for context):", spec.normalized.strip()]
    parts += ["", "Produce the RTL module now."]
    return "\n".join(parts)


def _repair_prompt(
    module: ModuleDecl,
    previous_source: str,
    violations: list[Violation],
) -> str:
    parts = [
        f"Module: {module.name} (id={module.module_id})",
        "",
        "Previous RTL (failing):",
        previous_source.rstrip(),
        "",
        "Failing checks:",
    ]
    if not violations:
        parts.append("  - <no structured violations were captured>")
    else:
        for v in violations:
            loc = f" @ {v.location}" if v.location else ""
            parts.append(f"  - [{v.severity}] {v.code}{loc}: {v.message}")
    parts += [
        "",
        "Repair the module without changing the port list. Output ONLY the RTL.",
    ]
    return "\n".join(parts)


def _format_port(p: Port) -> str:
    desc = f" -- {p.description}" if p.description else ""
    return f"  - {p.name}: {p.direction}, {p.width} bit{'s' if p.width > 1 else ''}{desc}"


