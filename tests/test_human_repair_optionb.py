"""F23.5 Option B: interactive-repair interrupt/resume node mechanics.

Exercises the checkpoint-friendly path (used by the TUI + `resume --hint`)
at the node level, without standing up a full graph run:

* ``_open_human_repair_pause`` parks a ``PendingHumanRepair`` + routes to
  the interrupt node — only when a distiller is wired *without* a blocking
  provider (Option A owns that case) and a turn is left.
* ``_route_after_human_repair`` applies the hint iff a transcript was
  injected, else ends the run (an RTL escalation never falls through to
  gdsii).
* ``_make_human_repair_apply`` distils the injected transcript, consumes a
  bounded turn, clears the head, and re-enters RTL — or ends blocked on
  decline / spent budget.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.graph import END

from chip_agent.agents.human_hint_distill import HumanHintDistillAgent
from chip_agent.design_state import (
    DesignPlan,
    DesignState,
    DesignStatus,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    ModuleDecl,
    ModuleState,
    PendingHumanRepair,
    Provenance,
    Stage,
    TaskType,
)
from chip_agent.graph.blackboard import get_or_create_stage_state
from chip_agent.graph.stage_context import StageContext
from chip_agent.graph.state_graph import (
    _NODE_NAMES,
    HUMAN_REPAIR_APPLY_NODE,
    HUMAN_REPAIR_NODE,
    _make_human_repair_apply,
    _open_human_repair_pause,
    _route_after_human_repair,
)
from chip_agent.store import SqliteArtifactStore


@dataclass
class StubRouter:
    chosen: str
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic", model="claude-sonnet-4-6", temperature=0.0,
        ),
    )

    def generate(
        self, task: TaskType, *, context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        return GenerationResult(
            candidates=[self.chosen], chosen=self.chosen,
            invocation=self.invocation,
        )


_DISTILL_JSON = (
    '{"hint_kind": "point_at_bug", '
    '"summary": "Add the final addRoundKey after round 31.", '
    '"suggested_route": "regen_current_rtl"}'
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteArtifactStore]:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite", content_dir=tmp_path / "content",
    )
    yield s
    s.close()


def _state_with_plan(store: SqliteArtifactStore) -> DesignState:
    st = DesignState(design_id="d0", name="present80")
    st.modules["m"] = ModuleState(module_id="m", name="present80")
    plan = DesignPlan(
        artifact_id="d0.plan", design_id="d0", top_module_id="m",
        modules=[ModuleDecl(module_id="m", name="present80", description="cipher")],
        provenance=Provenance(produced_by=Stage.PLAN, agent="planner"),
    )
    st.plan = store.put(plan)
    return st


def _diagnosis() -> FailureDiagnosis:
    return FailureDiagnosis(
        artifact_id="d0.m.diagnosis", design_id="d0", module_id="m",
        nl_summary="ciphertext wrong", failing_signal="ciphertext",
        provenance=Provenance(produced_by=Stage.RTL, agent="rtl_stage"),
    )


def _ctx(store: SqliteArtifactStore, *, blocking: bool = False) -> StageContext:
    return StageContext(
        store=store,
        human_hint_distiller=HumanHintDistillAgent(
            router=StubRouter(chosen=_DISTILL_JSON), design_id="d0",
        ),
        human_transcript_for=(lambda _s, _m, _d: "x") if blocking else None,
    )


# --------------------------------------------------------------------------- #
# _open_human_repair_pause
# --------------------------------------------------------------------------- #
def test_pause_parks_request_and_routes_to_interrupt(store: SqliteArtifactStore) -> None:
    state = _state_with_plan(store)
    cmd = _open_human_repair_pause(
        _ctx(store), state, "m", SimpleNamespace(diagnosis=_diagnosis()),
    )
    assert cmd is not None
    assert cmd.goto == HUMAN_REPAIR_NODE
    pending = cmd.update["pending_human_repair"]
    assert isinstance(pending, PendingHumanRepair)
    assert pending.module_id == "m"
    assert pending.transcript is None                 # filled later by resume
    assert pending.diagnosis_ref is not None          # persisted for reload
    assert cmd.update["status"] is DesignStatus.AWAITING_HUMAN


def test_pause_declines_when_blocking_provider_present(store: SqliteArtifactStore) -> None:
    # Option A owns the blocking case; Option B stays out of it.
    state = _state_with_plan(store)
    assert _open_human_repair_pause(
        _ctx(store, blocking=True), state, "m",
        SimpleNamespace(diagnosis=_diagnosis()),
    ) is None


# --------------------------------------------------------------------------- #
# _route_after_human_repair
# --------------------------------------------------------------------------- #
def test_route_applies_only_with_transcript() -> None:
    st = DesignState(design_id="d0", name="present80")
    st.pending_human_repair = PendingHumanRepair(module_id="m", stage=Stage.RTL)
    assert _route_after_human_repair(st) == END                 # no transcript
    st.pending_human_repair.transcript = "fix the last round"
    assert _route_after_human_repair(st) == HUMAN_REPAIR_APPLY_NODE
    st.pending_human_repair = None
    assert _route_after_human_repair(st) == END


# --------------------------------------------------------------------------- #
# _make_human_repair_apply
# --------------------------------------------------------------------------- #
def test_apply_distils_and_reenters_rtl(store: SqliteArtifactStore) -> None:
    state = _state_with_plan(store)
    diag_ref = store.put(_diagnosis())
    state.pending_human_repair = PendingHumanRepair(
        module_id="m", stage=Stage.RTL, diagnosis_ref=diag_ref,
        transcript="you forgot the final addRoundKey",
    )
    apply = _make_human_repair_apply(_ctx(store))

    cmd = apply(state)

    assert cmd.goto == _NODE_NAMES[Stage.RTL]
    assert cmd.update["pending_human_repair"] is None       # request cleared
    assert cmd.update["status"] is DesignStatus.RUNNING     # not stuck at human gate
    # A hint was persisted; a bounded turn consumed.
    hint = store.get_by_id("d0.m.hint")
    assert "addRoundKey" in hint.summary
    ss = get_or_create_stage_state(state, Stage.RTL, module_id="m")
    assert ss.human_turns_used == 1


def test_apply_ends_blocked_on_decline(store: SqliteArtifactStore) -> None:
    state = _state_with_plan(store)
    state.pending_human_repair = PendingHumanRepair(
        module_id="m", stage=Stage.RTL, transcript=None,
    )
    cmd = _make_human_repair_apply(_ctx(store))(state)
    assert cmd.goto == END
    assert cmd.update["pending_human_repair"] is None


def test_apply_ends_blocked_when_turns_spent(store: SqliteArtifactStore) -> None:
    state = _state_with_plan(store)
    diag_ref = store.put(_diagnosis())
    ss = get_or_create_stage_state(state, Stage.RTL, module_id="m")
    ss.human_turns_used = ss.max_human_turns          # exhausted
    state.pending_human_repair = PendingHumanRepair(
        module_id="m", stage=Stage.RTL, diagnosis_ref=diag_ref,
        transcript="try again",
    )
    cmd = _make_human_repair_apply(_ctx(store))(state)
    assert cmd.goto == END
