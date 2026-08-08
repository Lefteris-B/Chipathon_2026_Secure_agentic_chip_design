"""OracleGenAgent (F19.4 + F19.4b).

ContractArtifact -> OracleArtifact via a TWO-STAGE router pipeline
that catches the "syntactically valid but semantically wrong Python"
failure mode the live UART RX `baud_gen` run exposed (the oracle
treated a counter's `count` as a static register; downstream layers
chased a wrong ground truth and exhausted the budget).

F19.4b decomposition (inspired by Faver, arXiv 2510.08664, + ChatModel,
arXiv 2506.15066):

  1. **Plan stage** (``TaskType.ORACLE_PLAN``): the model emits a
     JSON object describing the module's per-clock semantics
     (`is_clocked`, `step_summary`, `reset_summary`, `key_state`)
     PLUS a hand-walked `worked_example` of 4-6 cycles where the
     model chooses stim values and reasons about the spec to fill
     `expected_output`. The Faver framing — "clock as event with a
     step() function" — is encoded in the system prompt so the model
     is forced to think per-cycle rather than emit code that returns
     a static dict.

  2. **Code stage** (existing ``TaskType.ORACLE_GEN``): the same
     ``reference(stim) -> observed`` Python module the F19.6 gate +
     F19.8 differential TB consume, generated from the contract WITH
     the plan inlined so the model has to satisfy its own
     worked_example.

  3. **Self-consistency check** (deterministic; no router call):
     run the emitted code in a subprocess (reusing F19.6's
     ``_oracle_runner.py`` seam) against the plan's
     `worked_example` stim, compare per-cycle output to
     `expected_output`. On mismatch, retry the code stage with a
     delta prompt ("your plan says cycle N produces X, your code
     produces Y; fix") up to ``max_self_check_attempts`` (default 2).
     After exhaustion, fall back to the direct (pre-F19.4b) code path
     so the spine doesn't crash on a model that can't satisfy its
     own plan — F19.6's triangulation gate is the warning-only
     downstream backstop.

The plan + worked_example land on ``OracleArtifact.rationale_notes``
(in the F19.4 ``_NON_CONTENT_FIELDS`` exclusion set already, so the
content hash is unchanged — demo goldens stay byte-identical).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from chip_agent.design_state import (
    ArtifactRef,
    ContractArtifact,
    ModelRouter,
    ModuleDecl,
    OracleArtifact,
    Provenance,
    Spec,
    Stage,
    TaskType,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.sandbox import ProcessRunner, SubprocessProcessRunner

__all__ = ["OracleGenAgent", "OracleGenError"]


# Path to the F19.6 static runner script reused for F19.4b's self-check.
_RUNNER_SOURCE_PATH = Path(__file__).parent / "_oracle_runner.py"


# Defensive strip of ```python ... ``` / bare ``` fences the LLM emits
# despite the system-prompt instruction. Mirrors testbench_gen._FENCE_RE.
_FENCE_RE = re.compile(
    r"^\s*```(?:python|py)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Search-anywhere variant: a fenced block may follow prose.
_FENCE_SEARCH_RE = re.compile(
    r"```(?:python|py)?\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

# Extract a leading rationale comment block: a run of consecutive lines
# at the top of the file matching ``# rationale: <text>``. F19.6 reads
# these for diagnosis; the F19.2 _NON_CONTENT_FIELDS exclusion keeps
# them out of the content hash so two oracles with identical bodies
# dedupe even if the model labelled them differently.
_RATIONALE_PREFIX = "# rationale:"


# F19.4c — deterministic post-LLM guard against clock-edge-detection
# patterns in the generated oracle. The per-row contract is: each STIM
# row IS one rising-edge step; the model must NOT track prev_clk, read
# cyc.get('clk'), or implement posedge-shaped logic. The live
# shift_register run that drove this feature used `prev_clk == 0 and
# clk == 1` and produced an all-zeros reference because stim_ramp
# pins clk=1 every row, so the condition never fires past row 0.
_EDGE_DETECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("prev_clk", re.compile(r"\bprev_clk\b", re.IGNORECASE)),
    (
        "cyc.get('clk'",
        re.compile(
            r"\b(?:cyc|cycle|row|step)\s*"
            r"(?:\.get\(\s*['\"]clk['\"]"
            r"|\[\s*['\"]clk['\"]\s*\])",
            re.IGNORECASE,
        ),
    ),
    ("posedge", re.compile(r"\bposedge\b", re.IGNORECASE)),
    (
        "if clk == 1",
        re.compile(
            r"\bif\b[^\n]{0,40}\bclk\b[^\n]{0,40}==\s*1\b",
            re.IGNORECASE,
        ),
    ),
]


def _detect_edge_detection_patterns(source: str) -> str | None:
    """F19.4c: scan oracle source for clock-edge-detection patterns.

    Returns the friendly name of the first matched pattern, or None on
    a clean source. Pure string scan — sufficient to flag the
    wrong-attractor patterns that the live shift_register failure
    surfaced.
    """
    for name, pattern in _EDGE_DETECTION_PATTERNS:
        if pattern.search(source):
            return name
    return None

_ORACLE_PLAN_SYSTEM_PROMPT = """\
You are a chip-design REFERENCE MODEL planner. Given a structured
behavioural contract for one module, emit a JSON object that
captures HOW the module evolves on each clock edge, plus a hand-
walked worked_example you produce by reasoning about the spec.

