"""F4.4 acceptance: seeded functional bug fixed via outer loop with diagnosis
(not raw stdout) in the repair prompt; HUMAN on exhaustion."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.agents.rtl_gen import RTLGenerationAgent
from chip_agent.agents.rtl_stage import (
    RTLStageDriver,
    RTLStageError,
    RTLStageOutcome,
)
from chip_agent.design_state import (
    ArtifactKind,
    DesignConstraints,
    DesignPlan,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    LintResult,
    ModelInvocation,
    ModuleDecl,
    Port,
    Provenance,
    RTLArtifact,
    SimulationResult,
    Spec,
    Stage,
    TaskType,
    TestbenchArtifact,
    Violation,
)
from chip_agent.store import SqliteArtifactStore

# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
_DEFAULT_RATIONALE_TEXT = (
    "I hypothesised the reset polarity was inverted on the prior pass. "
    "Next I'd check the enable-signal sampling edge."
)


@dataclass
class StubRouter:
    """Returns canned RTL texts in order. Records every call (incl. failure kwarg).

    F20.1: ``TaskType.REPAIR_RATIONALE`` calls are handled out-of-band — they
    don't consume from ``self.texts`` (so existing tests that pre-date F20.1
    don't need to interleave a rationale string between every repair text)
    and return ``rationale_texts.pop(0)`` when set, falling back to
    :data:`_DEFAULT_RATIONALE_TEXT` so unparameterised tests still get a
    sensible value.
    """

    texts: list[str]
    rationale_texts: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic", model="claude-opus-4-7",
            temperature=0.2, seed=None,
            prompt_tokens=400, completion_tokens=180, cost_usd=0.004,
        )
    )

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        self.calls.append({
            "task": task,
            "context": dict(context),
            "failure": failure,
            "escalation": escalation,
            "n": n,
        })
        if task is TaskType.REPAIR_RATIONALE:
            text = (
                self.rationale_texts.pop(0)
                if self.rationale_texts
                else _DEFAULT_RATIONALE_TEXT
            )
            return GenerationResult(
                candidates=[text], chosen=text, invocation=self.invocation,
            )
        # Repair / generation calls consume from ``texts`` by index, where
        # the index is the count of non-rationale calls seen so far.
        non_rationale_idx = sum(
            1 for c in self.calls[:-1] if c["task"] is not TaskType.REPAIR_RATIONALE
        )
        text = (
            self.texts[non_rationale_idx]
            if non_rationale_idx < len(self.texts)
            else self.texts[-1]
        )
        return GenerationResult(
            candidates=[text], chosen=text, invocation=self.invocation,
        )


@dataclass
class StubChecker:
    """Stand-in for both Linter and Elaborator."""

    passed_per_call: list[bool]
    violations: list[Violation] = field(default_factory=list)
    seen: list[RTLArtifact] = field(default_factory=list)

    def _result(self, rtl: RTLArtifact) -> LintResult:
        idx = len(self.seen)
        passed = self.passed_per_call[idx] if idx < len(self.passed_per_call) else True
        self.seen.append(rtl)
        return LintResult(
            artifact_id=f"{rtl.design_id}.{rtl.module_id}.lint",
            design_id=rtl.design_id, module_id=rtl.module_id,
            passed=passed,
            violations=[] if passed else list(self.violations),
            provenance=Provenance(produced_by=Stage.RTL, inputs=[rtl.ref()]),
        )

    def lint(self, rtl: RTLArtifact) -> LintResult:
        return self._result(rtl)

    def elaborate(self, rtl: RTLArtifact) -> LintResult:
        return self._result(rtl)


@dataclass
class StubSimulator:
    """Returns canned SimulationResult per call.

    ``failing_assertion`` is what the parser will see; the driver feeds it
    through F2.4's build_failure_diagnosis. The CYCLE/SIGNAL fields below
    are what the test asserts the prompt contains.
    """

    pass_per_call: list[bool]
    failing_assertion: str = "ack low at cycle 5, expected high"
    seen: list[tuple[RTLArtifact, TestbenchArtifact, int]] = field(default_factory=list)

    def simulate(
        self,
        rtl: RTLArtifact,
        tb: TestbenchArtifact,
        *,
        seed: int = 0,
    ) -> SimulationResult:
        idx = len(self.seen)
        passed = self.pass_per_call[idx] if idx < len(self.pass_per_call) else True
        self.seen.append((rtl, tb, seed))
        violations: list[Violation] = []
        failing: list[str] = []
        if not passed:
            failing = [self.failing_assertion]
            violations = [Violation(
                code="ASSERT_FAIL", severity="error",
                message=self.failing_assertion,
                location="test_counter::test_overflow",
            )]
        return SimulationResult(
            artifact_id=f"{rtl.design_id}.{rtl.module_id}.sim",
            design_id=rtl.design_id, module_id=rtl.module_id,
            passed=passed,
            tests_total=2, tests_passed=2 if passed else 1,
            failing_assertions=failing,
            violations=violations,
            provenance=Provenance(produced_by=Stage.RTL, inputs=[rtl.ref(), tb.ref()]),
        )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "runs",
    )
    yield s
    s.close()


def _plan() -> DesignPlan:
    return DesignPlan(
        artifact_id="d0.plan", design_id="d0",
        top_module_id="counter",
        modules=[ModuleDecl(
            module_id="counter", name="counter",
            description="4-bit synchronous counter",
            ports=[
                Port(name="clk", direction="in", width=1),
                Port(name="rst", direction="in", width=1),
                Port(name="ack", direction="out", width=1),
                Port(name="q", direction="out", width=4),
            ],
        )],
        provenance=Provenance(produced_by=Stage.PLAN),
    )


def _spec() -> Spec:
    return Spec(
        artifact_id="d0.spec", design_id="d0",
        raw_text="4-bit counter with ack",
        normalized="* Ports: clk, rst, ack, q",
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


def _stage_tb(store: SqliteArtifactStore) -> TestbenchArtifact:
    blob = store.put_blob(b"# cocotb tb\n", media_type="text/x-python")
    tb = TestbenchArtifact(
        artifact_id="d0.counter.tb", design_id="d0",
        module_id="counter", target_module="counter",
        framework="cocotb", source=blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(tb)
    loaded = store.get_by_id(tb.artifact_id)
    assert isinstance(loaded, TestbenchArtifact)
    return loaded


_GEN_RTL_V1 = "module counter(input clk, input rst, output ack, output [3:0] q); endmodule"
_GEN_RTL_V2 = "module counter(input clk, input rst, output ack, output [3:0] q);\n  // v2\nendmodule"
_GEN_RTL_V3 = "module counter(input clk, input rst, output ack, output [3:0] q);\n  // v3 fix\nendmodule"
_GEN_RTL_V4 = "module counter(input clk, input rst, output ack, output [3:0] q);\n  // v4\nendmodule"


def _build_driver(
    store: SqliteArtifactStore,
    *,
    router_texts: list[str],
    inner_lint_pass: list[bool] | None = None,
    inner_elab_pass: list[bool] | None = None,
    outer_lint_pass: list[bool] | None = None,
    outer_elab_pass: list[bool] | None = None,
    sim_pass: list[bool] | None = None,
    outer_max_attempts: int = 3,
    inner_max_attempts: int = 3,
) -> tuple[RTLStageDriver, StubRouter, StubChecker, StubChecker, StubSimulator]:
    router = StubRouter(texts=router_texts)
    # Inner-loop checkers are private to gen_agent; outer-loop checkers see
    # repairs the driver makes after sim fails.
    inner_linter = StubChecker(
        passed_per_call=inner_lint_pass or [True],
        violations=[Violation(code="LATCH_INFERRED", severity="error",
                              message="latch", location="counter.v:8:5")],
    )
    inner_elaborator = StubChecker(
        passed_per_call=inner_elab_pass or [True],
        violations=[Violation(code="PORT_WIDTH_MISMATCH", severity="error",
                              message="width", location="counter.v:10:5")],
    )
    outer_linter = StubChecker(
        passed_per_call=outer_lint_pass or [True, True, True, True],
        violations=[Violation(code="LATCH_INFERRED", severity="error",
                              message="regression latch")],
    )
    outer_elaborator = StubChecker(
        passed_per_call=outer_elab_pass or [True, True, True, True],
    )
    sim = StubSimulator(pass_per_call=sim_pass or [True])

    gen_agent = RTLGenerationAgent(
        router=router, store=store,
        linter=inner_linter, elaborator=inner_elaborator,
        design_id="d0", max_attempts=inner_max_attempts,
    )
    driver = RTLStageDriver(
        gen_agent=gen_agent, simulator=sim, store=store,
        router=router, linter=outer_linter, elaborator=outer_elaborator,
        design_id="d0", outer_max_attempts=outer_max_attempts,
    )
    return driver, router, outer_linter, outer_elaborator, sim


# --------------------------------------------------------------------------- #
# AC: seeded functional bug is fixed via the outer loop.
# --------------------------------------------------------------------------- #
def test_outer_loop_fixes_seeded_functional_bug(
    store: SqliteArtifactStore,
) -> None:
    # Inner converges on attempt 1 (V1 is lint+elaborate clean).
    # First sim fails (the seeded bug); router returns V2 which sim accepts.
    driver, router, _ol, _oe, _sim = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2],
        sim_pass=[False, True],
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb, spec=_spec())

    assert isinstance(outcome, RTLStageOutcome)
    assert outcome.passed is True
    assert outcome.escalate_to is None
    assert outcome.inner_attempts == 1
    assert outcome.outer_attempts == 1
    # The diagnosis was emitted and stored.
    assert outcome.diagnosis is not None
    assert outcome.diagnosis.kind is ArtifactKind.DIAGNOSIS
    assert outcome.diagnosis.failing_signal == "ack"
    assert outcome.diagnosis.cycle == 5
    # Router calls (filtered to gen/repair): RTL_GEN + RTL_REPAIR.
    # F20.1 interleaves a REPAIR_RATIONALE call after the repair.
    non_rationale_calls = [
        c for c in router.calls if c["task"] is not TaskType.REPAIR_RATIONALE
    ]
    assert [c["task"] for c in non_rationale_calls] == [
        TaskType.RTL_GEN, TaskType.RTL_REPAIR,
    ]
    # The semantic repair call carried the FailureDiagnosis as `failure`,
    # which is what makes F3.2's policy route to loops.outer (frontier).
    repair_call = non_rationale_calls[1]
    assert isinstance(repair_call["failure"], FailureDiagnosis)


# --------------------------------------------------------------------------- #
# AC: the diagnosis (not raw stdout) lands in the repair prompt.
# --------------------------------------------------------------------------- #
def test_repair_prompt_contains_diagnosis_not_raw_stdout(
    store: SqliteArtifactStore,
) -> None:
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2],
        sim_pass=[False, True],
    )
    tb = _stage_tb(store)
    driver.drive_module(_plan(), "counter", tb)

    repair_prompt = router.calls[1]["context"]["prompt"]
    # Typed diagnosis fields appear by name.
    assert "Failing signal: ack" in repair_prompt
    assert "Cycle: 5" in repair_prompt
    assert "Expected: high" in repair_prompt
    assert "Actual: low" in repair_prompt
    # F2.4's deterministic NL summary lands in the prompt.
    assert "Signal `ack` was `low` at cycle 5" in repair_prompt
    # Critical: the prompt must NOT carry raw simulator stdout / log lines.
    # cocotb logs look like "  INFO     cocotb.regression..." etc.
    assert "cocotb.regression" not in repair_prompt
    assert "INFO" not in repair_prompt
    assert "stdout" not in repair_prompt.lower()


# --------------------------------------------------------------------------- #
# AC: on outer-loop exhaustion + F12.5 EXHAUSTED-rung failure, escalate_to == HUMAN.
# --------------------------------------------------------------------------- #
def test_outer_loop_and_frontier_fallback_exhaustion_escalates_to_human(
    store: SqliteArtifactStore,
) -> None:
    # Every sim fails; outer budget is 2 + F12.5 EXHAUSTED attempt = 3 repair calls.
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2, _GEN_RTL_V3, _GEN_RTL_V3],
        sim_pass=[False, False, False, False, False],
        outer_max_attempts=2,
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb)

    assert outcome.passed is False
    assert outcome.escalate_to is EscalationLevel.HUMAN
    assert outcome.outer_attempts == 2
    assert outcome.last_failure is not None
    assert outcome.last_failure.kind is ArtifactKind.SIM
    # F12.5: outer exhaustion now fires one EXHAUSTED-rung frontier attempt
    # before HUMAN — flagged on the outcome for telemetry / cost analysis.
    assert outcome.frontier_fallback_used
    # Router calls (filtered to gen / repair): gen + 2 outer repairs +
    # 1 EXHAUSTED-rung repair = 4. F20.1 interleaves rationale calls
    # between repair attempts; this assertion ignores them.
    non_rationale_calls = [
        c for c in router.calls if c["task"] is not TaskType.REPAIR_RATIONALE
    ]
    assert [c["task"] for c in non_rationale_calls] == [
        TaskType.RTL_GEN,
        TaskType.RTL_REPAIR, TaskType.RTL_REPAIR, TaskType.RTL_REPAIR,
    ]
    # The final repair call carried escalation=EXHAUSTED — that's what
    # routes it to the frontier-bound plan task slot in the policy.
    assert non_rationale_calls[-1]["escalation"] is EscalationLevel.EXHAUSTED
    assert all(
        c["escalation"] is EscalationLevel.INNER
        for c in non_rationale_calls[1:-1]
    )
    # Every repair call carried a FailureDiagnosis (no slipping back to
    # SYNTACTIC because we lost the diagnosis somewhere).
    assert all(
        isinstance(c["failure"], FailureDiagnosis)
        for c in router.calls if c["task"] is TaskType.RTL_REPAIR
    )


# --------------------------------------------------------------------------- #
# Inner-loop failure short-circuits to escalate_to=OUTER without running sim.
# --------------------------------------------------------------------------- #
def test_inner_loop_failure_short_circuits_to_outer_escalation(
    store: SqliteArtifactStore,
) -> None:
    # Inner can't converge: every lint call refuses.
    driver, _router, _ol, _oe, sim = _build_driver(
        store,
        router_texts=[
            f"// v1\n{_GEN_RTL_V1}",
            f"// v2\n{_GEN_RTL_V2}",
            f"// v3\n{_GEN_RTL_V3}",
        ],
        inner_lint_pass=[False, False, False],
        outer_max_attempts=3,
        inner_max_attempts=3,
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb)

    assert outcome.passed is False
    assert outcome.escalate_to is EscalationLevel.OUTER
    # Sim must not have run.
    assert sim.seen == []
    assert outcome.sim is None
    assert outcome.diagnosis is None
    assert outcome.outer_attempts == 0


# --------------------------------------------------------------------------- #
# Outer-loop repair that breaks the inner gates still consumes the outer budget.
# --------------------------------------------------------------------------- #
def test_outer_repair_with_inner_regression_consumes_outer_budget(
    store: SqliteArtifactStore,
) -> None:
    # Inner converges on v1. Sim fails. First outer repair (v2) fails the
    # NEW lint pass — a regression. Second outer repair (v3) is lint-clean,
    # elaborate-clean, and sim-clean.
    driver, _router, ol, oe, sim = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2, _GEN_RTL_V3],
        outer_lint_pass=[False, True],   # v2 regresses, v3 passes
        outer_elab_pass=[True, True],
        sim_pass=[False, True],          # only one sim run on the lint-clean v3
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb)

    assert outcome.passed is True
    assert outcome.outer_attempts == 2
    # The outer linter saw v2 then v3.
    assert len(ol.seen) == 2
    # The outer elaborator only saw v3 (v2 short-circuited at lint).
    assert len(oe.seen) == 1
    # Simulator: first sim on the inner-loop RTL (v1, failed) + final sim on v3.
    assert len(sim.seen) == 2


# --------------------------------------------------------------------------- #
# Sim passes immediately -> no outer-loop iterations.
# --------------------------------------------------------------------------- #
def test_sim_passes_first_time_no_outer_loop(store: SqliteArtifactStore) -> None:
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1],
        sim_pass=[True],
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb)
    assert outcome.passed is True
    assert outcome.outer_attempts == 0
    assert outcome.diagnosis is None
    assert outcome.escalate_to is None
    # Only one router call — the original generation.
    assert len(router.calls) == 1
    assert router.calls[0]["task"] is TaskType.RTL_GEN


# --------------------------------------------------------------------------- #
# Provenance + lineage on outer repairs.
# --------------------------------------------------------------------------- #
def test_outer_repair_provenance_links_previous_rtl_and_diagnosis(
    store: SqliteArtifactStore,
) -> None:
    driver, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2],
        sim_pass=[False, True],
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb)

    history = store.history("d0.counter.rtl")
    assert len(history) == 2
    _v1, v2 = history
    # v2's provenance carries the diagnosis ref + the prior RTL ref.
    input_kinds = {r.kind for r in v2.provenance.inputs}
    assert ArtifactKind.DIAGNOSIS in input_kinds
    assert ArtifactKind.RTL in input_kinds
    assert v2.provenance.notes == "outer attempt 1"
    # The outcome's diagnosis is what got persisted.
    assert outcome.diagnosis is not None
    diag_refs = [r for r in v2.provenance.inputs if r.kind is ArtifactKind.DIAGNOSIS]
    assert diag_refs[0] == outcome.diagnosis.ref()


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #
def test_empty_router_output_on_outer_repair_rejected(
    store: SqliteArtifactStore,
) -> None:
    driver, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, "   \n  "],
        sim_pass=[False, True],
    )
    tb = _stage_tb(store)
    with pytest.raises(RTLStageError):
        driver.drive_module(_plan(), "counter", tb)


# --------------------------------------------------------------------------- #
# F18: ``_strip_fences`` extraction layers (outer-loop variant). Same
# motivation as ``test_rtl_gen.test_strip_fences_*`` but pinned at the
# outer-loop seam where the UART RX run was bitten: the model returns
# prose-then-code instead of a single fenced block; without the new
# extraction layers the entire prose blob was persisted as RTL.
# --------------------------------------------------------------------------- #
def test_strip_fences_extracts_module_span_from_prose_outer() -> None:
    """The outer-loop ``_strip_fences`` mirrors the rtl_gen helper. Pin
    the verbatim shape that bit the live UART RX run: 1500-char
    reasoning preamble followed by the actual Verilog, no fences."""
    from chip_agent.agents.rtl_stage import _strip_fences
    prose_then_rtl = (
        "Looking at the failing test `test_incremental_shift_evolution`, "
        "the issue is likely with the shift direction. The current "
        "implementation shifts right (LSB first reception), inserting "
        "new bits at the MSB position: `{rx_sync, shift_reg[7:1]}`.\n"
        "\n"
        "Let me try the left-shift variant as the fix:\n"
        "\n"
        "module rx_shift_register (\n"
        "    input  wire       clk,\n"
        "    input  wire       rst,\n"
        "    output reg  [7:0] shift_out\n"
        ");\n"
        "    always @(posedge clk) begin\n"
        "        if (rst) shift_out <= 8'b0;\n"
        "        else     shift_out <= {shift_out[6:0], 1'b1};\n"
        "    end\n"
        "endmodule\n"
    )
    extracted = _strip_fences(prose_then_rtl)
    # Inline-code backticks from the prose (the trigger of the live
    # ``Define or directive not defined: '`test_incremental_shift_evolution'``
    # Verilator errors) must NOT survive into the extracted RTL.
    assert "`test_incremental_shift_evolution" not in extracted
    assert "`{rx_sync" not in extracted
    assert "Looking at the failing test" not in extracted
    assert extracted.startswith("module rx_shift_register")
    assert extracted.endswith("endmodule")


def test_strip_fences_handles_fence_after_prose_outer() -> None:
    """A fenced block following prose must be extracted via the
    search-anywhere fallback even though the strict full-string match
    rejects it."""
    from chip_agent.agents.rtl_stage import _strip_fences
    response = (
        "I see the bug. Here is the corrected RTL:\n"
        "\n"
        "```verilog\n"
        "module fix (input clk); endmodule\n"
        "```\n"
        "\n"
        "Done.\n"
    )
    assert _strip_fences(response) == "module fix (input clk); endmodule"


def test_design_id_mismatch_rejected(store: SqliteArtifactStore) -> None:
    driver, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1],
        sim_pass=[True],
    )
    bad_plan = DesignPlan(
        artifact_id="other.plan", design_id="other",
        top_module_id="counter",
        modules=_plan().modules,
        provenance=Provenance(produced_by=Stage.PLAN),
    )
    tb = _stage_tb(store)
    with pytest.raises(RTLStageError):
        driver.drive_module(bad_plan, "counter", tb)


def test_unknown_module_rejected(store: SqliteArtifactStore) -> None:
    driver, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1],
        sim_pass=[True],
    )
    tb = _stage_tb(store)
    with pytest.raises(RTLStageError):
        driver.drive_module(_plan(), "ghost", tb)


def test_outer_max_attempts_validated(store: SqliteArtifactStore) -> None:
    router = StubRouter(texts=[_GEN_RTL_V1])
    gen_agent = RTLGenerationAgent(
        router=router, store=store,
        linter=StubChecker(passed_per_call=[True]),
        elaborator=StubChecker(passed_per_call=[True]),
        design_id="d0",
    )
    with pytest.raises(ValueError):
        RTLStageDriver(
            gen_agent=gen_agent, simulator=StubSimulator(pass_per_call=[True]),
            store=store, router=router,
            linter=StubChecker(passed_per_call=[True]),
            elaborator=StubChecker(passed_per_call=[True]),
            design_id="d0", outer_max_attempts=0,
        )


# --------------------------------------------------------------------------- #
# F12.5 — when the outer loop's budget exhausts, the driver fires one
# attempt at ``escalation=EXHAUSTED`` (the frontier-fallback rung). If
# that attempt closes all three gates, the outcome is ``passed=True`` with
# ``frontier_fallback_used=True``. If it also fails, ``escalate_to=HUMAN``
# with the same flag.
# --------------------------------------------------------------------------- #
def test_frontier_fallback_rescues_after_outer_exhaustion(
    store: SqliteArtifactStore, tmp_path: Path,
) -> None:
    """Outer loop's 2 attempts fail; the EXHAUSTED rung's single attempt
    converges. Spine sees ``passed=True`` and the flag tells callers the
    frontier did the work."""
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2, _GEN_RTL_V3, _GEN_RTL_V3],
        sim_pass=[False, False, False, True],  # initial + 2 outer + EXHAUSTED PASS
        outer_max_attempts=2,
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb)

    assert outcome.passed is True
    assert outcome.escalate_to is None
    assert outcome.frontier_fallback_used
    # Last router call was the EXHAUSTED-rung repair.
    assert router.calls[-1]["task"] is TaskType.RTL_REPAIR
    assert router.calls[-1]["escalation"] is EscalationLevel.EXHAUSTED


def test_frontier_fallback_emits_audit_event_when_log_supplied(
    store: SqliteArtifactStore, tmp_path: Path,
) -> None:
    """F12.5 records an ``RTL_FRONTIER_FALLBACK`` event when an audit log is
    wired into the driver. No log -> silent fallback (no event)."""
    from chip_agent.obs.audit_log import EventType, SqliteAuditLog
    audit = SqliteAuditLog(
        db_path=tmp_path / "audit.sqlite",
        hmac_key=b"f12.5-test-hmac",
    )
    try:
        driver, *_ = _build_driver(
            store,
            router_texts=[_GEN_RTL_V1, _GEN_RTL_V2, _GEN_RTL_V3, _GEN_RTL_V3],
            sim_pass=[False, False, False, True],
            outer_max_attempts=2,
        )
        driver.audit_log = audit  # late-bind so the helper covers both paths
        tb = _stage_tb(store)
        outcome = driver.drive_module(_plan(), "counter", tb)

        assert outcome.frontier_fallback_used
        events = audit.events("d0")
        fb_events = [
            e for e in events
            if e.event_type is EventType.RTL_FRONTIER_FALLBACK
        ]
        assert len(fb_events) == 1
        payload = fb_events[0].payload
        assert payload["outer_attempts_exhausted"] == 2
        assert payload["fallback_invocation"]["provider"] == "anthropic"
    finally:
        audit.close()


def test_frontier_fallback_used_flag_false_when_outer_succeeds(
    store: SqliteArtifactStore,
) -> None:
    """When the outer loop converges within budget, the EXHAUSTED rung
    is never triggered and ``frontier_fallback_used`` stays ``False``."""
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2],
        sim_pass=[False, True],  # initial fails, first outer repair passes
        outer_max_attempts=3,
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb)

    assert outcome.passed is True
    assert not outcome.frontier_fallback_used
    # Last call was the outer repair, not an EXHAUSTED rung.
    assert all(
        c["escalation"] is not EscalationLevel.EXHAUSTED for c in router.calls
    )


# --------------------------------------------------------------------------- #
# F20.6 — _outer_repair_prompt inlines the enriched diagnosis fields
# --------------------------------------------------------------------------- #
def test_outer_repair_prompt_includes_enriched_fields_when_populated() -> None:
    """An enriched FailureDiagnosis must surface its test_source /
    VCD window / signal snapshot in the outer-loop repair prompt so
    the semantic-repair model has the structured context to reason
    over. Empty fields produce a prompt body identical to the
    pre-F20.6 baseline (covered by other tests in this file)."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    from chip_agent.design_state import FailureDiagnosis, ModuleDecl, Port

    module = ModuleDecl(
        module_id="counter", name="counter",
        description="8-bit counter",
        ports=[Port(name="clk", direction="in"), Port(name="q", direction="out", width=8)],
    )
    diagnosis = FailureDiagnosis(
        artifact_id="d0.counter.diag",
        design_id="d0",
        module_id="counter",
        failing_signal="q",
        cycle=4,
        expected="0x5",
        actual="0x4",
        nl_summary="q off by one at cycle 4",
        test_source=(
            "@cocotb.test()\n"
            "async def test_increment(dut):\n"
            "    assert int(dut.q.value) == cycle + 1\n"
        ),
        window_vcd_summary=(
            "Cycle 3: clk=1, q=0x3\n"
            "Cycle 4 (FAILURE): clk=1, q=0x4"
        ),
        active_signals_at_failure_cycle={"clk": "1", "q": "0x4"},
        provenance=Provenance(produced_by=Stage.RTL),
    )
    prompt = _outer_repair_prompt(module, "module counter; endmodule", diagnosis)

    # The standard diagnosis fields are still present.
    assert "Failing signal: q" in prompt
    assert "Cycle: 4" in prompt
    # F20.6 enrichment surfaces.
    assert "Failing test (cocotb):" in prompt
    assert "test_increment" in prompt
    assert "Signal window around the failure" in prompt
    assert "Cycle 4 (FAILURE)" in prompt
    assert "Active signals at the failure cycle:" in prompt
    assert "clk = 1" in prompt
    assert "q = 0x4" in prompt


