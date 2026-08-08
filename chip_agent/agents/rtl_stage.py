"""RTL stage driver — composes inner + outer loops for one module (F4.4).

The driver wraps :class:`RTLGenerationAgent` (F4.3, inner loop: lint + elaborate)
and adds the outer semantic loop:

1. Run the inner loop until it converges or its budget is exhausted.
2. If the inner loop *passes*, simulate against the supplied testbench.
3. If sim *fails*, build a typed :class:`FailureDiagnosis` via F2.4's
   ``build_failure_diagnosis``. The diagnosis (not the raw simulator stdout)
   is what feeds the repair prompt — the F4.4 AC is explicit about this.
4. Call ``router.generate(RTL_REPAIR, failure=diagnosis)``. The F3.2 policy
   infers SEMANTIC from the present ``failure`` and routes to the OUTER loop
   slot (frontier model in the default config).
5. Re-run lint + elaborate + sim on the repaired RTL. Loop bounded by
   ``outer_max_attempts``. On exhaustion: ``escalate_to=HUMAN``.

Pure of orchestration — the driver does not promote heads or mutate stage
state on its own; the control graph (F5) consumes the outcome and runs the
appropriate :mod:`chip_agent.graph.blackboard` transitions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from chip_agent.agents.rtl_gen import (
    Elaborator,
    Linter,
    RTLGenerationAgent,
    RTLGenerationOutcome,
    _find_parent,
    _render_inferred_instantiation,
    _sibling_modules,
)
from chip_agent.design_state import (
    ArtifactRef,
    ContractArtifact,
    DesignPlan,
    EscalationLevel,
    FailureDiagnosis,
    LintResult,
    ModelInvocation,
    ModelRouter,
    ModuleDecl,
    Port,
    Provenance,
    RepairAttempt,
    RTLArtifact,
    SimulationResult,
    Spec,
    Stage,
    TaskType,
    TestbenchArtifact,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.trace import build_failure_diagnosis

__all__ = [
    "RTLStageDriver",
    "RTLStageError",
    "RTLStageOutcome",
    "Simulator",
]


_OUTER_SYSTEM_PROMPT = """\
You are a Verilog/SystemVerilog semantic-repair tool. The previous RTL passes
lint and elaborate but FAILS simulation — the testbench observed a behavioural
mismatch. Use the typed failure diagnosis (not raw simulator output) to find
the root cause and emit a corrected module.