Output ONLY JSON. No preamble, no markdown fences, no trailing prose.

Schema:

  {
    "is_clocked": <bool>,
    "step_summary": "<one paragraph: what happens on each RISING clock edge — describe the per-cycle state change, NOT a static description>",
    "reset_summary": "<one paragraph: what happens while reset is asserted>",
    "key_state": ["<name of state var 1>", "<name of state var 2>", ...],
    "worked_example": [
      {"cycle_index": 0, "stim": {"<port>": <int>, ...}, "expected_output": {"<port>": <int>, ...}},
      ... 4 to 6 entries ...
    ]
  }

Rules:

* ``is_clocked`` is true iff the contract names a clock domain or a
  clock-like port.
* ``step_summary`` MUST describe the per-cycle state evolution as a
  verb — e.g. "counter decrements by 1 when enable is high; reloads
  to BAUD_DIV-1 when it reaches 0; held at 0 during reset". DO NOT
  produce a static description (no "the counter holds the value").
* ``step_summary`` describes the per-ROW state update: each row of
  the STIM list IS one rising clock edge that has already occurred.
  Do not write "on rising edge of clk" — write "on each row" or
  "per cycle". The downstream reference function MUST iterate
  `for cyc in stim` WITHOUT implementing its own edge detection.
* ``key_state`` lists every internal register the module needs to
  track across cycles.
* ``worked_example`` is YOUR OWN HAND-WALKED simulation of the
  module's behaviour over 4-6 cycles. You CHOOSE the stim values;
  you DERIVE expected_output by reading the contract. Walk one
  cycle at a time:
    - cycle_index 0: reset asserted; expected_output reflects the
      reset state.
    - cycle_index 1: reset released; one or more inputs change.
    - subsequent cycles: exercise normal operation AND at least one
      boundary (overflow, counter reload, state transition, etc.).
  expected_output[N] is what the module produces AFTER the cycle-N
  rising edge — i.e. the value the spec says is observable on that
  cycle.
* For a counter-shaped module, the worked_example MUST show the
  counter CHANGING across cycles. A worked_example where the state
  port is the same across consecutive cycles in normal operation is
  almost always a misread of the spec — re-read and walk again.
""".strip()


_ORACLE_SYSTEM_PROMPT = """\
You are a chip-design REFERENCE MODEL writer. Given a structured
behavioural contract, produce a self-contained Python module that
defines:

  def reference(stim: list[dict]) -> list[dict]: ...

Cycle contract (CRITICAL — read carefully):

* Each STIM row IS one rising clock edge that has already occurred.
  Iterate `for cyc in stim` and apply ONE cycle of state update per
  row.
* DO NOT implement edge detection. DO NOT track `prev_clk`. DO NOT
  read `cyc.get("clk")` or `cyc["clk"]`. DO NOT write `posedge`-shaped
  logic. The clock has already ticked between every consecutive pair
  of rows; the loop body IS the per-edge update.
* The `clk` field may appear in STIM rows for reference; ignore it.
  Reading it will produce a broken reference (the stimulus harness
  pins `clk=1` every row, so `prev_clk == 0 and clk == 1` fires
  only at row 0).
* Cycle 0 is the post-edge state of the reset condition. Cycle 1
  is the post-edge state after reset releases. Each subsequent
  cycle is ONE rising edge past its predecessor.