def test_outer_repair_prompt_omits_enriched_sections_when_empty() -> None:
    """Backwards compat: a diagnosis without any F20.6 fields produces
    a prompt that does NOT carry the new section headers. Pins that
    existing call sites passing pre-F20.6-shaped diagnoses see no
    semantic drift."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    from chip_agent.design_state import FailureDiagnosis, ModuleDecl, Port

    module = ModuleDecl(
        module_id="counter", name="counter", description="counter",
        ports=[Port(name="clk", direction="in"), Port(name="q", direction="out", width=8)],
    )
    diagnosis = FailureDiagnosis(
        artifact_id="d0.counter.diag",
        design_id="d0",
        module_id="counter",
        failing_signal="q",
        cycle=4,
        nl_summary="q off by one",
        provenance=Provenance(produced_by=Stage.RTL),
    )
    prompt = _outer_repair_prompt(module, "module counter; endmodule", diagnosis)

    # No F20.6 section headers when their fields are empty.
    assert "Failing test (cocotb):" not in prompt
    assert "Signal window around the failure" not in prompt
    assert "Active signals at the failure cycle" not in prompt


# --------------------------------------------------------------------------- #
# F20.1 — Cross-attempt rationale persistence.
# --------------------------------------------------------------------------- #
def _bare_diagnosis() -> FailureDiagnosis:
    from chip_agent.design_state import FailureDiagnosis
    return FailureDiagnosis(
        artifact_id="d0.counter.diag",
        design_id="d0",
        module_id="counter",
        failing_signal="q",
        cycle=4,
        nl_summary="q off by one",
        provenance=Provenance(produced_by=Stage.RTL),
    )


def _bare_module() -> ModuleDecl:
    from chip_agent.design_state import ModuleDecl, Port
    return ModuleDecl(
        module_id="counter", name="counter", description="counter",
        ports=[
            Port(name="clk", direction="in"),
            Port(name="q", direction="out", width=8),
        ],
    )


def test_outer_repair_prompt_omits_rationale_section_when_empty() -> None:
    """No ``recent_rationales`` kwarg ⇒ prompt body is identical to the
    pre-F20.1 baseline (no "Previous repair attempts" header)."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    prompt = _outer_repair_prompt(
        _bare_module(), "module counter; endmodule", _bare_diagnosis(),
    )
    assert "Previous repair attempts" not in prompt