Constraints:
* Do not change the port list.
* Do not introduce latches or unsynthesisable constructs.
* Output ONLY the repaired RTL — no preamble, no markdown fences, no prose.
""".strip()


_FENCE_RE = re.compile(
    r"^\s*```(?:verilog|systemverilog|sv|v)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Search-anywhere variant: a fenced block may follow prose. Used as the
# first fallback when the strict full-string match fails.
_FENCE_SEARCH_RE = re.compile(
    r"```(?:verilog|systemverilog|sv|v)?\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

# When the model wraps RTL in reasoning prose with no fences, fall back
# to extracting the canonical ``module <name> ... endmodule`` span. The
# start anchor MUST be start-of-line so prose mention of the word
# "module" doesn't false-match. The ``endmodule`` end anchor accepts
# either start-of-line OR mid-line — single-line module bodies (common
# in test fixtures and trivial wrappers) put both keywords on the same
# line. The token "endmodule" outside Verilog source is vanishingly
# rare, so the looser end anchor is safe.
_MODULE_RE = re.compile(
    r"(?m)^\s*module\s+\w+\b.*?\bendmodule\b",
    re.DOTALL,
)


class Simulator(Protocol):
    """The cocotb-like seam — :class:`SimulationResult` in / out is typed."""

    def simulate(
        self, rtl: RTLArtifact, tb: TestbenchArtifact, *, seed: int = 0,
    ) -> SimulationResult: ...


class RTLStageError(ValueError):
    """The driver was misconfigured or the router produced unusable text."""


@dataclass(frozen=True)
class RTLStageOutcome:
    """Result of one ``drive_module`` invocation.

    ``passed`` is True iff lint + elaborate + sim all closed. ``escalate_to``
    is ``None`` on success, ``EscalationLevel.OUTER`` when the inner loop
    couldn't converge (control graph should escalate INNER -> OUTER), or
    ``EscalationLevel.HUMAN`` when both the outer loop AND the F12.5 EXHAUSTED
    frontier-fallback attempt exhausted their budgets.

    ``frontier_fallback_used`` (F12.5) is ``True`` when the EXHAUSTED rung
    fired — regardless of whether it ultimately passed. Lets callers
    distinguish "local outer model converged" from "frontier rescued it"
    for telemetry / cost analysis without re-reading the audit log.
    """

    passed: bool
    escalate_to: EscalationLevel | None
    rtl: RTLArtifact
    rtl_ref: ArtifactRef
    lint: LintResult
    elaborate: LintResult | None
    sim: SimulationResult | None
    diagnosis: FailureDiagnosis | None
    inner_attempts: int
    outer_attempts: int
    versions: list[int]
    last_failure: ArtifactRef | None
    frontier_fallback_used: bool = False


class RTLStageDriver:
    """Runs the inner + outer repair loops for one module within their budgets."""

    OUTER_SYSTEM_PROMPT = _OUTER_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        gen_agent: RTLGenerationAgent,
        simulator: Simulator,
        store: SqliteArtifactStore,
        router: ModelRouter,
        linter: Linter,
        elaborator: Elaborator,
        design_id: str,
        outer_max_attempts: int = 3,
        language: str = "verilog",
        agent_name: str = "rtl_specialist",
        audit_log: SqliteAuditLog | None = None,
    ) -> None:
        if not design_id:
            raise ValueError("design_id must be non-empty")
        if outer_max_attempts < 1:
            raise ValueError("outer_max_attempts must be >= 1")
        if gen_agent.design_id != design_id:
            raise RTLStageError(
                f"gen_agent.design_id {gen_agent.design_id!r} != driver.design_id "
                f"{design_id!r}"
            )
        self.gen_agent = gen_agent
        self.simulator = simulator
        self.store = store
        self.router = router
        self.linter = linter
        self.elaborator = elaborator
        self.design_id = design_id
        self.outer_max_attempts = outer_max_attempts
        self.language = language
        self.agent_name = agent_name
        # F12.5: optional audit-log feed for the EXHAUSTED frontier fallback.
        # When ``None`` the fallback still fires but no audit event is emitted —
        # matches the F11.5 ``LiteLLMRouter`` pattern of duck-typed wiring.
        self.audit_log = audit_log

    def drive_module(
        self,
        plan: DesignPlan,
        module_id: str,
        tb: TestbenchArtifact,
        *,
        spec: Spec | None = None,
        contract: ContractArtifact | None = None,
        hint_text: str | None = None,
        sim_seed: int = 0,
    ) -> RTLStageOutcome:
        """Drive one module from plan -> validated RTL within both loop budgets.

        F23.3: ``hint_text`` is an optional operator-guidance block (rendered
        by ``graph.human_repair.hint_prompt_section`` from a distilled
        ``HumanHint``) inlined into every outer-repair prompt. ``None``
        (the default) leaves the prompt byte-identical to pre-F23.
        """
        if plan.design_id != self.design_id:
            raise RTLStageError(
                f"plan.design_id {plan.design_id!r} != driver.design_id {self.design_id!r}"
            )
        module = _find_module(plan, module_id)
        # F20.2: cross-module context — bind once for both outer-loop
        # repair-prompt sites (main + F12.5 EXHAUSTED-rung fallback).
        parent = _find_parent(plan, module_id)
        siblings = _sibling_modules(plan, module_id)

        # --- Inner loop -----------------------------------------------------
        inner: RTLGenerationOutcome = self.gen_agent.generate_module(
            plan, module_id, spec=spec,
        )
        if not inner.passed:
            # Inner couldn't converge; the control graph will bump escalation
            # INNER -> OUTER and re-enter. F4.4 records the recommendation.
            return RTLStageOutcome(
                passed=False,
                escalate_to=EscalationLevel.OUTER,
                rtl=inner.rtl,
                rtl_ref=inner.rtl_ref,
                lint=inner.lint,
                elaborate=inner.elaborate,
                sim=None,
                diagnosis=None,
                inner_attempts=inner.attempts,
                outer_attempts=0,
                versions=list(inner.versions),
                last_failure=inner.last_failure,
            )

        # --- First simulation ----------------------------------------------
        current_rtl = inner.rtl
        current_lint = inner.lint
        current_elab = inner.elaborate
        current_sim = self.simulator.simulate(current_rtl, tb, seed=sim_seed)
        self.store.put(current_sim)
        versions = list(inner.versions)
        if current_sim.gate_ok:
            return RTLStageOutcome(
                passed=True,
                escalate_to=None,
                rtl=current_rtl,
                rtl_ref=inner.rtl_ref,
                lint=current_lint,
                elaborate=current_elab,
                sim=current_sim,
                diagnosis=None,
                inner_attempts=inner.attempts,
                outer_attempts=0,
                versions=versions,
                last_failure=None,
            )

        # --- Outer loop -----------------------------------------------------
        outer_attempts = 0
        previous_rtl = current_rtl
        previous_sim = current_sim
        diagnosis: FailureDiagnosis | None = None
        last_failure_ref: ArtifactRef | None = current_sim.ref()
        # F20.1 — rationales for each completed outer-loop attempt. The
        # next iteration's prompt receives the last 1-2 entries via
        # ``_outer_repair_prompt(recent_rationales=...)``. Bounded by
        # ``self.outer_max_attempts`` so unbounded growth isn't a concern.
        recent_rationales: list[str] = []

        while outer_attempts < self.outer_max_attempts:
            outer_attempts += 1

            diagnosis = build_failure_diagnosis(
                previous_sim, previous_rtl,
                testbench=tb, store=self.store,   # F20.6 enrichment
            )
            self.store.put(diagnosis)

            # Read the previous RTL source so we can inline it in the prompt.
            previous_source = self.store.get_blob(previous_rtl.source).decode("utf-8")

            user_prompt = _outer_repair_prompt(
                module, previous_source, diagnosis,
                recent_rationales=recent_rationales[-2:],
                parent=parent, siblings=siblings,
                contract=contract, hint_text=hint_text,
            )
            result = self.router.generate(
                TaskType.RTL_REPAIR,
                context={
                    "prompt": user_prompt,
                    "system": self.OUTER_SYSTEM_PROMPT,
                },
                failure=diagnosis,  # F3.2 -> SEMANTIC -> loops.outer
            )
            source = _strip_fences(result.chosen)
            if not source:
                raise RTLStageError(
                    f"router returned empty RTL on outer attempt {outer_attempts}"
                )

            new_rtl, new_rtl_ref = self._persist_repair(
                module=module,
                source=source,
                attempt=outer_attempts,
                invocation=result.invocation,
                previous_rtl_ref=previous_rtl.ref(),
                diagnosis_ref=diagnosis.ref(),
            )
            versions.append(new_rtl_ref.version)

            # F20.1 — extract the rationale for THIS attempt and persist a
            # RepairAttempt artifact carrying it. The rationale call is
            # independent of the repair call (separate router task, separate
            # model decision); the new_rtl_ref is the durable record either
            # way. The next iteration reads ``recent_rationales[-2:]``.
            self._record_repair_rationale(
                module=module, previous_source=previous_source,
                new_source=source, diagnosis=diagnosis,
                previous_rtl_ref=previous_rtl.ref(),
                new_rtl_ref=new_rtl_ref,
                attempt=outer_attempts,
                recent_rationales=recent_rationales,
            )

            # Re-verify inner gates first.
            new_lint = self.linter.lint(new_rtl)
            self.store.put(new_lint)
            if not new_lint.gate_ok:
                previous_rtl = new_rtl
                current_lint = new_lint
                current_elab = None
                current_sim = previous_sim  # unchanged — sim wasn't re-run
                last_failure_ref = new_lint.ref()
                continue

            new_elab = self.elaborator.elaborate(new_rtl)
            self.store.put(new_elab)
            if not new_elab.gate_ok:
                previous_rtl = new_rtl
                current_lint = new_lint
                current_elab = new_elab
                last_failure_ref = new_elab.ref()
                continue

            # Inner gates pass; rerun simulation.
            new_sim = self.simulator.simulate(new_rtl, tb, seed=sim_seed)
            self.store.put(new_sim)
            if new_sim.gate_ok:
                return RTLStageOutcome(
                    passed=True,
                    escalate_to=None,
                    rtl=new_rtl,
                    rtl_ref=new_rtl_ref,
                    lint=new_lint,
                    elaborate=new_elab,
                    sim=new_sim,
                    diagnosis=diagnosis,
                    inner_attempts=inner.attempts,
                    outer_attempts=outer_attempts,
                    versions=versions,
                    last_failure=None,
                )

            # Sim still fails — set up next outer iteration.
            previous_rtl = new_rtl
            previous_sim = new_sim
            current_lint = new_lint
            current_elab = new_elab
            current_sim = new_sim
            last_failure_ref = new_sim.ref()

        # --- F12.5 EXHAUSTED rung: one frontier attempt before HUMAN ------
        # The outer loop's repair budget is gone. Route one more attempt via
        # the EXHAUSTED escalation; the policy resolver picks the model bound
        # to ``routing.tasks.plan`` (by convention the project's frontier),
        # which gets a single shot at the same repair prompt the outer loop
        # was wrestling with. Cost-bounded by definition (n=1, one call).
        diagnosis = build_failure_diagnosis(
            previous_sim, previous_rtl,
            testbench=tb, store=self.store,   # F20.6 enrichment
        )
        self.store.put(diagnosis)
        previous_source = self.store.get_blob(previous_rtl.source).decode("utf-8")
        user_prompt = _outer_repair_prompt(
            module, previous_source, diagnosis,
            parent=parent, siblings=siblings,
            contract=contract, hint_text=hint_text,
        )
        fallback_result = self.router.generate(
            TaskType.RTL_REPAIR,
            context={
                "prompt": user_prompt,
                "system": self.OUTER_SYSTEM_PROMPT,
            },
            failure=diagnosis,
            escalation=EscalationLevel.EXHAUSTED,
        )
        self._emit_frontier_fallback_audit(
            invocation=fallback_result.invocation,
            outer_attempts=outer_attempts,
        )
        fallback_source = _strip_fences(fallback_result.chosen)
        if not fallback_source:
            raise RTLStageError(
                "EXHAUSTED frontier-fallback returned empty RTL",
            )
        new_rtl, new_rtl_ref = self._persist_repair(
            module=module,
            source=fallback_source,
            attempt=outer_attempts + 1,
            invocation=fallback_result.invocation,
            previous_rtl_ref=previous_rtl.ref(),
            diagnosis_ref=diagnosis.ref(),
        )
        versions.append(new_rtl_ref.version)
        new_lint = self.linter.lint(new_rtl)
        self.store.put(new_lint)
        fallback_elab: LintResult | None = (
            self.elaborator.elaborate(new_rtl) if new_lint.gate_ok else None
        )
        if fallback_elab is not None:
            self.store.put(fallback_elab)
        fallback_sim: SimulationResult | None = (
            self.simulator.simulate(new_rtl, tb, seed=sim_seed)
            if (new_lint.gate_ok and fallback_elab is not None and fallback_elab.gate_ok)
            else None
        )
        if fallback_sim is not None:
            self.store.put(fallback_sim)
        if (
            new_lint.gate_ok
            and fallback_elab is not None and fallback_elab.gate_ok
            and fallback_sim is not None and fallback_sim.gate_ok
        ):
            return RTLStageOutcome(
                passed=True,
                escalate_to=None,
                rtl=new_rtl,
                rtl_ref=new_rtl_ref,
                lint=new_lint,
                elaborate=fallback_elab,
                sim=fallback_sim,
                diagnosis=diagnosis,
                inner_attempts=inner.attempts,
                outer_attempts=outer_attempts,
                versions=versions,
                last_failure=None,
                frontier_fallback_used=True,
            )

        # Frontier rescue also failed -> HUMAN.
        # Pick the most-recent failing artifact ref for last_failure.
        if fallback_sim is not None and not fallback_sim.gate_ok:
            last_failure_ref = fallback_sim.ref()
        elif fallback_elab is not None and not fallback_elab.gate_ok:
            last_failure_ref = fallback_elab.ref()
        else:
            last_failure_ref = new_lint.ref()
        return RTLStageOutcome(
            passed=False,
            escalate_to=EscalationLevel.HUMAN,
            rtl=new_rtl,
            rtl_ref=new_rtl_ref,
            lint=new_lint,
            elaborate=fallback_elab,
            sim=fallback_sim if fallback_sim is not None else current_sim,
            diagnosis=diagnosis,
            inner_attempts=inner.attempts,
            outer_attempts=outer_attempts,
            versions=versions,
            last_failure=last_failure_ref,
            frontier_fallback_used=True,
        )

    def _emit_frontier_fallback_audit(
        self, *, invocation: ModelInvocation, outer_attempts: int,
    ) -> None:
        """Record the F12.5 RTL frontier-fallback firing in the audit log.

        Best-effort: when ``audit_log`` is ``None`` (typical for unit tests
        that don't wire one) the event is dropped silently. The fallback
        itself still fires.
        """
        if self.audit_log is None:
            return
        self.audit_log.append(
            design_id=self.design_id,
            event_type=EventType.RTL_FRONTIER_FALLBACK,
            payload={
                "outer_attempts_exhausted": outer_attempts,
                "fallback_invocation": {
                    "provider": invocation.provider,
                    "model": invocation.model,
                    "prompt_tokens": invocation.prompt_tokens,
                    "completion_tokens": invocation.completion_tokens,
                },
            },
        )

    # ----------------------------------------------------------------- internals
    def _persist_repair(
        self,
        *,
        module: ModuleDecl,
        source: str,
        attempt: int,
        invocation: ModelInvocation,
        previous_rtl_ref: ArtifactRef,
        diagnosis_ref: ArtifactRef,
    ) -> tuple[RTLArtifact, ArtifactRef]:
        # Mirror RTLGenerationAgent._persist: Verible's "posix-eof" rule
        # treats a missing trailing newline as a style violation, so every
        # persisted RTL must end with exactly one ``\n``.
        normalized = source.rstrip("\n") + "\n"
        blob = self.store.put_blob(normalized.encode("utf-8"), media_type="text/x-verilog")
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
                inputs=[previous_rtl_ref, diagnosis_ref],
                notes=f"outer attempt {attempt}",
            ),
        )
        ref = self.store.put(rtl)
        loaded = self.store.get(ref)
        assert isinstance(loaded, RTLArtifact)
        return loaded, ref

    def _record_repair_rationale(
        self,
        *,
        module: ModuleDecl,
        previous_source: str,
        new_source: str,
        diagnosis: FailureDiagnosis,
        previous_rtl_ref: ArtifactRef,
        new_rtl_ref: ArtifactRef,
        attempt: int,
        recent_rationales: list[str],
    ) -> None:
        """F20.1 — one router call to extract the model's hypothesis,
        persisted as a :class:`RepairAttempt` artifact + appended to
        ``recent_rationales`` for the next outer-loop iteration's prompt.
        Empty rationales are persisted but NOT appended (the prompt
        builder's ``if non_empty_rationales`` filter would skip them
        anyway; not appending keeps the list strictly meaningful).
        """
        rationale_prompt = _repair_rationale_prompt(
            module=module,
            previous_source=previous_source,
            new_source=new_source,
            diagnosis=diagnosis,
        )
        rationale_result = self.router.generate(
            TaskType.REPAIR_RATIONALE,
            context={
                "prompt": rationale_prompt,
                "system": _RATIONALE_SYSTEM_PROMPT,
            },
        )
        rationale_text = rationale_result.chosen.strip()
        attempt_artifact = RepairAttempt(
            artifact_id=(
                f"{self.design_id}.{module.module_id}.repair_attempt_{attempt}"
            ),
            design_id=self.design_id,
            module_id=module.module_id,
            attempt_index=attempt,
            previous_rtl_ref=previous_rtl_ref,
            new_rtl_ref=new_rtl_ref,
            diagnosis_ref=diagnosis.ref(),
            rationale=rationale_text,
            provenance=Provenance(
                produced_by=Stage.RTL,
                agent=self.agent_name,
                model=rationale_result.invocation,
                inputs=[previous_rtl_ref, new_rtl_ref, diagnosis.ref()],
                notes=f"outer attempt {attempt} rationale",
            ),
        )
        self.store.put(attempt_artifact)
        if rationale_text:
            recent_rationales.append(rationale_text)


def _find_module(plan: DesignPlan, module_id: str) -> ModuleDecl:
    for m in plan.modules:
        if m.module_id == module_id:
            return m
    known = [m.module_id for m in plan.modules]
    raise RTLStageError(
        f"module_id {module_id!r} not in plan; known modules: {known}"
    )


def _strip_fences(text: str) -> str:
    """Extract Verilog from a model response.

    Three layers, most-specific first:

    1. The whole response is a single fenced block (the model followed
       its system prompt).
    2. Prose followed by (or wrapping) a fenced block — emit the fence
       body.
    3. No fences, but a real ``module <name> ... endmodule`` is in
       there somewhere — emit just that span. Live observed shape from
       the UART RX run: 1500 chars of reasoning prose followed by 500
       chars of Verilog, no fences. Without this fallback the agent
       persisted the prose as RTL, Verilator parsed inline-code
       backticks (``\\`test_name\\```) as undefined macros, and the outer
       loop spiralled.

    Falls through to the stripped input when even the module-span
    extraction fails — the caller raises ``RTLStageError`` on an empty
    return, so a true garbage response still fails loud rather than
    persisting prose as RTL.
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


def _render_contract_constraints(contract: ContractArtifact) -> list[str]:
    """F20.8: render the contract as a constraint block for the outer-repair prompt.

    Each subsection is included only when its source field is non-empty
    so the block stays terse for modules with sparse contracts.
    """
    parts: list[str] = [
        "",
        "Contract constraints (these MUST hold — do not regress):",
    ]
    if contract.reset is not None:
        r = contract.reset
        parts.append(
            f"  Reset {r.name}: polarity={r.polarity}, "
            f"synchronicity={r.synchronicity}"
        )
        if r.affects:
            parts.append(f"    affects: {', '.join(r.affects)}")
    for cd in contract.clock_domains:
        line = f"  Clock {cd.name}"
        if cd.frequency_mhz is not None:
            line += f": {cd.frequency_mhz} MHz"
        parts.append(line)
    informative_ports = [
        pa for pa in contract.port_assumptions
        if pa.polarity != "n/a"
        or pa.encoding != "n/a"
        or pa.expected_range is not None
        or pa.notes
    ]
    if informative_ports:
        parts.append("  Port assumptions:")
        for pa in informative_ports:
            fields = []
            if pa.polarity != "n/a":
                fields.append(f"polarity={pa.polarity}")
            if pa.encoding != "n/a":
                fields.append(f"encoding={pa.encoding}")
            if pa.expected_range is not None:
                fields.append(f"range={pa.expected_range}")
            if pa.notes:
                fields.append(f"notes={pa.notes}")
            parts.append(f"    {pa.port_name}: " + ", ".join(fields))
    if contract.behavior_invariants:
        parts.append("  Behavior invariants:")
        for idx, inv in enumerate(contract.behavior_invariants, start=1):
            parts.append(
                f"    {idx}. {inv.name}: {inv.description} "
                f"(condition: {inv.condition})"
            )
    if contract.ambiguity_notes:
        parts.append(
            "  Ambiguity notes (assumed during contract extraction):"
        )
        for note in contract.ambiguity_notes:
            parts.append(f"    - {note}")
    return parts


def _outer_repair_prompt(
    module: ModuleDecl,
    previous_source: str,
    diagnosis: FailureDiagnosis,
    *,
    recent_rationales: Sequence[str] = (),
    parent: ModuleDecl | None = None,
    siblings: Sequence[ModuleDecl] = (),
    contract: ContractArtifact | None = None,
    hint_text: str | None = None,
) -> str:
    parts = [
        f"Module: {module.name} (id={module.module_id})",
        "",
        "Previous RTL (lint-clean, elaborate-clean, but failing sim):",
        previous_source.rstrip(),
    ]
    # F23.3 — operator guidance from an interactive HUMAN turn, anchored at
    # the very top so the model reads it before the symptom. Empty default
    # keeps every pre-F23 caller byte-identical.
    if hint_text:
        parts += ["", hint_text.rstrip()]
    # F20.8 — contract constraints anchored BEFORE the diagnosis so the
    # model reads the target invariants before the symptom. Empty default
    # keeps pre-F20.8 callers byte-identical.
    if contract is not None:
        parts += _render_contract_constraints(contract)
    parts += [
        "",
        "Failure diagnosis:",
        f"  Failing signal: {diagnosis.failing_signal or '<unknown>'}",
        f"  Cycle: {diagnosis.cycle if diagnosis.cycle is not None else '<unknown>'}",
        f"  Expected: {diagnosis.expected or '<unknown>'}",
        f"  Actual: {diagnosis.actual or '<unknown>'}",
    ]
    if diagnosis.suspected_cause:
        parts.append(f"  Suspected cause: {diagnosis.suspected_cause}")
    parts += [
        "",
        "Summary:",
        f"  {diagnosis.nl_summary}",
    ]

    # F20.6 — enriched context. Each section appears only if populated
    # so callers (and tests) that pass an un-enriched diagnosis see the
    # same prompt body as before this feature landed.
    if diagnosis.test_source:
        parts += [
            "",
            "Failing test (cocotb):",
            "```python",
            diagnosis.test_source.rstrip(),
            "```",
        ]
    if diagnosis.window_vcd_summary:
        parts += [
            "",
            "Signal window around the failure (±3 cycles):",
            "```",
            diagnosis.window_vcd_summary.rstrip(),
            "```",
        ]
    if diagnosis.active_signals_at_failure_cycle:
        parts += ["", "Active signals at the failure cycle:"]
        for name, value in sorted(
            diagnosis.active_signals_at_failure_cycle.items(),
        ):
            parts.append(f"  {name} = {value}")

    # F20.1 — prior repair rationales. Empty sequence is the default
    # so callers that don't pass the kwarg see the pre-F20.1 prompt
    # body byte-for-byte. Most-recent-first ("Attempt -1" is the
    # immediately previous attempt) so the model reads the freshest
    # hypothesis first; callers pass the list in attempt-creation
    # order and the renderer reverses for display.
    non_empty_rationales = [r.strip() for r in recent_rationales if r.strip()]
    if non_empty_rationales:
        parts += ["", "Previous repair attempts (most recent first):"]
        for offset, rationale in enumerate(reversed(non_empty_rationales)):
            parts.append(f"  Attempt -{offset + 1}: {rationale}")

    # F20.2 — cross-module context. Empty defaults keep the prompt body
    # byte-identical to pre-F20.2 callers (top modules + single-module
    # plans + tests that don't set the kwargs).
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
                parts.append(f"    {_format_port_inline(p)}")

    parts += [
        "",
        "Repair the module so the testbench passes. Output ONLY the repaired RTL.",
    ]
    return "\n".join(parts)


def _format_port_inline(p: Port) -> str:
    """F20.2 — single-line port renderer for nested sibling enumeration.

    Kept distinct from ``rtl_gen._format_port`` so the leading "- "
    marker is omitted (the section already nests under a bullet),
    while the shape stays consistent.
    """
    desc = f" -- {p.description}" if p.description else ""
    return (
        f"{p.name}: {p.direction}, {p.width} bit"
        f"{'s' if p.width > 1 else ''}{desc}"
    )


_RATIONALE_SYSTEM_PROMPT = """\
You are a chip-design repair-rationale extractor. Given the previous
RTL, the failure diagnosis, and the RTL you just produced as a
repair, write ONE short paragraph (<= 4 sentences) capturing:

  * what hypothesis you acted on when producing the repair, AND
  * what you would try next if this repair also fails.

Output ONLY the paragraph. No preamble, no markdown, no bullet list.
""".strip()


def _repair_rationale_prompt(
    *,
    module: ModuleDecl,
    previous_source: str,
    new_source: str,
    diagnosis: FailureDiagnosis,
) -> str:
    """User prompt for the F20.1 rationale-extraction router call."""
    parts = [
        f"Module: {module.name} (id={module.module_id})",
        "",
        "Failure diagnosis summary:",
        f"  {diagnosis.nl_summary}",
        f"  failing_signal={diagnosis.failing_signal!r}, "
        f"cycle={diagnosis.cycle}, "
        f"expected={diagnosis.expected!r}, actual={diagnosis.actual!r}",
        "",
        "Previous RTL (the version you repaired):",
        previous_source.rstrip(),
        "",
        "Repaired RTL (the version you just produced):",
        new_source.rstrip(),
        "",
        "Write one short paragraph: what hypothesis did you act on, "
        "and what would you try next if this repair also fails?",
    ]
    return "\n".join(parts)