* Reset semantics override the per-cycle update: when reset is
  asserted on row N, output reset value for row N regardless of
  other inputs.

Correct shape (counter example — note: no edge detection, no
`prev_clk`, no read of `cyc["clk"]`):

    def reference(stim):
        q = 0
        out = []
        for cyc in stim:
            if cyc.get("rst_n", 1) == 0:
                q = 0
            elif cyc.get("en", 0) == 1:
                q = (q + 1) % 256
            out.append({"q": q})
        return out

Output rules:
* Output ONLY Python source — no markdown fences, no preamble, no
  trailing prose.
* Use only the Python standard library.
* The function MUST be importable and pure (no I/O, no global state
  that persists across calls).
* The entry point MUST be named ``reference`` and have the signature
  shown above. You may emit helper functions but ``reference`` is the
  single public callable.

Encoding rules:
* Honour every ``behavior_invariant`` from the contract.
* Treat ``port_assumptions`` widths and ranges as hard constraints
  (e.g. an 8-bit output wraps at modulo 256).
* Apply ``reset`` semantics literally — active_low + async means the
  output is forced to its reset value the moment reset asserts.
* The ``encoding`` dict carries protocol parameters (baud rates, data
  widths, parity, etc.); encode them as constants in the module.
* ``ambiguity_notes`` are FYI: if the contract assumed a behaviour the
  spec did not pin down, make the same assumption here so the F19.6
  triangulation gate can detect a disagreement against the assertions.

