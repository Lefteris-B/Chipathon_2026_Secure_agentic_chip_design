"""Bounded cross-stage feedback (F5.4).

Backend stages (SYNTH / PHYSICAL / SIGNOFF) can attribute a failure to an
upstream stage (typically RTL) and re-open it for another pass. The decision
to do so is the :data:`Transition.FEEDBACK_UPSTREAM` branch of
:func:`chip_agent.graph.transitions.decide_transition`; this module applies
it.

Two invariants make this loop bounded and therefore safe:

1. **Global budget.** ``DesignState.global_feedback_budget`` is decremented
   on every fire. ``decide_transition`` only returns
   ``FEEDBACK_UPSTREAM`` while the budget is positive — so this function is
   the only side-effect path that touches the counter, and it is the
   gate's predicate.
2. **Upstream reset is shallow.** The upstream :class:`StageState` is
   reset to a clean inner-loop start (``attempts=0``,
   ``escalation=INNER``, ``status=IN_PROGRESS``, ``head=None``,
   ``last_failure=None``). The previous head/lineage stays in the store
   for provenance — the stage just gets a fresh budget to produce a new
   head. The source stage is left untouched (its failure is the trigger
   and lives in its ``last_failure``).

Calling this when ``global_feedback_budget == 0`` raises
:class:`FeedbackBudgetExhausted` so callers can route to HUMAN — never to
a re-fire. The control graph's gate enforces the same property, so the
exception is defensive: it would fire only if a handler is wired wrong.
"""

from __future__ import annotations

from chip_agent.design_state import (
    BACKEND_STAGES,
    DesignState,
    EscalationLevel,
    FailureDiagnosis,
    Stage,
    StageState,
    StageStatus,
)
from chip_agent.graph.blackboard import get_or_create_stage_state

__all__ = [
    "FeedbackBudgetExhausted",
    "FeedbackOriginError",
    "apply_cross_stage_feedback",
]


class FeedbackBudgetExhausted(RuntimeError):
    """Caller asked for cross-stage feedback after the global budget was spent."""


class FeedbackOriginError(ValueError):
    """The source stage is not a backend stage — only SYNTH / PHYSICAL / SIGNOFF
    may attribute upstream (CLAUDE.md: backend feedback is the carved-out edge)."""


def apply_cross_stage_feedback(
    design: DesignState,
    *,
    from_stage: Stage,
    diagnosis: FailureDiagnosis | None = None,
    target_stage: Stage = Stage.RTL,
    target_module_id: str | None = None,
) -> StageState:
    """Re-open ``target_stage`` and decrement ``global_feedback_budget``.

    Args:
        design: The blackboard. Mutated in place.
        from_stage: The backend stage that produced the failure. Must be in
            :data:`BACKEND_STAGES`.
        diagnosis: Optional :class:`FailureDiagnosis`. Overrides
            ``target_stage`` / ``target_module_id`` if its fields are set —
            the diagnosis is the artifact agents reason over, so it owns
            the routing if it has an opinion.
        target_stage: Default upstream stage (RTL).
        target_module_id: Default module to reset (``None`` for the
            design-level stage container).

    Returns the upstream :class:`StageState` after the reset, so the caller
    can re-prime ``max_attempts`` or attach the diagnosis as an input on
    the next attempt.

    Raises:
        FeedbackOriginError: if ``from_stage`` is not a backend stage.
        FeedbackBudgetExhausted: if the global budget is already ``0``.
    """
    if from_stage not in BACKEND_STAGES:
        raise FeedbackOriginError(
            f"cross-stage feedback may only originate from a backend stage "
            f"({sorted(s.value for s in BACKEND_STAGES)}); got {from_stage.value!r}"
        )
    if design.global_feedback_budget <= 0:
        raise FeedbackBudgetExhausted(
            f"global_feedback_budget exhausted on design {design.design_id!r}; "
            f"route to HUMAN instead of re-firing"
        )

    # Diagnosis-supplied routing overrides defaults.
    if diagnosis is not None:
        if diagnosis.target_stage is not None:
            target_stage = diagnosis.target_stage
        if diagnosis.target_module is not None:
            target_module_id = diagnosis.target_module

    if target_stage == from_stage:
        raise FeedbackOriginError(
            f"target_stage {target_stage.value!r} must be upstream of "
            f"from_stage {from_stage.value!r}, not the same stage"
        )

    design.global_feedback_budget -= 1
    upstream = get_or_create_stage_state(
        design, target_stage, module_id=target_module_id
    )
    upstream.attempts = 0
    upstream.escalation = EscalationLevel.INNER
    upstream.status = StageStatus.IN_PROGRESS
    upstream.head = None
    upstream.last_failure = None
    return upstream
