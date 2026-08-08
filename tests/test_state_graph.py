"""F5.1 acceptance: a run resumes from a checkpoint with identical DesignState."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chip_agent.design_state import (
    DesignConstraints,
    DesignState,
    DesignStatus,
    Stage,
)
from chip_agent.graph.state_graph import (
    STAGE_ORDER,
    build_design_graph,
    open_sqlite_checkpointer,
)


def _initial() -> DesignState:
    return DesignState(
        design_id="d0",
        name="counter",
        constraints=DesignConstraints(pdk="sky130A"),
    )


def _thread(name: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": name}}


def _values(state: object) -> DesignState:
    """LangGraph may return a dict or a Pydantic instance; normalise to DesignState."""
    if isinstance(state, DesignState):
        return state
    if isinstance(state, dict):
        return DesignState.model_validate(state)
    raise TypeError(f"unexpected state type: {type(state).__name__}")


# --------------------------------------------------------------------------- #
# Graph wiring
# --------------------------------------------------------------------------- #
def test_stage_order_matches_macro_pipeline() -> None:
    assert STAGE_ORDER == [
        Stage.SPEC, Stage.PLAN,
        # F19.7: M19 Phase 1 stages between PLAN and RTL.
        Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS,
        Stage.ORACLE_VERIFICATION,
        Stage.RTL,
        Stage.SYNTH, Stage.PHYSICAL, Stage.SIGNOFF, Stage.GDSII,
    ]


def test_invoke_walks_to_terminal_stage(tmp_path: Path) -> None:
    # F5.3 inserts a pre-GDSII pause; reaching the terminal stage requires a
    # resume call. The first invoke halts at AWAITING_HUMAN; the second
    # (invoke(None, config)) finishes the run.
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver)
        graph.invoke(_initial(), _thread("run-1"))
        final = graph.invoke(None, _thread("run-1"))

    state = _values(final)
    assert state.current_stage is Stage.GDSII
    assert state.status is DesignStatus.COMPLETED
    # Initial fields preserved end-to-end.
    assert state.design_id == "d0"
    assert state.name == "counter"
    assert state.constraints.pdk == "sky130A"


# --------------------------------------------------------------------------- #
# AC: resume from checkpoint yields identical state.
# --------------------------------------------------------------------------- #
def test_resume_from_disk_checkpoint_is_identical(tmp_path: Path) -> None:
    ckpt_path = tmp_path / "ckpt.sqlite"
    config = _thread("run-1")

    # First process: run + persist. The F5.3 interrupt halts before GDSII;
    # `invoke` returns the state captured at the pause point.
    with open_sqlite_checkpointer(ckpt_path) as saver:
        graph_a = build_design_graph(checkpointer=saver)
        first_final = _values(graph_a.invoke(_initial(), config))

    # Second process: fresh saver + graph, load via get_state.
    with open_sqlite_checkpointer(ckpt_path) as saver:
        graph_b = build_design_graph(checkpointer=saver)
        snapshot = graph_b.get_state(config)
        resumed = _values(snapshot.values)

    # The AC: identical DesignState.
    assert resumed == first_final
    assert resumed.model_dump(mode="json") == first_final.model_dump(mode="json")
    assert resumed.status is DesignStatus.AWAITING_HUMAN


def test_checkpoint_visible_to_a_fresh_saver_instance(tmp_path: Path) -> None:
    # Even without keeping the saver alive between invoke and get_state, the
    # on-disk SQLite database must carry the checkpoint forward.
    ckpt_path = tmp_path / "ckpt.sqlite"
    config = _thread("run-2")

    with open_sqlite_checkpointer(ckpt_path) as saver:
        graph = build_design_graph(checkpointer=saver)
        graph.invoke(_initial(), config)
        graph.invoke(None, config)  # resume past the pre-GDSII pause
    # First saver is closed here.

    with open_sqlite_checkpointer(ckpt_path) as saver:
        snapshot = build_design_graph(checkpointer=saver).get_state(config)
    assert snapshot.values is not None
    state = _values(snapshot.values)
    assert state.current_stage is Stage.GDSII
    assert state.design_id == "d0"


def test_state_history_records_every_stage_transition(tmp_path: Path) -> None:
    ckpt_path = tmp_path / "ckpt.sqlite"
    config = _thread("run-3")
    with open_sqlite_checkpointer(ckpt_path) as saver:
        graph = build_design_graph(checkpointer=saver)
        graph.invoke(_initial(), config)
        graph.invoke(None, config)  # resume past the pre-GDSII pause
        history = list(graph.get_state_history(config))

    # The history exists and is non-empty; every macro stage shows up at
    # least once in the recorded current_stage values.
    assert history
    stages_seen = {_values(h.values).current_stage for h in history if h.values}
    for stage in STAGE_ORDER:
        assert stage in stages_seen, f"missing stage {stage.value!r} in history"


# --------------------------------------------------------------------------- #
# Different threads keep independent state.
# --------------------------------------------------------------------------- #
def test_threads_are_isolated(tmp_path: Path) -> None:
    ckpt_path = tmp_path / "ckpt.sqlite"
    with open_sqlite_checkpointer(ckpt_path) as saver:
        graph = build_design_graph(checkpointer=saver)
        graph.invoke(
            DesignState(design_id="alpha", name="alpha"),
            _thread("alpha-run"),
        )
        graph.invoke(
            DesignState(design_id="beta", name="beta"),
            _thread("beta-run"),
        )

    with open_sqlite_checkpointer(ckpt_path) as saver:
        graph = build_design_graph(checkpointer=saver)
        alpha = _values(graph.get_state(_thread("alpha-run")).values)
        beta = _values(graph.get_state(_thread("beta-run")).values)
    assert alpha.design_id == "alpha"
    assert beta.design_id == "beta"


# --------------------------------------------------------------------------- #
# Without a checkpointer, the graph still runs but doesn't persist.
# --------------------------------------------------------------------------- #
def test_runs_without_checkpointer() -> None:
    # Without a checkpointer the F5.3 interrupt still halts the invoke at
    # await_human; the run can be observed there but not resumed because no
    # state was persisted to load.
    graph = build_design_graph()
    final = _values(graph.invoke(_initial(), _thread("ephemeral")))
    assert final.current_stage is Stage.SIGNOFF
    assert final.status is DesignStatus.AWAITING_HUMAN
    # No checkpointer -> get_state should fail or return empty.
    with pytest.raises(Exception):
        graph.get_state(_thread("ephemeral"))