Optionally prefix the file with one or more ``# rationale: <one-line
note>`` comments describing your reasoning. These do not change the
contract or function semantics; they are persisted out-of-band for
post-hoc inspection.
""".strip()


class OracleGenError(ValueError):
    """The router output was unparseable or produced invalid Python."""


class OracleGenAgent:
    """ContractArtifact -> OracleArtifact via plan -> code -> self-check.

    Independent of F19.5 :class:`AssertionGenAgent`: distinct class,
    distinct router tasks, distinct context. The F19.6 triangulation
    gate downstream is the empirical check that both produced
    consistent interpretations of the same contract.

    F19.4b: ``generate`` now runs a plan stage first
    (``TaskType.ORACLE_PLAN``) and self-consistency-checks the
    emitted code against the plan's ``worked_example`` before
    persisting. Plan-parse failures and self-check exhaustion both
    degrade gracefully to the pre-F19.4b direct code path so the
    spine doesn't crash on a model that can't satisfy its own plan.
    """

    PLAN_SYSTEM_PROMPT: ClassVar[str] = _ORACLE_PLAN_SYSTEM_PROMPT
    SYSTEM_PROMPT: ClassVar[str] = _ORACLE_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        store: SqliteArtifactStore,
        design_id: str,
        agent_name: str = "oracle_gen",
        max_self_check_attempts: int = 2,
        runner: ProcessRunner | None = None,
        timeout_s: int = 30,
    ) -> None:
        if not design_id:
            raise OracleGenError("design_id must be non-empty")
        if max_self_check_attempts < 0:
            raise OracleGenError(
                "max_self_check_attempts must be >= 0",
            )
        self.router = router
        self.store = store
        self.design_id = design_id
        self.agent_name = agent_name
        self.max_self_check_attempts = max_self_check_attempts
        self.runner: ProcessRunner = (
            runner if runner is not None else SubprocessProcessRunner()
        )
        self.timeout_s = timeout_s

    def generate(
        self,
        contract: ContractArtifact,
        module: ModuleDecl,
        *,
        spec: Spec | None = None,
        system_prompt: str | None = None,
    ) -> OracleArtifact:
        """Plan -> code -> self-check; persist the resulting reference.

        ``system_prompt`` overrides the CODE-stage system prompt only
        (kept for back-compat with existing tests). The plan stage's
        system prompt is fixed.
        """
        self._validate_ids(contract, module)

        # ----- Plan stage (graceful fallback if it doesn't parse) ----
        plan = self._extract_plan(contract, module, spec=spec)

        # ----- Code stage + self-check loop -------------------------
        if plan is not None:
            normalised, code_result = self._generate_code_with_self_check(
                contract, module,
                plan=plan,
                spec=spec,
                system_prompt=system_prompt,
            )
        else:
            normalised, code_result = self._generate_code_direct(
                contract, module,
                spec=spec,
                system_prompt=system_prompt,
            )

        # ----- Persist (rationale_notes carries plan + code header) -
        return self._persist(
            normalised=normalised,
            code_result=code_result,
            contract=contract,
            module=module,
            spec=spec,
            plan=plan,
        )

    # ------------------------------------------------------------- #
    # Plan stage
    # ------------------------------------------------------------- #
    def _extract_plan(
        self,
        contract: ContractArtifact,
        module: ModuleDecl,
        *,
        spec: Spec | None,
    ) -> dict[str, Any] | None:
        """Run the plan-stage router call. Returns None on any failure
        so the caller can degrade to direct code-gen.

        We swallow router / parse exceptions deliberately: the plan is
        a quality improvement, not a hard requirement. The stub
        backend has no plan-stage matcher → its
        ``AssertionError`` here is the trigger for the back-compat
        path that demos and existing tests rely on.
        """
        plan_prompt = _plan_user_prompt(contract, module, spec)
        try:
            plan_result = self.router.generate(
                TaskType.ORACLE_PLAN,
                context={
                    "prompt": plan_prompt,
                    "system": self.PLAN_SYSTEM_PROMPT,
                },
            )
        except Exception:
            return None
        payload = _parse_plan_json(plan_result.chosen)
        if payload is None:
            return None
        if not _validate_plan_shape(payload):
            return None
        return payload

    # ------------------------------------------------------------- #
    # Code stage + self-check
    # ------------------------------------------------------------- #
    def _generate_code_with_self_check(
        self,
        contract: ContractArtifact,
        module: ModuleDecl,
        *,
        plan: dict[str, Any],
        spec: Spec | None,
        system_prompt: str | None,
    ) -> tuple[str, Any]:
        """Generate code with up to ``max_self_check_attempts`` retries
        on worked-example divergence. Returns the normalised source +
        the final code router result so the caller can record the
        invocation in provenance.
        """
        last_mismatch: dict[str, Any] | None = None
        for attempt in range(1, self.max_self_check_attempts + 2):
            prompt = _code_user_prompt_with_plan(
                contract, module, plan,
                last_mismatch=last_mismatch, spec=spec,
            )
            code_result = self.router.generate(
                TaskType.ORACLE_GEN,
                context={
                    "prompt": prompt,
                    "system": system_prompt or self.SYSTEM_PROMPT,
                },
            )
            normalised = _normalise_oracle_source(code_result.chosen)
            mismatch = self._self_check(normalised, plan)
            if mismatch is None:
                return normalised, code_result
            last_mismatch = mismatch
            if attempt > self.max_self_check_attempts:
                # Budget exhausted — fall back to direct code-gen so the
                # spine doesn't crash on a model that can't satisfy its
                # own plan. F19.6's triangulation gate is the
                # warning-only downstream backstop.
                return self._generate_code_direct(
                    contract, module, spec=spec,
                    system_prompt=system_prompt,
                )
        # Defensive: unreachable, but mypy needs a return.
        return self._generate_code_direct(
            contract, module, spec=spec, system_prompt=system_prompt,
        )

    def _generate_code_direct(
        self,
        contract: ContractArtifact,
        module: ModuleDecl,
        *,
        spec: Spec | None,
        system_prompt: str | None,
    ) -> tuple[str, Any]:
        """Pre-F19.4b path: single ORACLE_GEN call, no self-check."""
        prompt = _user_prompt(contract, module, spec)
        code_result = self.router.generate(
            TaskType.ORACLE_GEN,
            context={
                "prompt": prompt,
                "system": system_prompt or self.SYSTEM_PROMPT,
            },
        )
        return _normalise_oracle_source(code_result.chosen), code_result

    def _self_check(
        self,
        source: str,
        plan: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Run ``reference(stim)`` over the plan's worked_example stim
        and compare per-cycle output to ``expected_output``. Returns
        the first mismatch as a dict (cycle / port / expected /
        observed) or None on full agreement. Returns None on any
        runner-level failure (we can't distinguish "bad code" from
        "bad runner" at this layer, and the downstream compile
        check catches the former).
        """
        # F19.4c — deterministic guard runs FIRST. The
        # wrong-attractor (clock-edge-detection) patterns are cheap
        # to catch with a string scan and give a clear correction
        # signal on retry; running the worked_example comparison
        # against an oracle that already implements `prev_clk` /
        # `cyc.get('clk')` would surface a value mismatch the model
        # would misdiagnose as a per-cycle logic bug.
        matched = _detect_edge_detection_patterns(source)
        if matched is not None:
            return {
                "kind": "edge_detection",
                "matched_pattern": matched,
                "message": (
                    "The generated oracle implements clock-edge "
                    f"detection (matched: {matched!r}). Per the "
                    "oracle contract, each STIM row IS one rising "
                    "clock edge — do NOT implement edge detection or "
                    "read cyc.get('clk'). Iterate `for cyc in stim` "
                    "and apply one cycle of state update per row."
                ),
            }
        worked = plan.get("worked_example") or []
        if not worked:
            return None
        stim = [entry["stim"] for entry in worked]
        expected_seq = [entry["expected_output"] for entry in worked]
        try:
            observed = self._run_reference(source, stim)
        except OracleGenError:
            return None
        if observed is None or len(observed) != len(expected_seq):
            return {
                "kind": "length",
                "expected_len": len(expected_seq),
                "observed_len": len(observed) if observed else 0,
            }
        for i, (exp, got) in enumerate(zip(expected_seq, observed, strict=False)):
            for port, want in exp.items():
                have = got.get(port)
                if have != want:
                    return {
                        "kind": "value",
                        "cycle_index": worked[i].get("cycle_index", i),
                        "port": port,
                        "expected": want,
                        "observed": have,
                    }
        return None

    def _run_reference(
        self, source: str, stim: list[dict[str, int]],
    ) -> list[dict[str, int]] | None:
        """F19.4b self-check seam: spawn ``_oracle_runner.py`` against
        ``source`` + an empty assertions stub, read back ``observed``.
        Returns None on any subprocess failure (the caller treats this
        as "self-check inconclusive", which is the same as success —
        we don't penalise the model for runner flakiness).
        """
        with tempfile.TemporaryDirectory(prefix="chip-agent-oracle-plan-") as tmp:
            work = Path(tmp)
            (work / "oracle.py").write_text(source)
            # F19.4b: the runner expects an assertions.py module but
            # an empty list of callsites means the for-loop is empty.
            (work / "assertions.py").write_text("# F19.4b stub\n")
            (work / "input.json").write_text(json.dumps({
                "reference_fn_name": "reference",
                "stim": stim,
                "callsites": [],
            }))
            shutil.copy(_RUNNER_SOURCE_PATH, work / "runner.py")
            proc = self.runner.run(
                [sys.executable, str(work / "runner.py"), str(work)],
                timeout=self.timeout_s,
            )
            if proc.timed_out or proc.returncode != 0:
                return None
            result_path = work / "result.json"
            if not result_path.exists():
                return None
            try:
                payload = json.loads(result_path.read_text())
            except json.JSONDecodeError:
                return None
        observed = payload.get("observed")
        if not isinstance(observed, list):
            return None
        out: list[dict[str, int]] = []
        for entry in observed:
            if not isinstance(entry, dict):
                return None
            out.append({k: v for k, v in entry.items()})
        return out

    # ------------------------------------------------------------- #
    # Validation + persistence
    # ------------------------------------------------------------- #
    def _validate_ids(
        self, contract: ContractArtifact, module: ModuleDecl,
    ) -> None:
        if contract.design_id != self.design_id:
            raise OracleGenError(
                f"contract.design_id {contract.design_id!r} does not match "
                f"agent.design_id {self.design_id!r}"
            )
        if contract.module_id != module.module_id:
            raise OracleGenError(
                f"contract.module_id {contract.module_id!r} does not match "
                f"module.module_id {module.module_id!r}"
            )

    def _persist(
        self,
        *,
        normalised: str,
        code_result: Any,
        contract: ContractArtifact,
        module: ModuleDecl,
        spec: Spec | None,
        plan: dict[str, Any] | None,
    ) -> OracleArtifact:
        # Syntax-validate BEFORE persisting so a broken model output
        # doesn't leave an orphan blob in the store.
        try:
            compile(normalised, "<oracle>", "exec")
        except SyntaxError as e:
            raise OracleGenError(
                f"oracle source failed to compile: {e}; "
                f"first 200 chars: {normalised[:200]!r}"
            ) from e

        rationale_notes = list(_extract_rationale_notes(normalised))
        if plan is not None:
            # F19.4b: prepend the plan as a JSON line in rationale_notes
            # so it survives without entering the content hash. Existing
            # rationale_notes (the model's own ``# rationale:`` comments)
            # follow.
            rationale_notes = [
                f"plan: {json.dumps(plan, sort_keys=True)}",
                *rationale_notes,
            ]

        blob = self.store.put_blob(
            normalised.encode("utf-8"), media_type="text/x-python",
        )

        inputs: list[ArtifactRef] = [contract.ref()]
        if spec is not None:
            inputs.append(spec.ref())

        oracle = OracleArtifact(
            artifact_id=f"{self.design_id}.{module.module_id}.oracle",
            design_id=self.design_id,
            module_id=module.module_id,
            source=blob,
            module_signature=list(module.ports),
            reference_fn_name="reference",
            rationale_notes=rationale_notes,
            provenance=Provenance(
                produced_by=Stage.PLAN,
                agent=self.agent_name,
                model=code_result.invocation,
                inputs=inputs,
            ),
        )
        self.store.put(oracle)
        loaded = self.store.get_by_id(oracle.artifact_id)
        assert isinstance(loaded, OracleArtifact)
        return loaded


