"""F5.3 acceptance: graph halts with ``AWAITING_HUMAN``; resume continues
from the same state.

Two pause points are exercised:

* **Pre-GDSII pause** — the linear macro spine routes through the public
  :data:`HUMAN_REVIEW_NODE` before ``gdsii_emit``. A single ``invoke`` halts
  there; ``invoke(None, config)`` resumes and completes.
* **HUMAN-escalation seam** — :func:`request_human_review` returns the
  update dict any future stage handler will merge with a ``Command(goto=…)``
  to reach the same node. F5.3 verifies the seam exists and yields the
  correct status; F5.4 wires real conditional edges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chip_agent.design_state import (
    DesignState,
    DesignStatus,
    Stage,
)
from chip_agent.graph.state_graph import (
    HUMAN_REVIEW_NODE,
    build_design_graph,
    open_sqlite_checkpointer,
    request_human_review,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _initial() -> DesignState:
    return DesignState(design_id="d0", name="counter")


def _thread(name: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": name}}


def _values(state: object) -> DesignState:
    if isinstance(state, DesignState):
        return state
    if isinstance(state, dict):
        return DesignState.model_validate(state)
    raise TypeError(f"unexpected state type: {type(state).__name__}")


# --------------------------------------------------------------------------- #
# Pre-GDSII pause
# --------------------------------------------------------------------------- #
def test_graph_halts_before_gdsii_with_awaiting_human(tmp_path: Path) -> None:
    config = _thread("pre-gdsii")
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver)
        first = _values(graph.invoke(_initial(), config))

    # Halt point: graph has run through the await_human node (status flips to
    # AWAITING_HUMAN) but NOT through gdsii_emit (current_stage still SIGNOFF).
    assert first.status is DesignStatus.AWAITING_HUMAN
    assert first.current_stage is Stage.SIGNOFF


def test_resume_continues_to_gdsii_completed(tmp_path: Path) -> None:
    config = _thread("resume-once")
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver)
        graph.invoke(_initial(), config)
        final = _values(graph.invoke(None, config))

    assert final.status is DesignStatus.COMPLETED
    assert final.current_stage is Stage.GDSII


def test_resumed_state_carries_over_initial_fields(tmp_path: Path) -> None:
    config = _thread("resume-fields")
    initial = DesignState(design_id="d-XYZ", name="my_counter")
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver)
        graph.invoke(initial, config)
        final = _values(graph.invoke(None, config))

    # The same DesignState resumes — no fields lost across the pause.
    assert final.design_id == "d-XYZ"
    assert final.name == "my_counter"


# --------------------------------------------------------------------------- #
# Resume identity (the F5.3 AC: same state, no drift)
# --------------------------------------------------------------------------- #
def test_state_at_pause_persists_across_saver_instances(tmp_path: Path) -> None:
    """Halting state must round-trip cleanly through SQLite — the F5.1
    checkpoint property holds at the F5.3 pause too."""
    ckpt = tmp_path / "ckpt.sqlite"
    config = _thread("cross-process")

    with open_sqlite_checkpointer(ckpt) as saver:
        graph = build_design_graph(checkpointer=saver)
        halted = _values(graph.invoke(_initial(), config))

    # Reopen with a fresh saver — the persisted state is byte-identical.
    with open_sqlite_checkpointer(ckpt) as saver:
        graph = build_design_graph(checkpointer=saver)
        loaded = _values(graph.get_state(config).values)

    assert loaded == halted
    assert loaded.model_dump(mode="json") == halted.model_dump(mode="json")
    assert loaded.status is DesignStatus.AWAITING_HUMAN


def test_resume_from_disk_completes_run(tmp_path: Path) -> None:
    """Halt in one saver lifetime, resume in another — same outcome as a
    single in-process resume."""
    ckpt = tmp_path / "ckpt.sqlite"
    config = _thread("cross-process-resume")

    with open_sqlite_checkpointer(ckpt) as saver:
        build_design_graph(checkpointer=saver).invoke(_initial(), config)

    with open_sqlite_checkpointer(ckpt) as saver:
        graph = build_design_graph(checkpointer=saver)
        final = _values(graph.invoke(None, config))

    assert final.status is DesignStatus.COMPLETED
    assert final.current_stage is Stage.GDSII


# --------------------------------------------------------------------------- #
# Pause point appears in the recorded history
# --------------------------------------------------------------------------- #
def test_pause_recorded_in_state_history(tmp_path: Path) -> None:
    config = _thread("history")
    ckpt = tmp_path / "ckpt.sqlite"
    with open_sqlite_checkpointer(ckpt) as saver:
        graph = build_design_graph(checkpointer=saver)
        graph.invoke(_initial(), config)
        history_at_pause = list(graph.get_state_history(config))

    statuses = {_values(h.values).status for h in history_at_pause if h.values}
    assert DesignStatus.AWAITING_HUMAN in statuses


# --------------------------------------------------------------------------- #
# Without a checkpointer, the interrupt is a no-op
# --------------------------------------------------------------------------- #
def test_interrupt_halts_even_without_checkpointer() -> None:
    # The interrupt is a compile-time property of the graph; it halts the
    # invoke even when no checkpointer is wired. Resuming the run is what
    # requires a checkpointer (no checkpointer == no state to load).
    graph = build_design_graph()
    final = _values(graph.invoke(_initial(), _thread("ephemeral")))
    assert final.status is DesignStatus.AWAITING_HUMAN
    assert final.current_stage is Stage.SIGNOFF


# --------------------------------------------------------------------------- #
# HUMAN-escalation seam
# --------------------------------------------------------------------------- #
def test_request_human_review_returns_status_update() -> None:
    # The seam is a small public helper that any future stage handler will
    # merge into its Command(goto=HUMAN_REVIEW_NODE, update=…).
    update = request_human_review()
    assert update == {"status": DesignStatus.AWAITING_HUMAN}


def test_human_review_node_name_is_exported() -> None:
    # Conditional edges in F5.4 reference this string by name; locking it in
    # here prevents an accidental rename from silently breaking routing.
    assert HUMAN_REVIEW_NODE == "await_human"