def test_outer_repair_prompt_includes_recent_rationales() -> None:
    """``recent_rationales`` is in attempt-creation order; the renderer
    reverses it so ``Attempt -1`` is the most recent."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    prompt = _outer_repair_prompt(
        _bare_module(), "module counter; endmodule", _bare_diagnosis(),
        recent_rationales=[
            "Tried inverting reset polarity.",  # oldest of the two
            "Tried widening q to 9 bits.",      # most recent
        ],
    )
    assert "Previous repair attempts (most recent first):" in prompt
    # "Attempt -1" is the most recent (the LAST element of the input list).
    pos_first = prompt.index("Attempt -1: Tried widening q to 9 bits.")
    pos_second = prompt.index("Attempt -2: Tried inverting reset polarity.")
    assert pos_first < pos_second


def test_outer_repair_prompt_filters_empty_rationale_strings() -> None:
    """Empty/whitespace rationale entries are dropped silently."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    prompt = _outer_repair_prompt(
        _bare_module(), "module counter; endmodule", _bare_diagnosis(),
        recent_rationales=["", "  ", "Real rationale.", ""],
    )
    assert "Previous repair attempts (most recent first):" in prompt
    assert "Attempt -1: Real rationale." in prompt
    # No "Attempt -2" — the empties don't shift the indexing.
    assert "Attempt -2:" not in prompt


