"""GDSII stage gate (F9.1).

Terminal stage. The driver either produces a :class:`GDSIIArtifact` or
raises — there is no inner repair loop and no escalation. The handler
promotes the GDS ref to the design-level ``Stage.GDSII`` head and flips
the design ``status`` to ``COMPLETED``.

The control graph guarantees this node only fires **after** the F5.3
``await_human`` interrupt resolves, so "GDS emitted only after
approval" is the gate ordering, not a check inside the handler.
"""

from __future__ import annotations

from chip_agent.agents.gdsii_stage import GDSIIStageOutcome
from chip_agent.design_state import (
    DesignState,
    DesignStatus,
    Stage,
    StageState,
)
from chip_agent.graph.blackboard import promote_to_head
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = ["apply_gdsii_outcome"]


def apply_gdsii_outcome(
    design: DesignState,
    outcome: GDSIIStageOutcome,
    *,
    store: SqliteArtifactStore,
) -> StageState:
    """Apply ``outcome`` to ``design.stages[GDSII]`` and complete the design."""
    ss = promote_to_head(design, Stage.GDSII, outcome.gds_ref, store=store)
    ss.last_failure = None
    design.status = DesignStatus.COMPLETED
    return ss
