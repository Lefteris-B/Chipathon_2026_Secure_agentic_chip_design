"""RTL stage gate (F4.5).

Translates an :class:`RTLStageOutcome` (produced by the F4.3 + F4.4 driver)
into the deterministic blackboard mutations the control graph reads:

* All verification artifacts (lint / elaborate / sim) are appended to the
  stage's ``results`` so :func:`decide_transition` can see what closed —
  or didn't — without re-running the gates.
* ``passed`` ⇒ :func:`promote_to_head` (head advances, new RTL ACCEPTED,
  prior head SUPERSEDED, status PASSED). This is the AC's "head advances
  iff both pass."
* ``escalate_to == OUTER`` ⇒ ``escalation = OUTER``, ``status = ESCALATED``,
  attempts reset to 0 (fresh budget at the next loop slot).
* ``escalate_to == HUMAN`` ⇒ ``escalation = HUMAN``, ``status = BLOCKED``,
  attempts reset to 0 (the control graph hands off; no further model calls).
* Any other failure mode ⇒ ``status = FAILED``.

The handler never re-runs the gates: ``outcome.passed`` is the binding read,
exactly per CLAUDE.md ("NEVER advance a stage on a model's self-assessment").
"""

from __future__ import annotations

from chip_agent.agents.rtl_stage import RTLStageOutcome
from chip_agent.design_state import (
    ArtifactRef,
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

__all__ = ["apply_rtl_outcome"]


def apply_rtl_outcome(
    design: DesignState,
    outcome: RTLStageOutcome,
    *,
    module_id: str,
    store: SqliteArtifactStore,
) -> StageState:
    """Apply ``outcome`` to ``design.modules[module_id].stages[RTL]``.

    Returns the mutated :class:`StageState` for caller inspection.
    """
    ss = get_or_create_stage_state(design, Stage.RTL, module_id=module_id)

    _record_results(ss, outcome)

    if outcome.passed:
        promote_to_head(
            design, Stage.RTL, outcome.rtl_ref,
            store=store, module_id=module_id,
        )
        ss.last_failure = None
        return ss

    # Failed path: record the failing verification artifact, walk the
    # recommended escalation rung, and reset the per-level attempt counter.
    if outcome.last_failure is not None:
        ss.last_failure = outcome.last_failure

    if outcome.escalate_to is EscalationLevel.HUMAN:
        ss.escalation = EscalationLevel.HUMAN
        ss.attempts = 0
        ss.status = StageStatus.BLOCKED
    elif outcome.escalate_to is EscalationLevel.OUTER:
        ss.escalation = EscalationLevel.OUTER
        ss.attempts = 0
        ss.status = StageStatus.ESCALATED
    else:
        # No escalation recommendation but the gate didn't close — caller
        # will surface this as FAILED until the control graph chooses.
        ss.status = StageStatus.FAILED

    return ss


def _record_results(ss: StageState, outcome: RTLStageOutcome) -> None:
    """Append every verification artifact to ``ss.results`` idempotently."""
    refs: list[ArtifactRef] = []
    if outcome.lint is not None and outcome.lint.content_hash:
        refs.append(outcome.lint.ref())
    if outcome.elaborate is not None and outcome.elaborate.content_hash:
        refs.append(outcome.elaborate.ref())
    if outcome.sim is not None and outcome.sim.content_hash:
        refs.append(outcome.sim.ref())
    for ref in refs:
        if ref not in ss.results:
            ss.results.append(ref)
