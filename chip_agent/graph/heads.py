"""Read-only head resolvers for the LangGraph stage nodes (F9.1).

Each stage node needs to turn ``DesignState`` head refs into typed
artifacts via the store. Doing that inline in five different node
closures would scatter the same checks across five files; this module
centralises the lookup so each node body stays small and the error
shape is uniform.

Resolvers are pure — they raise :class:`MissingHeadError` when a ref
is missing rather than producing partial state. The driver code paths
already enforce the same invariants, so this is defence-in-depth.
"""

from __future__ import annotations

from typing import cast

from chip_agent.design_state import (
    ArtifactRef,
    AssertionSpec,
    ContractArtifact,
    DesignPlan,
    DesignState,
    LayoutArtifact,
    ModuleState,
    NetlistArtifact,
    OracleArtifact,
    RTLArtifact,
    Spec,
    Stage,
    StageState,
    StageStatus,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = [
    "MissingHeadError",
    "clear_module_stage_head",
    "load_assertion_spec_head",
    "load_contract_head",
    "load_layout_head",
    "load_netlist_head",
    "load_oracle_head",
    "load_plan",
    "load_rtl_head",
    "load_spec_head",
    "set_module_stage_head",
]


class MissingHeadError(LookupError):
    """A stage's head pointer is unset when the spine expected it.

    Usually means an upstream stage didn't promote (e.g. PHYSICAL ran
    before SYNTH closed). The control graph should have caught this via
    its gate predicates; raising here keeps the failure loud rather
    than producing partial state.
    """


def load_plan(design: DesignState, store: SqliteArtifactStore) -> DesignPlan:
    """Return the :class:`DesignPlan` head; raise if not set."""
    ref = design.plan
    if ref is None:
        raise MissingHeadError(
            f"design {design.design_id!r} has no plan head — was PLAN stage "
            f"completed?",
        )
    art = store.get(ref)
    assert isinstance(art, DesignPlan)
    return art


def load_rtl_head(
    design: DesignState, module_id: str, store: SqliteArtifactStore,
) -> RTLArtifact:
    """Return the RTL head for ``module_id``; raise if not promoted yet."""
    module = design.modules.get(module_id)
    if module is None:
        raise MissingHeadError(
            f"design {design.design_id!r} has no module {module_id!r}",
        )
    ss = module.stages.get(Stage.RTL)
    if ss is None or ss.head is None:
        raise MissingHeadError(
            f"module {module_id!r} in design {design.design_id!r} has no RTL "
            f"head — was RTL stage promoted?",
        )
    art = store.get(ss.head)
    assert isinstance(art, RTLArtifact)
    return art


def load_netlist_head(
    design: DesignState, store: SqliteArtifactStore,
) -> NetlistArtifact:
    """Return the SYNTH (design-level) netlist head; raise if not set."""
    ss = design.stages.get(Stage.SYNTH)
    if ss is None or ss.head is None:
        raise MissingHeadError(
            f"design {design.design_id!r} has no SYNTH head — was synth "
            f"stage promoted?",
        )
    art = store.get(ss.head)
    assert isinstance(art, NetlistArtifact)
    return art


def load_layout_head(
    design: DesignState, store: SqliteArtifactStore,
) -> LayoutArtifact:
    """Return the PHYSICAL (design-level) layout head; raise if not set."""
    ss = design.stages.get(Stage.PHYSICAL)
    if ss is None or ss.head is None:
        raise MissingHeadError(
            f"design {design.design_id!r} has no PHYSICAL head — was the "
            f"physical stage promoted?",
        )
    art = store.get(ss.head)
    assert isinstance(art, LayoutArtifact)
    return art


# --------------------------------------------------------------------------- #
# F19.7 — per-module M19 Phase 1 heads
# --------------------------------------------------------------------------- #
def load_spec_head(design: DesignState, store: SqliteArtifactStore) -> Spec:
    """Return the design-level :class:`Spec` head; raise if not set."""
    ref = design.spec
    if ref is None:
        raise MissingHeadError(
            f"design {design.design_id!r} has no spec head — was SPEC stage "
            f"completed?",
        )
    art = store.get(ref)
    assert isinstance(art, Spec)
    return art


def load_contract_head(
    design: DesignState, module_id: str, store: SqliteArtifactStore,
) -> ContractArtifact:
    """Return the F19.1 ContractArtifact head for ``module_id``."""
    return _load_module_artifact(
        design, module_id, Stage.CONTRACT, ContractArtifact, store,
    )


def load_oracle_head(
    design: DesignState, module_id: str, store: SqliteArtifactStore,
) -> OracleArtifact:
    """Return the F19.2 OracleArtifact head for ``module_id``."""
    return _load_module_artifact(
        design, module_id, Stage.ORACLE, OracleArtifact, store,
    )


def load_assertion_spec_head(
    design: DesignState, module_id: str, store: SqliteArtifactStore,
) -> AssertionSpec:
    """Return the F19.2 AssertionSpec head for ``module_id``."""
    return _load_module_artifact(
        design, module_id, Stage.ASSERTIONS, AssertionSpec, store,
    )


def _load_module_artifact[ArtifactT](
    design: DesignState,
    module_id: str,
    stage: Stage,
    expected_type: type[ArtifactT],
    store: SqliteArtifactStore,
) -> ArtifactT:
    """Generic per-module head loader. Mirrors :func:`load_rtl_head`."""
    module = design.modules.get(module_id)
    if module is None:
        raise MissingHeadError(
            f"design {design.design_id!r} has no module {module_id!r}",
        )
    ss = module.stages.get(stage)
    if ss is None or ss.head is None:
        raise MissingHeadError(
            f"module {module_id!r} in design {design.design_id!r} has no "
            f"{stage.value} head — was the {stage.value} stage promoted?",
        )
    art = store.get(ss.head)
    if not isinstance(art, expected_type):
        raise MissingHeadError(
            f"module {module_id!r} {stage.value} head points to "
            f"{type(art).__name__}, expected {expected_type.__name__}",
        )
    return cast(ArtifactT, art)


def set_module_stage_head(
    design: DesignState,
    module_id: str,
    stage: Stage,
    ref: ArtifactRef,
) -> None:
    """Promote ``ref`` to the head of ``stage`` for ``module_id``.

    F19.7 helper used by the new per-module stage nodes (CONTRACT,
    ORACLE, ASSERTIONS, ORACLE_VERIFICATION). The matching
    :class:`ModuleState` is created on demand if absent; the stage's
    :class:`StageState` is created on demand and marked ``PASSED``.
    Mirrors the per-module mutation pattern used by
    :func:`apply_rtl_outcome` (which lives in a sibling module).
    """
    module = design.modules.get(module_id)
    if module is None:
        module = ModuleState(module_id=module_id, name=module_id)
        design.modules[module_id] = module
    ss = module.stages.get(stage)
    if ss is None:
        ss = StageState(stage=stage)
        module.stages[stage] = ss
    ss.head = ref
    ss.attempts = max(ss.attempts, 1)
    ss.status = StageStatus.PASSED
    ss.results.append(ref)


def clear_module_stage_head(
    design: DesignState,
    module_id: str,
    stage: Stage,
) -> None:
    """Reset the per-module ``stage`` head + status to ``PENDING``.

    F19.9 helper. The reflection dispatcher calls this before
    re-entering an upstream node so the regeneration is a true rerun,
    not a cache-hit on the existing head. Mirrors
    :func:`set_module_stage_head` in reverse. Idempotent: clearing a
    stage that was never promoted (``ss is None``) is a no-op.
    """
    module = design.modules.get(module_id)
    if module is None:
        return
    ss = module.stages.get(stage)
    if ss is None:
        return
    ss.head = None
    ss.status = StageStatus.PENDING
    # The results history + attempt count stay for provenance — the
    # next promotion bumps ``attempts`` via :func:`set_module_stage_head`.
