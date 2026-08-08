"""F4.5 acceptance: head advances iff both gates pass; otherwise the stage state
captures FAILED/ESCALATED with the budget respected."""

from __future__ import annotations

from pathlib import Path

import pytest

from chip_agent.agents.rtl_stage import RTLStageOutcome
from chip_agent.design_state import (
    ArtifactRef,
    ArtifactStatus,
    BlobRef,
    DesignConstraints,
    DesignState,
    EscalationLevel,
    LintResult,
    ModuleState,
    Provenance,
    RTLArtifact,
    SimulationResult,
    Stage,
    StageStatus,
    Violation,
)
from chip_agent.graph.rtl_handler import apply_rtl_outcome
from chip_agent.store import SqliteArtifactStore


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


def _design() -> DesignState:
    return DesignState(
        design_id="d0",
        name="counter",
        constraints=DesignConstraints(),
        modules={"counter": ModuleState(module_id="counter", name="counter")},
    )


def _stage_rtl(
    store: SqliteArtifactStore, *, suffix: str = "a",
) -> RTLArtifact:
    blob = store.put_blob(f"module counter; // {suffix}\nendmodule\n".encode(),
                         media_type="text/x-verilog")
    art = RTLArtifact(
        artifact_id="d0.counter.rtl", design_id="d0",
        module_id="counter", top_module="counter",
        language="verilog", source=blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(art)
    loaded = store.get_by_id(art.artifact_id)
    assert isinstance(loaded, RTLArtifact)
    return loaded


def _stage_lint(
    store: SqliteArtifactStore, *, passed: bool, suffix: str = "a",
) -> LintResult:
    art = LintResult(
        artifact_id=f"d0.counter.lint.{suffix}", design_id="d0",
        module_id="counter",
        passed=passed,
        violations=[] if passed else [Violation(
            code="LATCH_INFERRED", severity="error",
            message="x", location="counter.v:8:5",
        )],
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(art)
    loaded = store.get_by_id(art.artifact_id)
    assert isinstance(loaded, LintResult)
    return loaded


def _stage_elaborate(
    store: SqliteArtifactStore, *, passed: bool, suffix: str = "a",
) -> LintResult:
    art = LintResult(
        artifact_id=f"d0.counter.elaborate.{suffix}", design_id="d0",
        module_id="counter",
        passed=passed,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(art)
    loaded = store.get_by_id(art.artifact_id)
    assert isinstance(loaded, LintResult)
    return loaded


def _stage_sim(
    store: SqliteArtifactStore, *, passed: bool, suffix: str = "a",
) -> SimulationResult:
    art = SimulationResult(
        artifact_id=f"d0.counter.sim.{suffix}", design_id="d0",
        module_id="counter",
        passed=passed,
        tests_total=2, tests_passed=2 if passed else 1,
        failing_assertions=[] if passed else [
            "ack low at cycle 5, expected high",
        ],
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(art)
    loaded = store.get_by_id(art.artifact_id)
    assert isinstance(loaded, SimulationResult)
    return loaded


def _outcome(
    *,
    passed: bool,
    escalate_to: EscalationLevel | None = None,
    rtl: RTLArtifact,
    lint: LintResult,
    elaborate: LintResult | None = None,
    sim: SimulationResult | None = None,
    last_failure: ArtifactRef | None = None,
    inner_attempts: int = 1,
    outer_attempts: int = 0,
) -> RTLStageOutcome:
    return RTLStageOutcome(
        passed=passed,
        escalate_to=escalate_to,
        rtl=rtl,
        rtl_ref=rtl.ref(),
        lint=lint,
        elaborate=elaborate,
        sim=sim,
        diagnosis=None,
        inner_attempts=inner_attempts,
        outer_attempts=outer_attempts,
        versions=[rtl.version],
        last_failure=last_failure,
    )


# --------------------------------------------------------------------------- #
# AC: head advances iff both pass.
# --------------------------------------------------------------------------- #
def test_passed_outcome_promotes_to_head_and_marks_accepted(
    store: SqliteArtifactStore,
) -> None:
    design = _design()
    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=True)
    elab = _stage_elaborate(store, passed=True)
    sim = _stage_sim(store, passed=True)
    outcome = _outcome(passed=True, rtl=rtl, lint=lint, elaborate=elab, sim=sim)

    ss = apply_rtl_outcome(design, outcome, module_id="counter", store=store)

    # Head advanced.
    assert ss.head == rtl.ref()
    assert ss.status is StageStatus.PASSED
    # New RTL is ACCEPTED in the store.
    assert store.get(rtl.ref()).status is ArtifactStatus.ACCEPTED
    # Verification artifacts landed in results.
    assert lint.ref() in ss.results
    assert elab.ref() in ss.results
    assert sim.ref() in ss.results
    # No lingering failure pointer.
    assert ss.last_failure is None


def test_passed_outcome_supersedes_prior_head(
    store: SqliteArtifactStore,
) -> None:
    design = _design()
    first_rtl = _stage_rtl(store, suffix="v1")
    first_lint = _stage_lint(store, passed=True, suffix="v1")
    first_elab = _stage_elaborate(store, passed=True, suffix="v1")
    first_sim = _stage_sim(store, passed=True, suffix="v1")
    apply_rtl_outcome(
        design,
        _outcome(passed=True, rtl=first_rtl, lint=first_lint,
                 elaborate=first_elab, sim=first_sim),
        module_id="counter", store=store,
    )
    # A second pass promotes the new RTL and supersedes the first.
    second_rtl = _stage_rtl(store, suffix="v2")
    second_lint = _stage_lint(store, passed=True, suffix="v2")
    second_elab = _stage_elaborate(store, passed=True, suffix="v2")
    second_sim = _stage_sim(store, passed=True, suffix="v2")
    ss = apply_rtl_outcome(
        design,
        _outcome(passed=True, rtl=second_rtl, lint=second_lint,
                 elaborate=second_elab, sim=second_sim),
        module_id="counter", store=store,
    )
    assert ss.head == second_rtl.ref()
    assert store.get(second_rtl.ref()).status is ArtifactStatus.ACCEPTED
    assert store.get(first_rtl.ref()).status is ArtifactStatus.SUPERSEDED


# --------------------------------------------------------------------------- #
# AC: head does NOT advance when failed.
# --------------------------------------------------------------------------- #
def test_failed_outcome_leaves_head_unchanged_and_records_failure(
    store: SqliteArtifactStore,
) -> None:
    design = _design()
    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=False)
    outcome = _outcome(
        passed=False, escalate_to=EscalationLevel.OUTER,
        rtl=rtl, lint=lint, last_failure=lint.ref(),
        inner_attempts=3,
    )

    ss = apply_rtl_outcome(design, outcome, module_id="counter", store=store)

    # Head must NOT have advanced.
    assert ss.head is None
    # The failed RTL stays DRAFT (not ACCEPTED).
    assert store.get(rtl.ref()).status is ArtifactStatus.DRAFT
    # The failing verification ref is recorded.
    assert ss.last_failure == lint.ref()
    assert lint.ref() in ss.results


# --------------------------------------------------------------------------- #
# Escalation transitions.
# --------------------------------------------------------------------------- #
def test_outer_escalation_resets_budget_and_marks_escalated(
    store: SqliteArtifactStore,
) -> None:
    design = _design()
    # Pre-seed a non-zero attempt counter so the reset is observable.
    from chip_agent.graph.blackboard import get_or_create_stage_state
    pre = get_or_create_stage_state(design, Stage.RTL, module_id="counter")
    pre.attempts = 3
    pre.status = StageStatus.IN_PROGRESS

    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=False)
    apply_rtl_outcome(
        design,
        _outcome(passed=False, escalate_to=EscalationLevel.OUTER,
                 rtl=rtl, lint=lint, last_failure=lint.ref(),
                 inner_attempts=3),
        module_id="counter", store=store,
    )
    ss = design.modules["counter"].stages[Stage.RTL]
    assert ss.escalation is EscalationLevel.OUTER
    assert ss.attempts == 0
    assert ss.status is StageStatus.ESCALATED
    assert ss.last_failure == lint.ref()


def test_human_escalation_blocks_with_fresh_budget(
    store: SqliteArtifactStore,
) -> None:
    design = _design()
    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=True)
    elab = _stage_elaborate(store, passed=True)
    sim = _stage_sim(store, passed=False)
    outcome = _outcome(
        passed=False, escalate_to=EscalationLevel.HUMAN,
        rtl=rtl, lint=lint, elaborate=elab, sim=sim,
        last_failure=sim.ref(),
        inner_attempts=1, outer_attempts=3,
    )

    ss = apply_rtl_outcome(design, outcome, module_id="counter", store=store)

    assert ss.escalation is EscalationLevel.HUMAN
    assert ss.attempts == 0
    assert ss.status is StageStatus.BLOCKED
    assert ss.last_failure == sim.ref()
    # Even though lint + elaborate gates closed, the head did not advance
    # because sim refused.
    assert ss.head is None


