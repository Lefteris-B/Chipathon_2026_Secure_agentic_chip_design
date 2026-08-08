"""F1.4 acceptance: promote / record-failure / attempt-bump / escalation transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chip_agent.design_state import (
    ArtifactStatus,
    BlobRef,
    DesignConstraints,
    DesignState,
    EscalationLevel,
    LintResult,
    ModuleState,
    Provenance,
    RTLArtifact,
    Spec,
    Stage,
    StageState,
    StageStatus,
    Violation,
)
from chip_agent.graph.blackboard import (
    BlackboardError,
    bump_attempt,
    escalate,
    get_or_create_stage_state,
    promote_to_head,
    record_failure,
    register_attempt_failure,
)
from chip_agent.store import SqliteArtifactStore


def _now() -> datetime:
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


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
        constraints=DesignConstraints(pdk="sky130A"),
        modules={
            "counter": ModuleState(module_id="counter", name="counter"),
        },
    )


def _spec(store: SqliteArtifactStore) -> Spec:
    s = Spec(
        artifact_id="d0.spec",
        design_id="d0",
        raw_text="counter",
        normalized="normalized",
        provenance=Provenance(produced_by=Stage.SPEC, started_at=_now()),
    )
    store.put(s)
    return store.get_by_id("d0.spec")  # type: ignore[return-value]


def _rtl(store: SqliteArtifactStore, *, suffix: str = "v1") -> RTLArtifact:
    spec = _spec(store)
    art = RTLArtifact(
        artifact_id="d0.counter.rtl",
        design_id="d0",
        module_id="counter",
        top_module="counter",
        source=BlobRef(path="00/0", sha256=suffix * 32, size_bytes=8, media_type="text/x-verilog"),
        provenance=Provenance(produced_by=Stage.RTL, inputs=[spec.ref()]),
    )
    store.put(art)
    return store.get_by_id("d0.counter.rtl")  # type: ignore[return-value]


def _lint_fail() -> LintResult:
    return LintResult(
        artifact_id="d0.counter.lint",
        design_id="d0",
        module_id="counter",
        passed=False,
        violations=[Violation(code="LATCH_INFERRED", severity="error", message="x")],
        provenance=Provenance(produced_by=Stage.RTL),
    )


# ----------------------------------------------------------------- get_or_create
def test_get_or_create_creates_default_state() -> None:
    design = _design()
    ss = get_or_create_stage_state(design, Stage.RTL, module_id="counter")
    assert isinstance(ss, StageState)
    assert ss.stage is Stage.RTL
    assert ss.status is StageStatus.PENDING
    assert ss.attempts == 0
    assert ss.escalation is EscalationLevel.INNER
    # Same call again returns the same object (in-place).
    again = get_or_create_stage_state(design, Stage.RTL, module_id="counter")
    assert again is ss


def test_get_or_create_unknown_module_raises() -> None:
    design = _design()
    with pytest.raises(BlackboardError):
        get_or_create_stage_state(design, Stage.RTL, module_id="ghost")


def test_design_level_stage_lives_on_design_stages() -> None:
    design = _design()
    ss = get_or_create_stage_state(design, Stage.SYNTH)
    assert design.stages[Stage.SYNTH] is ss


# ----------------------------------------------------------------- promote_to_head
def test_promote_marks_old_head_superseded(store: SqliteArtifactStore) -> None:
    design = _design()
    first = _rtl(store, suffix="a")
    second_artifact = RTLArtifact(
        artifact_id="d0.counter.rtl",
        design_id="d0",
        module_id="counter",
        top_module="counter",
        source=BlobRef(path="bb/b", sha256="b" * 64, size_bytes=8, media_type="text/x-verilog"),
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(second_artifact)
    second = store.get_by_id("d0.counter.rtl")

    # First promotion: head established, status -> ACCEPTED.
    ss = promote_to_head(design, Stage.RTL, first.ref(), store=store,
                         module_id="counter")
    assert ss.head == first.ref()
    assert ss.status is StageStatus.PASSED
    assert store.get(first.ref()).status is ArtifactStatus.ACCEPTED

    # Second promotion: new head ACCEPTED, prior head SUPERSEDED.
    ss = promote_to_head(design, Stage.RTL, second.ref(), store=store,
                         module_id="counter")
    assert ss.head == second.ref()
    assert store.get(second.ref()).status is ArtifactStatus.ACCEPTED
    assert store.get(first.ref()).status is ArtifactStatus.SUPERSEDED


def test_promote_to_same_ref_does_not_supersede(store: SqliteArtifactStore) -> None:
    design = _design()
    rtl = _rtl(store)
    promote_to_head(design, Stage.RTL, rtl.ref(), store=store, module_id="counter")
    promote_to_head(design, Stage.RTL, rtl.ref(), store=store, module_id="counter")
    assert store.get(rtl.ref()).status is ArtifactStatus.ACCEPTED


# ----------------------------------------------------------------- record_failure
def test_record_failure_sets_last_failure_and_appends_results(
    store: SqliteArtifactStore,
) -> None:
    design = _design()
    lint = _lint_fail()
    store.put(lint)
    ss = record_failure(design, Stage.RTL, lint.ref(), module_id="counter")
    assert ss.last_failure == lint.ref()
    assert lint.ref() in ss.results
    # Idempotent for the same ref.
    record_failure(design, Stage.RTL, lint.ref(), module_id="counter")
    assert ss.results.count(lint.ref()) == 1


# ----------------------------------------------------------------- bump_attempt
def test_bump_attempt_decrements_budget() -> None:
    design = _design()
    ss = get_or_create_stage_state(design, Stage.RTL, module_id="counter",
                                   max_attempts=3)
    assert ss.budget_left() == 3
    bump_attempt(design, Stage.RTL, module_id="counter")
    assert ss.budget_left() == 2
    assert ss.status is StageStatus.IN_PROGRESS
    bump_attempt(design, Stage.RTL, module_id="counter")
    bump_attempt(design, Stage.RTL, module_id="counter")
    assert ss.budget_left() == 0
    assert ss.status is StageStatus.FAILED


# ----------------------------------------------------------------- escalate
def test_escalate_walks_inner_outer_exhausted_human() -> None:
    """F12.5 inserted EXHAUSTED between OUTER and HUMAN: the rungs are now
    INNER -> OUTER -> EXHAUSTED -> HUMAN."""
    design = _design()
    ss = get_or_create_stage_state(design, Stage.RTL, module_id="counter")
    assert ss.escalation is EscalationLevel.INNER
    escalate(design, Stage.RTL, module_id="counter")
    assert ss.escalation is EscalationLevel.OUTER
    assert ss.status is StageStatus.ESCALATED
    assert ss.attempts == 0  # fresh budget at the new loop
    escalate(design, Stage.RTL, module_id="counter")
    assert ss.escalation is EscalationLevel.EXHAUSTED
    assert ss.status is StageStatus.ESCALATED
    escalate(design, Stage.RTL, module_id="counter")
    assert ss.escalation is EscalationLevel.HUMAN
    assert ss.status is StageStatus.BLOCKED
    # HUMAN is terminal — escalating once more stays at HUMAN.
    escalate(design, Stage.RTL, module_id="counter")
    assert ss.escalation is EscalationLevel.HUMAN


# ----------------------------------------------------------------- composite
def test_register_attempt_failure_walks_escalation_ladder(
    store: SqliteArtifactStore,
) -> None:
    design = _design()
    lint = _lint_fail()
    store.put(lint)

    # Budget of 2 so the test stays short.
    ss = get_or_create_stage_state(design, Stage.RTL, module_id="counter",
                                   max_attempts=2)

    # Two failures inside the INNER loop -> exhausts budget -> escalates to OUTER.
    assert register_attempt_failure(design, Stage.RTL, lint.ref(),
                                    module_id="counter") is EscalationLevel.INNER
    assert ss.attempts == 1
    assert register_attempt_failure(design, Stage.RTL, lint.ref(),
                                    module_id="counter") is EscalationLevel.OUTER
    # Fresh budget after escalation.
    assert ss.attempts == 0
    assert ss.escalation is EscalationLevel.OUTER

    # Two failures inside OUTER -> escalates to EXHAUSTED (F12.5 rung).
    assert register_attempt_failure(design, Stage.RTL, lint.ref(),
                                    module_id="counter") is EscalationLevel.OUTER
    assert register_attempt_failure(design, Stage.RTL, lint.ref(),
                                    module_id="counter") is EscalationLevel.EXHAUSTED
    assert ss.escalation is EscalationLevel.EXHAUSTED

    # Two failures inside EXHAUSTED -> escalates to HUMAN.
    assert register_attempt_failure(design, Stage.RTL, lint.ref(),
                                    module_id="counter") is EscalationLevel.EXHAUSTED
    assert register_attempt_failure(design, Stage.RTL, lint.ref(),
                                    module_id="counter") is EscalationLevel.HUMAN
    assert ss.status is StageStatus.BLOCKED

    # At HUMAN further failures don't escalate (terminal).
    assert register_attempt_failure(design, Stage.RTL, lint.ref(),
                                    module_id="counter") is EscalationLevel.HUMAN
