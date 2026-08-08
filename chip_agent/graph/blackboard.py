"""Stage-state mutations on the blackboard (F1.4).

Small, focused primitives the control graph uses to:

* promote an artifact to the stage's head (and supersede the previous head),
* record a verification failure on a stage,
* bump the attempt counter and walk the escalation ladder
  (``INNER -> OUTER -> HUMAN``) when the budget is exhausted.

These helpers MUTATE the supplied :class:`DesignState` in place — that's the
LangGraph state object, not a content artifact. They do **not** decide
``advance / retry / escalate / human``; that's the deterministic gate
function (F5.2).
"""

from __future__ import annotations

from chip_agent.design_state import (
    ArtifactRef,
    ArtifactStatus,
    DesignState,
    EscalationLevel,
    Stage,
    StageState,
    StageStatus,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = [
    "BlackboardError",
    "bump_attempt",
    "escalate",
    "get_or_create_stage_state",
    "promote_to_head",
    "record_failure",
    "register_attempt_failure",
]


class BlackboardError(RuntimeError):
    """Invalid stage / module reference on the blackboard."""


def get_or_create_stage_state(
    design: DesignState,
    stage: Stage,
    *,
    module_id: str | None = None,
    max_attempts: int = 3,
    max_react_steps: int = 6,
) -> StageState:
    """Return the :class:`StageState` for ``(stage, module_id)``, creating defaults if absent."""
    container = _stage_container(design, stage, module_id)
    if stage not in container:
        container[stage] = StageState(
            stage=stage,
            max_attempts=max_attempts,
            max_react_steps=max_react_steps,
        )
    return container[stage]


def promote_to_head(
    design: DesignState,
    stage: Stage,
    new_ref: ArtifactRef,
    *,
    store: SqliteArtifactStore,
    module_id: str | None = None,
) -> StageState:
    """Set the stage's head to ``new_ref``.

    Updates lifecycle status in the store: the new head moves to ``ACCEPTED``;
    any prior head (other than ``new_ref`` itself) moves to ``SUPERSEDED``.
    Sets ``StageState.status`` to ``PASSED``.
    """
    ss = get_or_create_stage_state(design, stage, module_id=module_id)
    old = ss.head
    ss.head = new_ref
    ss.status = StageStatus.PASSED
    store.set_status(new_ref, ArtifactStatus.ACCEPTED)
    if old is not None and old != new_ref:
        store.set_status(old, ArtifactStatus.SUPERSEDED)
    return ss


def record_failure(
    design: DesignState,
    stage: Stage,
    verification_ref: ArtifactRef,
    *,
    module_id: str | None = None,
) -> StageState:
    """Attach a failing verification artifact to the stage state."""
    ss = get_or_create_stage_state(design, stage, module_id=module_id)
    ss.last_failure = verification_ref
    if verification_ref not in ss.results:
        ss.results.append(verification_ref)
    return ss


def bump_attempt(
    design: DesignState,
    stage: Stage,
    *,
    module_id: str | None = None,
) -> StageState:
    """Increment the attempt counter; mark the stage FAILED on budget exhaustion."""
    ss = get_or_create_stage_state(design, stage, module_id=module_id)
    ss.attempts += 1
    if ss.budget_left() == 0:
        ss.status = StageStatus.FAILED
    else:
        ss.status = StageStatus.IN_PROGRESS
    return ss


def escalate(
    design: DesignState,
    stage: Stage,
    *,
    module_id: str | None = None,
) -> StageState:
    """Walk one rung up the escalation ladder; reset the attempt counter."""
    ss = get_or_create_stage_state(design, stage, module_id=module_id)
    ss.escalation = ss.escalation.escalated()
    ss.attempts = 0
    ss.status = (
        StageStatus.BLOCKED
        if ss.escalation is EscalationLevel.HUMAN
        else StageStatus.ESCALATED
    )
    return ss


def register_attempt_failure(
    design: DesignState,
    stage: Stage,
    verification_ref: ArtifactRef,
    *,
    module_id: str | None = None,
) -> EscalationLevel:
    """Composite: record the failing verification, bump attempts, escalate when exhausted.

    Returns the escalation level after the call so callers can dispatch on it:
    ``INNER`` (still inside the inner loop), ``OUTER`` (just escalated past the
    syntactic budget), or ``HUMAN`` (semantic budget exhausted; control graph
    should hand off to a person).
    """
    record_failure(design, stage, verification_ref, module_id=module_id)
    ss = bump_attempt(design, stage, module_id=module_id)
    if ss.budget_left() == 0 and ss.escalation is not EscalationLevel.HUMAN:
        escalate(design, stage, module_id=module_id)
    return get_or_create_stage_state(design, stage, module_id=module_id).escalation


def _stage_container(
    design: DesignState,
    stage: Stage,
    module_id: str | None,
) -> dict[Stage, StageState]:
    """Resolve to ``DesignState.stages`` (design-level) or ``ModuleState.stages`` (per-module)."""
    if module_id is None:
        return design.stages
    if module_id not in design.modules:
        raise BlackboardError(
            f"unknown module_id {module_id!r} in design {design.design_id!r}"
        )
    return design.modules[module_id].stages
