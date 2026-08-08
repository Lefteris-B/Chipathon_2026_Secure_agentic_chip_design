"""SYNTH stage gate (F9.1).

Mirrors :mod:`chip_agent.graph.rtl_handler`: translates a
:class:`SynthStageOutcome` into deterministic blackboard mutations so
the control graph reads ``passed`` / ``escalate_to`` rather than the
driver's word.

* The :class:`~chip_agent.design_state.SynthesisReport` ref is appended
  to ``ss.results`` so :func:`decide_transition` can see what closed
  the gate.
* ``passed`` ⇒ :func:`promote_to_head` of the netlist ref (head
  advances, new netlist ACCEPTED, prior head SUPERSEDED, status PASSED).
* ``escalate_to == HUMAN`` ⇒ ``escalation = HUMAN``, ``status =
  BLOCKED``, attempts reset to 0.
* ``escalate_to == OUTER`` ⇒ ``escalation = OUTER``, ``status =
  ESCALATED``, attempts reset to 0.
* Otherwise ``status = FAILED`` and ``last_failure`` points at the
  synthesis report so a future repair pass has the diagnostic.
"""

from __future__ import annotations

from chip_agent.agents.synth_stage import SynthStageOutcome
from chip_agent.design_state import (
    DesignState,
    EscalationLevel,
    Stage,
    StageState,
    StageStatus,
)
from chip_agent.graph.blackboard import (
    get_or_create_stage_state,
    promote_to_head,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = ["apply_synth_outcome"]


def apply_synth_outcome(
    design: DesignState,
    outcome: SynthStageOutcome,
    *,
    store: SqliteArtifactStore,
) -> StageState:
    """Apply ``outcome`` to ``design.stages[SYNTH]``."""
    ss = get_or_create_stage_state(design, Stage.SYNTH)
    if outcome.report_ref not in ss.results:
        ss.results.append(outcome.report_ref)

    if outcome.passed:
        promote_to_head(design, Stage.SYNTH, outcome.netlist_ref, store=store)
        ss.last_failure = None
        return ss

    ss.last_failure = outcome.report_ref

    if outcome.escalate_to is EscalationLevel.HUMAN:
        ss.escalation = EscalationLevel.HUMAN
        ss.attempts = 0
        ss.status = StageStatus.BLOCKED
    elif outcome.escalate_to is EscalationLevel.OUTER:
        ss.escalation = EscalationLevel.OUTER
        ss.attempts = 0
        ss.status = StageStatus.ESCALATED
    else:
        ss.status = StageStatus.FAILED

    return ss
