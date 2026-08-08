"""F5.4 acceptance: bounded cross-stage feedback.

* Feedback fires within budget — the upstream stage is reset, the global
  budget decrements by one, the source stage is untouched.
* Exhausting the budget routes to HUMAN — :func:`apply_cross_stage_feedback`
  refuses to fire and :func:`decide_transition` returns
  :data:`Transition.HUMAN`. Combined, the loop **cannot** run more than
  ``global_feedback_budget`` times.
* The diagnosis carries optional routing — when it specifies
  ``target_stage`` / ``target_module``, the helper honours that and resets
  the right per-module stage.
"""

from __future__ import annotations

import pytest

from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    DesignState,
    EscalationLevel,
    FailureDiagnosis,
    ModuleState,
    Provenance,
    Stage,
    StageState,
    StageStatus,
    Transition,
)
from chip_agent.graph.feedback import (
    FeedbackBudgetExhausted,
    FeedbackOriginError,
    apply_cross_stage_feedback,
)
from chip_agent.graph.transitions import decide_transition


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _design(*, budget: int = 2, with_module: bool = False) -> DesignState:
    d = DesignState(design_id="d0", name="d0", global_feedback_budget=budget)
    if with_module:
        d.modules["top"] = ModuleState(module_id="top", name="top")
    return d


def _backend_outer_failed(stage: Stage) -> StageState:
    return StageState(
        stage=stage,
        status=StageStatus.FAILED,
        attempts=3,
        max_attempts=3,
        escalation=EscalationLevel.OUTER,
    )


def _ref(artifact_id: str, kind: ArtifactKind) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id, version=1, kind=kind, content_hash="sha256:" + "0" * 64,
    )


def _diagnosis(
    *,
    target_stage: Stage | None = Stage.RTL,
    target_module: str | None = None,
) -> FailureDiagnosis:
    return FailureDiagnosis(
        artifact_id="diag.1",
        design_id="d0",
        provenance=Provenance(produced_by=Stage.SIGNOFF),
        target_stage=target_stage,
        target_module=target_module,
        nl_summary="root cause looks like RTL clock-gating",
    )


# --------------------------------------------------------------------------- #
# Within budget: feedback fires and decrements
# --------------------------------------------------------------------------- #
def test_feedback_decrements_budget_and_resets_upstream() -> None:
    design = _design(budget=2)

    # Pre-seed an RTL stage that already had a head and a prior failure.
    rtl_ss = StageState(
        stage=Stage.RTL,
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
        head=_ref("alu.rtl", ArtifactKind.RTL),
        last_failure=_ref("alu.sim", ArtifactKind.SIM),
    )
    design.stages[Stage.RTL] = rtl_ss

    upstream = apply_cross_stage_feedback(
        design, from_stage=Stage.SIGNOFF, diagnosis=_diagnosis(),
    )

    assert design.global_feedback_budget == 1
    # Upstream reset to a clean inner-loop start.
    assert upstream.attempts == 0
    assert upstream.escalation is EscalationLevel.INNER
    assert upstream.status is StageStatus.IN_PROGRESS
    assert upstream.head is None
    assert upstream.last_failure is None


def test_feedback_can_fire_repeatedly_within_budget() -> None:
    design = _design(budget=3)
    for expected_left in (2, 1, 0):
        apply_cross_stage_feedback(
            design, from_stage=Stage.PHYSICAL, diagnosis=_diagnosis(),
        )
        assert design.global_feedback_budget == expected_left


def test_source_stage_is_not_mutated() -> None:
    design = _design(budget=2)
    source = _backend_outer_failed(Stage.SIGNOFF)
    source.last_failure = _ref("sf.diag", ArtifactKind.DIAGNOSIS)
    design.stages[Stage.SIGNOFF] = source

    snapshot = source.model_dump(mode="json")
    apply_cross_stage_feedback(
        design, from_stage=Stage.SIGNOFF, diagnosis=_diagnosis(),
    )
    # Source stage state retains its post-failure state — the trigger lives
    # there as provenance and isn't replayed.
    assert design.stages[Stage.SIGNOFF].model_dump(mode="json") == snapshot


# --------------------------------------------------------------------------- #
# Bounded: exhaustion forces HUMAN
# --------------------------------------------------------------------------- #
def test_apply_refuses_when_budget_is_zero() -> None:
    design = _design(budget=0)
    with pytest.raises(FeedbackBudgetExhausted):
        apply_cross_stage_feedback(
            design, from_stage=Stage.SIGNOFF, diagnosis=_diagnosis(),
        )
    # Budget untouched on refusal — no silent debit.
    assert design.global_feedback_budget == 0