def test_outer_loop_persists_repair_attempt_artifacts(
    store: SqliteArtifactStore,
) -> None:
    """Every outer-loop attempt persists one ``RepairAttempt`` artifact
    carrying the canned rationale text the stub router returns."""
    from chip_agent.design_state import ArtifactKind, RepairAttempt
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2, _GEN_RTL_V3, _GEN_RTL_V4],
        # Two outer-loop iterations (attempts 1 + 2 fail sim; attempt 3 passes).
        sim_pass=[False, False, False, True],
    )
    router.rationale_texts = [
        "Attempt 1: tried inverting the reset.",
        "Attempt 2: tried widening q.",
        "Attempt 3: aligned clock edges.",
    ]
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb, spec=_spec())

    assert outcome.passed is True
    assert outcome.outer_attempts == 3

    # Three RepairAttempt artifacts persisted, one per outer-loop attempt.
    all_refs = [
        ref for ref in store.all_refs() if ref.kind is ArtifactKind.REPAIR_ATTEMPT
    ]
    assert len(all_refs) == 3
    by_index: dict[int, RepairAttempt] = {}
    for ref in all_refs:
        art = store.get(ref)
        assert isinstance(art, RepairAttempt)
        by_index[art.attempt_index] = art
    assert by_index[1].rationale == "Attempt 1: tried inverting the reset."
    assert by_index[2].rationale == "Attempt 2: tried widening q."
    assert by_index[3].rationale == "Attempt 3: aligned clock edges."
    # Each attempt's diagnosis_ref points at a diagnosis artifact.
    for art in by_index.values():
        assert art.diagnosis_ref.kind is ArtifactKind.DIAGNOSIS


