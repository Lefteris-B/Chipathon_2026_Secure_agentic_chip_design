"""Deterministic stage-transition decision (F5.2).

Implements the table from ``docs/control_graph.md``. ``decide_transition`` is
a **pure function** of the blackboard — no model calls, no I/O — so the
control-flow surface is replayable and unit-testable without live models.

The truth table (first match wins):

+----------------------------------------+---------------------+
| Stage state                            | Transition          |
+========================================+=====================+
| ``status == PASSED``                   | ``ADVANCE``         |
| ``budget_left() > 0``                  | ``RETRY``           |
| ``escalation < OUTER``                 | ``ESCALATE``        |
| backend stage, feedback budget left,   | ``FEEDBACK_UPSTREAM`` |
| upstream-attributable                  |                     |
| otherwise                              | ``HUMAN``           |
+----------------------------------------+---------------------+

* RTL agents promote the head via :mod:`chip_agent.graph.blackboard` when
  every check returns ``gate_ok``; the handler sets ``status = PASSED`` and
  ``decide_transition`` reads that. CLAUDE.md: "NEVER advance a stage on a
  model's self-assessment" — the assessment is a verification artifact, the
  gate is its ``gate_ok``, and ``status`` is the handler's promotion of it.
* The ``attributable`` callable is the heuristic-plus-judgment hook for
  cross-stage feedback (F5.4 wires the real one). Defaulting it to ``False``
  keeps F5.2 narrow: backend exhaustion at OUTER falls straight to HUMAN
  unless the caller plugs the heuristic in.
"""

from __future__ import annotations

from collections.abc import Callable

from chip_agent.design_state import (
    BACKEND_STAGES,
    DesignState,
    EscalationLevel,
    StageState,
    StageStatus,
    Transition,
)

__all__ = ["decide_transition", "gate_passes", "never_attributable"]


def gate_passes(stage_state: StageState) -> bool:
    """True iff the stage's binding gate has closed (``status == PASSED``)."""
    return stage_state.status is StageStatus.PASSED


def never_attributable(_stage_state: StageState) -> bool:
    """Default cross-stage attribution heuristic — F5.4 wires the real one."""
    return False


def decide_transition(
    stage_state: StageState,
    design: DesignState,
    *,
    attributable: Callable[[StageState], bool] = never_attributable,
) -> Transition:
    """Return the next :class:`Transition` for ``stage_state`` on ``design``.

    Pure of side effects. First-match-wins over the table in
    ``docs/control_graph.md`` / this module's docstring.
    """
    if gate_passes(stage_state):
        return Transition.ADVANCE

    if stage_state.budget_left() > 0:
        return Transition.RETRY

    # Budget exhausted at the current level. Walk the escalation ladder.
    if stage_state.escalation < EscalationLevel.OUTER:
        return Transition.ESCALATE

    # At OUTER (or already at HUMAN) with no budget. Cross-stage feedback is
    # the only remaining alternative to handing off to a person.
    if (
        stage_state.stage in BACKEND_STAGES
        and design.global_feedback_budget > 0
        and attributable(stage_state)
    ):
        return Transition.FEEDBACK_UPSTREAM

    return Transition.HUMAN