def test_failed_without_recommendation_marks_failed(
    store: SqliteArtifactStore,
) -> None:
    design = _design()
    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=False)
    outcome = _outcome(
        passed=False, escalate_to=None,
        rtl=rtl, lint=lint, last_failure=lint.ref(),
    )

    ss = apply_rtl_outcome(design, outcome, module_id="counter", store=store)
    assert ss.status is StageStatus.FAILED
    # No escalation requested -> escalation field unchanged from default.
    assert ss.escalation is EscalationLevel.INNER


# --------------------------------------------------------------------------- #
# Verification artifacts are appended idempotently.
# --------------------------------------------------------------------------- #
def test_results_appended_idempotently(store: SqliteArtifactStore) -> None:
    design = _design()
    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=False)
    outcome = _outcome(
        passed=False, escalate_to=EscalationLevel.OUTER,
        rtl=rtl, lint=lint, last_failure=lint.ref(),
    )
    apply_rtl_outcome(design, outcome, module_id="counter", store=store)
    apply_rtl_outcome(design, outcome, module_id="counter", store=store)
    ss = design.modules["counter"].stages[Stage.RTL]
    # lint.ref() appears exactly once even after two applies.
    assert ss.results.count(lint.ref()) == 1


# --------------------------------------------------------------------------- #
# When only a subset of verification artifacts is present.
# --------------------------------------------------------------------------- #
def test_partial_outcome_only_records_present_refs(
    store: SqliteArtifactStore,
) -> None:
    # No sim — typical of an inner-loop-only failure.
    design = _design()
    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=False)
    outcome = _outcome(
        passed=False, escalate_to=EscalationLevel.OUTER,
        rtl=rtl, lint=lint, elaborate=None, sim=None,
        last_failure=lint.ref(),
    )
    ss = apply_rtl_outcome(design, outcome, module_id="counter", store=store)
    assert lint.ref() in ss.results
    assert not any(r.artifact_id.endswith(".sim.a") for r in ss.results)


