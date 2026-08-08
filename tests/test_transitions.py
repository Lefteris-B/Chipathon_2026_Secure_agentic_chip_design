"""F5.2 acceptance: decide_transition is a pure function over the blackboard.

Every row of the table from docs/control_graph.md is covered against hand-built
StageState / DesignState fixtures — no model calls, no stores, no I/O.
"""

from __future__ import annotations

import pytest

from chip_agent.design_state import (
    DesignState,
    EscalationLevel,
    Stage,
    StageState,
    StageStatus,
    Transition,
)
from chip_agent.graph.transitions import (
    decide_transition,
    gate_passes,
    never_attributable,
)


# --------------------------------------------------------------------------- #
# Fixtures (no store, no router — purely typed state)
# --------------------------------------------------------------------------- #
def _design(*, feedback_budget: int = 2) -> DesignState:
    return DesignState(
        design_id="d0", name="d0",
        global_feedback_budget=feedback_budget,
    )


def _ss(
    stage: Stage = Stage.RTL,
    *,
    status: StageStatus = StageStatus.IN_PROGRESS,
    attempts: int = 0,
    max_attempts: int = 3,
    escalation: EscalationLevel = EscalationLevel.INNER,
) -> StageState:
    return StageState(
        stage=stage,
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        escalation=escalation,
    )


# --------------------------------------------------------------------------- #
# gate_passes helper
# --------------------------------------------------------------------------- #
def test_gate_passes_reads_status_only() -> None:
    assert gate_passes(_ss(status=StageStatus.PASSED)) is True
    for s in (
        StageStatus.PENDING,
        StageStatus.IN_PROGRESS,
        StageStatus.FAILED,
        StageStatus.ESCALATED,
        StageStatus.BLOCKED,
    ):
        assert gate_passes(_ss(status=s)) is False


# --------------------------------------------------------------------------- #
# ADVANCE
# --------------------------------------------------------------------------- #
def test_passed_status_yields_advance() -> None:
    # ADVANCE wins regardless of escalation / budget — the gate is closed.
    for esc in EscalationLevel:
        for attempts in (0, 3, 99):
            ss = _ss(status=StageStatus.PASSED, attempts=attempts,
                     max_attempts=3, escalation=esc)
            assert decide_transition(ss, _design()) is Transition.ADVANCE


# --------------------------------------------------------------------------- #
# RETRY
# --------------------------------------------------------------------------- #
def test_failure_with_budget_left_yields_retry() -> None:
    ss = _ss(status=StageStatus.IN_PROGRESS, attempts=1, max_attempts=3)
    assert decide_transition(ss, _design()) is Transition.RETRY

    ss_failed = _ss(status=StageStatus.FAILED, attempts=2, max_attempts=3)
    assert decide_transition(ss_failed, _design()) is Transition.RETRY


