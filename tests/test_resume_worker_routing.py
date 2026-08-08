"""F23.5: the resume worker routes a re-paused/blocked outcome as a pause.

Only a COMPLETED resume is a ``PipelineCompleted``; an interactive-repair
resume that re-pauses (or ends blocked) routes back as ``PipelinePaused``
so the app re-opens the repair modal instead of declaring success.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

from chip_agent.cli import ResumeOutcome, RunArgs
from chip_agent.design_state import (
    DesignState,
    DesignStatus,
    PendingHumanRepair,
    Stage,
)
from chip_agent.tui.messages import PipelineCompleted, PipelinePaused
from chip_agent.tui.workers.run_worker import resume_pipeline


class _FakeApp:
    def call_from_thread(self, fn: Any, *args: Any) -> None:
        fn(*args)


class _FakePane:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def post_message(self, message: Any) -> None:
        self.messages.append(message)


def _args(tmp_path: Path) -> RunArgs:
    return RunArgs(
        cmd="resume", spec_path=None, name="present80", run_dir=tmp_path,
        design_id="d0", hmac_key=b"k",
    )


def _drive(tmp_path: Path, final: DesignState) -> Any:
    pane = _FakePane()
    outcome = ResumeOutcome(
        design_id="d0", final_state=final, exports_dir=tmp_path / "exports",
    )
    with (
        mock.patch(
            "chip_agent.tui.workers.run_worker._attach_jsonl_tracer",
            side_effect=lambda a: a,
        ),
        mock.patch(
            "chip_agent.tui.workers.run_worker.cmd_resume",
            return_value=outcome,
        ),
    ):
        resume_pipeline(app=_FakeApp(), pane=pane, args=_args(tmp_path))
    return pane.messages[-1]


def test_repaused_resume_routes_to_pipeline_paused(tmp_path: Path) -> None:
    final = DesignState(
        design_id="d0", name="present80",
        status=DesignStatus.AWAITING_HUMAN, current_stage=Stage.RTL,
        pending_human_repair=PendingHumanRepair(module_id="m", stage=Stage.RTL),
    )
    assert isinstance(_drive(tmp_path, final), PipelinePaused)


def test_completed_resume_routes_to_pipeline_completed(tmp_path: Path) -> None:
    final = DesignState(
        design_id="d0", name="present80",
        status=DesignStatus.COMPLETED, current_stage=Stage.GDSII,
    )
    assert isinstance(_drive(tmp_path, final), PipelineCompleted)