def test_outer_loop_third_attempt_prompt_carries_prior_rationales(
    store: SqliteArtifactStore,
) -> None:
    """**AC**: a UART-RX-style failure (three repeated similar attempts)
    shows the new rationales in the prompt. The third attempt's prompt
    contains the prior two rationales as context.
    """
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2, _GEN_RTL_V3, _GEN_RTL_V4],
        # 3 outer-loop iterations (2 failing repairs + 1 passing).
        sim_pass=[False, False, False, True],
    )
    router.rationale_texts = [
        "RATIONALE_FOR_ATTEMPT_1",
        "RATIONALE_FOR_ATTEMPT_2",
        "RATIONALE_FOR_ATTEMPT_3",
    ]
    tb = _stage_tb(store)
    driver.drive_module(_plan(), "counter", tb, spec=_spec())

    # Filter to repair calls (skip RTL_GEN + REPAIR_RATIONALE).
    repair_calls = [
        c for c in router.calls if c["task"] is TaskType.RTL_REPAIR
    ]
    assert len(repair_calls) == 3
    # Attempt 1's prompt has NO prior rationales.
    assert "Previous repair attempts" not in repair_calls[0]["context"]["prompt"]
    # Attempt 2's prompt has attempt-1's rationale.
    p2 = repair_calls[1]["context"]["prompt"]
    assert "Attempt -1: RATIONALE_FOR_ATTEMPT_1" in p2
    # Attempt 3's prompt has BOTH prior rationales, most-recent first.
    p3 = repair_calls[2]["context"]["prompt"]
    assert "Attempt -1: RATIONALE_FOR_ATTEMPT_2" in p3
    assert "Attempt -2: RATIONALE_FOR_ATTEMPT_1" in p3