# --------------------------------------------------------------------------- #
# StageState is created if absent.
# --------------------------------------------------------------------------- #
def test_creates_stage_state_when_absent(store: SqliteArtifactStore) -> None:
    design = _design()
    assert Stage.RTL not in design.modules["counter"].stages
    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=True)
    elab = _stage_elaborate(store, passed=True)
    sim = _stage_sim(store, passed=True)
    apply_rtl_outcome(
        design,
        _outcome(passed=True, rtl=rtl, lint=lint, elaborate=elab, sim=sim),
        module_id="counter", store=store,
    )
    assert Stage.RTL in design.modules["counter"].stages


# --------------------------------------------------------------------------- #
# Failing RTL artifact identity is preserved even when not promoted.
# --------------------------------------------------------------------------- #
def test_failed_rtl_blobref_unaffected(store: SqliteArtifactStore) -> None:
    design = _design()
    rtl = _stage_rtl(store, suffix="v1")
    lint = _stage_lint(store, passed=False)
    outcome = _outcome(
        passed=False, escalate_to=EscalationLevel.OUTER,
        rtl=rtl, lint=lint, last_failure=lint.ref(),
    )
    apply_rtl_outcome(design, outcome, module_id="counter", store=store)
    # The blob still resolves; failure doesn't tamper with content.
    body = store.get_blob(rtl.source)
    assert body.startswith(b"module counter;")
    assert isinstance(rtl.source, BlobRef)