# --------------------------------------------------------------------------- #
# F19.4b plan-stage helpers
# --------------------------------------------------------------------------- #
# Match a JSON object greedily; the agent parses the first object it
# finds so a model that prefixes prose still gets parsed.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_plan_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON-parse the plan stage's chosen output.

    Frontier models occasionally wrap JSON in prose despite the system
    prompt; the greedy fallback survives that. Returns None on any
    parse failure so the caller degrades to the direct code path.
    """
    stripped = raw.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJECT_RE.search(stripped)
    if m is None:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _validate_plan_shape(payload: dict[str, Any]) -> bool:
    """F19.4b plan shape check — best-effort, not exhaustive.

    Returns True iff the payload has the minimum keys we need to run
    the self-check. Missing optional keys (``step_summary``,
    ``key_state``) don't disqualify; missing ``worked_example`` does
    because the self-check has nothing to consume.
    """
    worked = payload.get("worked_example")
    if not isinstance(worked, list) or not worked:
        return False
    for entry in worked:
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("stim"), dict):
            return False
        if not isinstance(entry.get("expected_output"), dict):
            return False
        for v in entry["stim"].values():
            if not isinstance(v, int) or isinstance(v, bool):
                return False
        for v in entry["expected_output"].values():
            if not isinstance(v, int) or isinstance(v, bool):
                return False
    return True


def _normalise_oracle_source(raw: str) -> str:
    """Strip fences + ensure a trailing newline."""
    source = _strip_fences(raw)
    if not source.strip():
        raise OracleGenError(
            "model produced no usable Python source after fence stripping"
        )
    return source.rstrip("\n") + "\n"


def _plan_user_prompt(
    contract: ContractArtifact,
    module: ModuleDecl,
    spec: Spec | None,
) -> str:
    """User prompt for the plan stage. Same contract-inlining shape as
    the code-stage prompt but asks for the JSON plan instead of the
    Python reference."""
    body = _user_prompt(contract, module, spec)
    # Replace the closing instruction with the plan-stage shape.
    body = body.removesuffix(
        "Produce the Python reference module now. Entry point: `reference`.\n"
    ).rstrip()
    return (
        body
        + "\n\nProduce the JSON plan now per the schema above. "
        + "The worked_example MUST hand-walk 4-6 cycles."
    )


def _code_user_prompt_with_plan(
    contract: ContractArtifact,
    module: ModuleDecl,
    plan: dict[str, Any],
    *,
    last_mismatch: dict[str, Any] | None,
    spec: Spec | None,
) -> str:
    """Code-stage prompt that inlines the plan + worked_example so the
    model has to satisfy the per-cycle semantics it just planned. On
    a self-check retry, also inlines the prior mismatch as a delta."""
    parts = [_user_prompt(contract, module, spec)]
    parts += [
        "",
        "Your previously-emitted PLAN (you must satisfy it):",
        json.dumps(plan, sort_keys=True, indent=2),
    ]
    if last_mismatch is not None:
        if last_mismatch.get("kind") == "edge_detection":
            # F19.4c — the deterministic guard fired. Surface the
            # matched pattern + per-row contract reminder so the next
            # attempt cannot re-introduce the same wrong-attractor.
            parts += [
                "",
                "CRITICAL — your last attempt failed the edge-detection "
                "guard:",
                f"  {last_mismatch.get('message', '')}",
                (
                    "Re-emit the reference WITHOUT clock-edge "
                    "detection. Iterate `for cyc in stim` and apply "
                    "one cycle of state update per row. Do NOT read "
                    "`cyc.get('clk')`. Do NOT track `prev_clk`. The "
                    f"forbidden pattern matched was: "
                    f"{last_mismatch.get('matched_pattern')!r}."
                ),
            ]
        else:
            parts += [
                "",
                "Your last code attempt diverged from the plan's worked_example:",
                f"  {json.dumps(last_mismatch, sort_keys=True)}",
                "Re-emit the Python reference so the worked_example holds.",
            ]
    parts += [
        "",
        "Produce the Python reference module now. Entry point: `reference`.",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Helpers (existing)
# --------------------------------------------------------------------------- #
def _user_prompt(
    contract: ContractArtifact,
    module: ModuleDecl,
    spec: Spec | None,
) -> str:
    """Inline the contract and module shape into the user prompt."""
    parts: list[str] = [
        f"Target module: {module.name}  (id={module.module_id})",
        f"Description: {module.description}",
    ]
    if module.ports:
        parts += ["", "Ports:"]
        for p in module.ports:
            parts.append(
                f"- {p.name} ({p.direction}, {p.width} bit"
                + ("s" if p.width != 1 else "")
                + (f", {p.description}" if p.description else "")
                + ")"
            )

    parts += ["", "Contract:"]
    if contract.behavior_invariants:
        parts += ["", "  behavior_invariants:"]
        for inv in contract.behavior_invariants:
            parts.append(f"    - {inv.name}: {inv.description}")
            parts.append(f"        condition: {inv.condition}")
    if contract.port_assumptions:
        parts += ["", "  port_assumptions:"]
        for pa in contract.port_assumptions:
            bits = [f"port_name={pa.port_name}"]
            if pa.polarity != "n/a":
                bits.append(f"polarity={pa.polarity}")
            if pa.expected_range is not None:
                bits.append(f"expected_range={list(pa.expected_range)}")
            if pa.encoding != "n/a":
                bits.append(f"encoding={pa.encoding}")
            parts.append("    - " + ", ".join(bits))
    if contract.clock_domains:
        parts += ["", "  clock_domains:"]
        for cd in contract.clock_domains:
            chunk = f"name={cd.name}"
            if cd.frequency_mhz is not None:
                chunk += f", frequency_mhz={cd.frequency_mhz}"
            if cd.source:
                chunk += f", source={cd.source}"
            parts.append(f"    - {chunk}")
    if contract.reset is not None:
        r = contract.reset
        parts += [
            "",
            f"  reset: name={r.name}, polarity={r.polarity}, "
            f"synchronicity={r.synchronicity}, affects={r.affects}",
        ]
    else:
        parts += ["", "  reset: none (combinational module)"]
    if contract.encoding:
        parts += ["", "  encoding:"]
        for k, v in contract.encoding.items():
            parts.append(f"    {k}: {v}")
    if contract.ambiguity_notes:
        parts += ["", "  ambiguity_notes:"]
        for note in contract.ambiguity_notes:
            parts.append(f"    - {note}")

    if spec is not None:
        parts += ["", "Raw spec (for grounding only):", spec.normalized.strip()]

    parts += [
        "",
        "Produce the Python reference module now. Entry point: `reference`.",
    ]
    return "\n".join(parts)


def _strip_fences(text: str) -> str:
    """Extract Python source from a model response.

    Tries strict full-string fence first, then a search-anywhere fence
    so prose-then-fence shapes survive. A truly no-fence prose response
    falls through unchanged; the subsequent ``compile()`` step then
    surfaces it as ``OracleGenError`` before anything is persisted.
    """
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group("body").strip()
    m = _FENCE_SEARCH_RE.search(stripped)
    if m:
        return m.group("body").strip()
    return stripped


def _extract_rationale_notes(source: str) -> list[str]:
    """Pull ``# rationale: ...`` lines from the top of the module.

    Stops at the first non-comment, non-blank line. Blank lines between
    rationale comments are allowed (the model may space them for
    readability). Empty list if no rationale prefix is found.
    """
    notes: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("#"):
            break
        body = line[1:].lstrip()
        if body.lower().startswith("rationale:"):
            notes.append(body[len("rationale:"):].strip())
            continue
        # A non-rationale comment at the top means rationale block
        # already ended (or never started); stop scanning.
        break
    return notes