def test_outer_loop_handles_empty_rationale_gracefully(
    store: SqliteArtifactStore,
) -> None:
    """An empty rationale string is persisted but NOT inlined in the
    next attempt's prompt (the strip-and-filter logic in
    ``_outer_repair_prompt`` skips empty entries)."""
    from chip_agent.design_state import ArtifactKind, RepairAttempt
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2, _GEN_RTL_V3, _GEN_RTL_V4],
        sim_pass=[False, False, False, True],
    )
    router.rationale_texts = ["", "   ", "Third one."]
    tb = _stage_tb(store)
    outcome = driver.drive_module(_plan(), "counter", tb, spec=_spec())

    assert outcome.passed
    # All three RepairAttempt artifacts persist (empty rationale + all).
    attempts = sorted(
        (store.get(r) for r in store.all_refs() if r.kind is ArtifactKind.REPAIR_ATTEMPT),
        key=lambda a: a.attempt_index,  # type: ignore[attr-defined]
    )
    assert len(attempts) == 3
    assert isinstance(attempts[0], RepairAttempt)
    assert attempts[0].rationale == ""
    # The third repair prompt has NO "Previous repair attempts" section
    # because the first two rationales were empty.
    repair_calls = [
        c for c in router.calls if c["task"] is TaskType.RTL_REPAIR
    ]
    p3 = repair_calls[2]["context"]["prompt"]
    assert "Previous repair attempts" not in p3


