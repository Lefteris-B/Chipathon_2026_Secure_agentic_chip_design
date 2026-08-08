"""PHYSICAL stage gate (F9.1).

Translates a :class:`PhysicalStageOutcome` into deterministic blackboard
mutations. The driver emits a ``LayoutArtifact`` even on failure (the
last attempted layout for diagnostic value), so the handler only
promotes when ``outcome.passed`` is true.

:class:`~chip_agent.tools.librelane.PhysicalRun` is a transient
dataclass (not an :class:`~chip_agent.design_state.Artifact`), so there
is no ref to record in ``ss.results`` on failure — the diagnostic lives
in the outcome's ``runs`` list and the tracer span. F9.x can later
synthesise a typed ``PhysicalReport`` if a cross-stage feedback edge
needs it.
"""

from __future__ import annotations

from chip_agent.agents.physical_stage import PhysicalStageOutcome
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

__all__ = ["apply_physical_outcome"]


def apply_physical_outcome(
    design: DesignState,
    outcome: PhysicalStageOutcome,
    *,
    store: SqliteArtifactStore,
) -> StageState:
    """Apply ``outcome`` to ``design.stages[PHYSICAL]``."""
    ss = get_or_create_stage_state(design, Stage.PHYSICAL)

    if outcome.passed:
        assert outcome.layout_ref is not None
        promote_to_head(design, Stage.PHYSICAL, outcome.layout_ref, store=store)
        ss.last_failure = None
        return ss

    # Failure path: the driver already escalated INNER -> OUTER if it
    # tried to retune and exhausted its budget; we just transcribe.
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