def test_decide_transition_routes_to_human_when_budget_exhausted() -> None:
    design = _design(budget=0)
    source = _backend_outer_failed(Stage.PHYSICAL)
    decision = decide_transition(
        source, design, attributable=lambda _ss: True,
    )
    assert decision is Transition.HUMAN


def test_loop_cannot_fire_more_than_budget_times() -> None:
    # The combined property: as long as the caller respects
    # decide_transition's verdict, the loop is bounded by the initial
    # global_feedback_budget.
    design = _design(budget=2)
    source = _backend_outer_failed(Stage.SIGNOFF)
    fires = 0
    for _ in range(10):  # well past the budget
        decision = decide_transition(
            source, design, attributable=lambda _ss: True,
        )
        if decision is not Transition.FEEDBACK_UPSTREAM:
            break
        apply_cross_stage_feedback(
            design, from_stage=Stage.SIGNOFF, diagnosis=_diagnosis(),
        )
        fires += 1
    assert fires == 2
    # After exhaustion the gate must fall through to HUMAN — never loop again.
    final = decide_transition(source, design, attributable=lambda _ss: True)
    assert final is Transition.HUMAN
    assert design.global_feedback_budget == 0


# --------------------------------------------------------------------------- #
# Diagnosis-supplied routing
# --------------------------------------------------------------------------- #
def test_diagnosis_target_module_routes_to_per_module_stage() -> None:
    design = _design(budget=2, with_module=True)
    # The top module has its own RTL stage state living under ModuleState.stages.
    design.modules["top"].stages[Stage.RTL] = StageState(
        stage=Stage.RTL,
        status=StageStatus.FAILED,
        attempts=3, max_attempts=3,
        escalation=EscalationLevel.OUTER,
        head=_ref("top.rtl", ArtifactKind.RTL),
    )

    upstream = apply_cross_stage_feedback(
        design,
        from_stage=Stage.SIGNOFF,
        diagnosis=_diagnosis(target_module="top"),
    )

    assert design.modules["top"].stages[Stage.RTL] is upstream
    assert upstream.head is None
    assert upstream.attempts == 0


def test_diagnosis_target_stage_overrides_default() -> None:
    design = _design(budget=2)
    upstream = apply_cross_stage_feedback(
        design,
        from_stage=Stage.SIGNOFF,
        diagnosis=_diagnosis(target_stage=Stage.SYNTH),
    )
    # SIGNOFF -> SYNTH is a valid upstream hop the diagnosis can ask for.
    assert upstream.stage is Stage.SYNTH


def test_diagnosis_without_routing_falls_back_to_defaults() -> None:
    design = _design(budget=2)
    upstream = apply_cross_stage_feedback(
        design, from_stage=Stage.SIGNOFF,
        diagnosis=_diagnosis(target_stage=None, target_module=None),
    )
    assert upstream.stage is Stage.RTL  # default target_stage
    assert Stage.RTL in design.stages  # default lives at the design level


# --------------------------------------------------------------------------- #
# Origin / target guardrails
# --------------------------------------------------------------------------- #
def test_origin_must_be_backend_stage() -> None:
    design = _design(budget=2)
    for bad in (Stage.SPEC, Stage.PLAN, Stage.RTL, Stage.GDSII):
        with pytest.raises(FeedbackOriginError):
            apply_cross_stage_feedback(
                design, from_stage=bad, diagnosis=_diagnosis(),
            )
    # Budget untouched — invalid origin must not debit the counter.
    assert design.global_feedback_budget == 2


def test_target_must_differ_from_origin() -> None:
    design = _design(budget=2)
    with pytest.raises(FeedbackOriginError):
        apply_cross_stage_feedback(
            design,
            from_stage=Stage.SIGNOFF,
            diagnosis=_diagnosis(target_stage=Stage.SIGNOFF),
        )
    assert design.global_feedback_budget == 2


# --------------------------------------------------------------------------- #
# Diagnosis routing wins over the keyword default
# --------------------------------------------------------------------------- #
def test_diagnosis_target_overrides_keyword_target_stage() -> None:
    # If the diagnosis says "go to SYNTH" but the caller passed `target_stage=RTL`,
    # the diagnosis wins — it's the artifact agents reason over, and the
    # routing must match what they decided.
    design = _design(budget=2)
    upstream = apply_cross_stage_feedback(
        design,
        from_stage=Stage.SIGNOFF,
        diagnosis=_diagnosis(target_stage=Stage.SYNTH),
        target_stage=Stage.RTL,
    )
    assert upstream.stage is Stage.SYNTH