# --------------------------------------------------------------------------- #
# F20.2 — Cross-module RTL prompt context (outer-loop repair side).
# --------------------------------------------------------------------------- #
def test_outer_repair_prompt_omits_parent_siblings_when_kwargs_unset() -> None:
    """Backwards compat: calling _outer_repair_prompt without the new
    F20.2 kwargs yields a prompt that contains no parent / siblings
    section headers — same idiom F20.1 and F20.6 use."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    prompt = _outer_repair_prompt(
        _bare_module(), "module counter; endmodule", _bare_diagnosis(),
    )
    assert "Parent module instantiation" not in prompt
    assert "Sibling modules" not in prompt


def test_outer_repair_prompt_includes_parent_siblings_when_supplied() -> None:
    """Passing ``parent=`` and ``siblings=`` adds the F20.2 sections to
    the outer-loop repair prompt, mirroring _gen_prompt's shape."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    from chip_agent.design_state import ModuleDecl, Port
    parent = ModuleDecl(
        module_id="uart_rx_top", name="uart_rx_top",
        description="UART RX top",
        ports=[Port(name="clk", direction="in", width=1)],
        depends_on=["rx_fsm", "rx_sync"],
    )
    sibling = ModuleDecl(
        module_id="rx_sync", name="rx_sync", description="sync",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rx_in", direction="in", width=1),
            Port(name="rx_clean", direction="out", width=1),
        ],
    )
    rx_fsm = ModuleDecl(
        module_id="rx_fsm", name="rx_fsm", description="fsm",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rx_clean", direction="in", width=1),
            Port(name="valid", direction="out", width=1),
        ],
    )
    prompt = _outer_repair_prompt(
        rx_fsm, "module rx_fsm; endmodule", _bare_diagnosis(),
        parent=parent, siblings=[sibling],
    )
    assert "Parent module instantiation (parent: uart_rx_top):" in prompt
    assert "rx_fsm u_rx_fsm (" in prompt
    assert "Sibling modules" in prompt
    assert "- rx_sync (id=rx_sync):" in prompt
    assert "rx_clean: out" in prompt