def test_retry_independent_of_escalation_level() -> None:
    # If there's budget left at the current loop, RETRY — never ESCALATE.
    ss = _ss(
        status=StageStatus.FAILED, attempts=0, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    assert decide_transition(ss, _design()) is Transition.RETRY


# --------------------------------------------------------------------------- #
# ESCALATE
# --------------------------------------------------------------------------- #
def test_inner_exhausted_yields_escalate() -> None:
    ss = _ss(
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.INNER,
    )
    assert decide_transition(ss, _design()) is Transition.ESCALATE


def test_escalate_only_below_outer() -> None:
    # At OUTER with budget exhausted and no upstream attribution -> HUMAN.
    ss = _ss(
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    assert decide_transition(ss, _design()) is Transition.HUMAN


# --------------------------------------------------------------------------- #
# FEEDBACK_UPSTREAM
# --------------------------------------------------------------------------- #
def test_backend_stage_at_outer_with_feedback_budget_and_attribution() -> None:
    ss = _ss(
        stage=Stage.PHYSICAL,
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    decision = decide_transition(
        ss, _design(feedback_budget=2), attributable=lambda _ss: True,
    )
    assert decision is Transition.FEEDBACK_UPSTREAM


def test_signoff_qualifies_for_feedback_upstream() -> None:
    ss = _ss(
        stage=Stage.SIGNOFF,
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    decision = decide_transition(
        ss, _design(feedback_budget=1), attributable=lambda _ss: True,
    )
    assert decision is Transition.FEEDBACK_UPSTREAM


def test_synth_qualifies_for_feedback_upstream() -> None:
    ss = _ss(
        stage=Stage.SYNTH,
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    decision = decide_transition(
        ss, _design(feedback_budget=1), attributable=lambda _ss: True,
    )
    assert decision is Transition.FEEDBACK_UPSTREAM


def test_feedback_needs_positive_global_budget() -> None:
    ss = _ss(
        stage=Stage.PHYSICAL,
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    # No feedback budget -> HUMAN even with attribution.
    decision = decide_transition(
        ss, _design(feedback_budget=0), attributable=lambda _ss: True,
    )
    assert decision is Transition.HUMAN


def test_feedback_needs_upstream_attribution() -> None:
    ss = _ss(
        stage=Stage.PHYSICAL,
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    decision = decide_transition(
        ss, _design(feedback_budget=1), attributable=never_attributable,
    )
    assert decision is Transition.HUMAN


def test_feedback_skipped_for_frontend_stages() -> None:
    # RTL / SPEC / PLAN don't qualify for FEEDBACK_UPSTREAM — only backend.
    for stage in (Stage.SPEC, Stage.PLAN, Stage.RTL):
        ss = _ss(
            stage=stage,
            status=StageStatus.FAILED,
            attempts=3, max_attempts=3,
            escalation=EscalationLevel.OUTER,
        )
        decision = decide_transition(
            ss, _design(feedback_budget=2), attributable=lambda _ss: True,
        )
        assert decision is Transition.HUMAN, f"stage {stage.value!r} should not feedback"


# --------------------------------------------------------------------------- #
# HUMAN
# --------------------------------------------------------------------------- #
def test_outer_exhausted_with_no_feedback_path_yields_human() -> None:
    ss = _ss(
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    assert decide_transition(ss, _design()) is Transition.HUMAN


def test_human_escalation_yields_human() -> None:
    ss = _ss(
        status=StageStatus.BLOCKED,
        attempts=0, max_attempts=3,
        escalation=EscalationLevel.HUMAN,
    )
    # HUMAN escalation -> no escalate rung exists; with no budget, HUMAN.
    # With budget, ladder skips ESCALATE check because escalation is not < OUTER.
    assert decide_transition(_ss(
        status=StageStatus.BLOCKED, attempts=3, max_attempts=3,
        escalation=EscalationLevel.HUMAN,
    ), _design()) is Transition.HUMAN

    # Even at HUMAN with budget left, the function says RETRY only if the gate
    # didn't pass — which is the intent. (The control graph wouldn't typically
    # call decide_transition while the stage is BLOCKED, but the function
    # remains pure and predictable.)
    assert decide_transition(ss, _design()) is Transition.RETRY


# --------------------------------------------------------------------------- #
# AC: pure function — same inputs -> same output, no side effects.
# --------------------------------------------------------------------------- #
def test_function_is_pure() -> None:
    design = _design()
    ss = _ss(
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.INNER,
    )
    snapshot_design = design.model_dump(mode="json")
    snapshot_ss = ss.model_dump(mode="json")
    a = decide_transition(ss, design)
    b = decide_transition(ss, design)
    assert a is b is Transition.ESCALATE
    # The inputs are unchanged after evaluation.
    assert ss.model_dump(mode="json") == snapshot_ss
    assert design.model_dump(mode="json") == snapshot_design


# --------------------------------------------------------------------------- #
# Attribution callable plumbing
# --------------------------------------------------------------------------- #
def test_attributable_receives_the_stage_state() -> None:
    received: list[StageState] = []

    def _attr(ss: StageState) -> bool:
        received.append(ss)
        return True

    target = _ss(
        stage=Stage.SIGNOFF,
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )
    decide_transition(target, _design(feedback_budget=1), attributable=_attr)
    assert received == [target]


# --------------------------------------------------------------------------- #
# Smoke: every Transition value is reachable through the function.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "attempts", "escalation", "stage", "feedback_budget", "attr", "expected"),
    [
        (StageStatus.PASSED,      0, EscalationLevel.INNER, Stage.RTL,      2, False, Transition.ADVANCE),
        (StageStatus.IN_PROGRESS, 1, EscalationLevel.INNER, Stage.RTL,      2, False, Transition.RETRY),
        (StageStatus.FAILED,      3, EscalationLevel.INNER, Stage.RTL,      2, False, Transition.ESCALATE),
        (StageStatus.FAILED,      3, EscalationLevel.OUTER, Stage.PHYSICAL, 2, True,  Transition.FEEDBACK_UPSTREAM),
        (StageStatus.FAILED,      3, EscalationLevel.OUTER, Stage.RTL,      2, False, Transition.HUMAN),
    ],
)
def test_truth_table_coverage(
    status: StageStatus,
    attempts: int,
    escalation: EscalationLevel,
    stage: Stage,
    feedback_budget: int,
    attr: bool,
    expected: Transition,
) -> None:
    ss = _ss(stage=stage, status=status, attempts=attempts,
             max_attempts=3, escalation=escalation)
    design = _design(feedback_budget=feedback_budget)
    decision = decide_transition(
        ss, design, attributable=lambda _ss: attr,
    )
    assert decision is expected