# --------------------------------------------------------------------------- #
# F20.8 — Contract-anchored outer-repair prompt: surface ResetSpec,
# PortAssumption, BehaviorInvariant, and ambiguity_notes from the
# F19.3 ContractArtifact so a multi-round repair loop cannot silently
# regress a constraint like "sync reset" or "active-high SE". The
# kwarg defaults to None so pre-F20.8 callers see byte-identical
# prompts.
# --------------------------------------------------------------------------- #
def _shift_reg_contract() -> Any:
    from chip_agent.design_state import (
        BehaviorInvariant,
        ContractArtifact,
        PortAssumption,
        ResetSpec,
    )
    return ContractArtifact(
        artifact_id="d0.counter.contract",
        design_id="d0",
        module_id="counter",
        reset=ResetSpec(
            name="RST",
            polarity="active_high",
            synchronicity="sync",
            affects=["PO"],
        ),
        port_assumptions=[
            PortAssumption(port_name="SE", polarity="positive"),
            PortAssumption(port_name="clk", polarity="n/a"),  # filtered
        ],
        behavior_invariants=[
            BehaviorInvariant(
                name="reset_clears_PO",
                description="PO returns to 0 on sync reset",
                condition="RST -> PO==0",
            ),
        ],
        ambiguity_notes=[
            "spec did not specify shift direction; assumed LSB-first",
        ],
        provenance=Provenance(produced_by=Stage.CONTRACT),
    )


def test_outer_repair_prompt_renders_contract_constraints() -> None:
    """A non-None ``contract`` surfaces ResetSpec / PortAssumption /
    BehaviorInvariant / ambiguity_notes in the prompt. The exact
    literal strings the prompt carries are what the model anchors
    on, so they're worth pinning."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    prompt = _outer_repair_prompt(
        _bare_module(), "module counter; endmodule", _bare_diagnosis(),
        contract=_shift_reg_contract(),
    )
    assert "Contract constraints (these MUST hold" in prompt
    assert "Reset RST: polarity=active_high, synchronicity=sync" in prompt
    assert "affects: PO" in prompt
    assert "Port assumptions:" in prompt
    assert "SE: polarity=positive" in prompt
    # The default "n/a" PortAssumption (clk) is filtered as uninformative.
    assert "clk: polarity=" not in prompt
    assert "Behavior invariants:" in prompt
    assert "1. reset_clears_PO: PO returns to 0 on sync reset" in prompt
    assert "Ambiguity notes (assumed during contract extraction):" in prompt
    assert "assumed LSB-first" in prompt


def test_outer_repair_prompt_omits_contract_section_when_none() -> None:
    """Back-compat: ``contract=None`` (the default) produces a body
    byte-identical to the existing call shape that doesn't pass the
    kwarg at all. Pins zero-behaviour-change for every pre-F20.8
    call site."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    baseline = _outer_repair_prompt(
        _bare_module(), "module counter; endmodule", _bare_diagnosis(),
    )
    explicit_none = _outer_repair_prompt(
        _bare_module(), "module counter; endmodule", _bare_diagnosis(),
        contract=None,
    )
    assert baseline == explicit_none
    assert "Contract constraints" not in baseline


def test_outer_repair_prompt_contract_block_precedes_diagnosis() -> None:
    """Anchor-first ordering: the contract block surfaces BEFORE the
    failure diagnosis so the model reads the *target* before the
    *symptom*. Pins the splice site so future refactors don't
    accidentally append it after the diagnosis."""
    from chip_agent.agents.rtl_stage import _outer_repair_prompt
    prompt = _outer_repair_prompt(
        _bare_module(), "module counter; endmodule", _bare_diagnosis(),
        contract=_shift_reg_contract(),
    )
    assert (
        prompt.index("Contract constraints")
        < prompt.index("Failure diagnosis:")
    )


def test_drive_module_threads_contract_into_repair_prompt(
    store: SqliteArtifactStore,
) -> None:
    """**AC**: a contract passed to ``RTLStageDriver.drive_module``
    lands in the outer-repair prompt the router observes. Pins the
    full wire from the state-graph entry-point to the LLM call.
    """
    driver, router, *_ = _build_driver(
        store,
        router_texts=[_GEN_RTL_V1, _GEN_RTL_V2, _GEN_RTL_V3, _GEN_RTL_V4],
        # One outer iteration: gen passes inner, sim fails, repair, sim passes.
        sim_pass=[False, True],
    )
    tb = _stage_tb(store)
    outcome = driver.drive_module(
        _plan(), "counter", tb,
        spec=_spec(), contract=_shift_reg_contract(),
    )
    assert outcome.passed
    repair_calls = [
        c for c in router.calls if c["task"] is TaskType.RTL_REPAIR
    ]
    assert len(repair_calls) == 1
    prompt = repair_calls[0]["context"]["prompt"]
    assert "Contract constraints" in prompt
    assert "polarity=active_high" in prompt
    assert "synchronicity=sync" in prompt
    assert "SE: polarity=positive" in prompt
