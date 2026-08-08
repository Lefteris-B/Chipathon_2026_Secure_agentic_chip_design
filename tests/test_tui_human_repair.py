"""F23.5 Option B TUI surface: the interactive-repair modal.

Pins:

* ``format_diagnosis`` renders the diagnosis fields (and tolerates None).
* ``HumanRepairScreen`` dismisses with the entered text on send, ``None``
  on skip / blank.
* ``on_pipeline_paused`` with a ``pending_human_repair`` request opens the
  modal (instead of the pre-GDSII approve path).
* ``_on_repair_hint`` resumes with the hint attached (``cmd_resume --hint``).

Mounts :class:`ChipAgentApp` via Textual's :meth:`App.run_test` and drives
the handlers directly — no real spine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from chip_agent.cli import RunArgs
from chip_agent.design_state import (
    DesignConstraints,
    DesignState,
    DesignStatus,
    FailureDiagnosis,
    PendingHumanRepair,
    Provenance,
    Stage,
)
from chip_agent.settings import Settings
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tui.app import ChipAgentApp
from chip_agent.tui.messages import PipelinePaused
from chip_agent.tui.panes.human_repair import HumanRepairScreen, format_diagnosis
from tests._routing_stub import (
    CHAT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

HMAC_KEY = b"f23-tui-tests-hmac-key"


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


def _diagnosis() -> FailureDiagnosis:
    return FailureDiagnosis(
        artifact_id="orig.m.diagnosis", design_id="orig", module_id="m",
        nl_summary="ciphertext wrong for the all-zero vector",
        failing_signal="ciphertext", cycle=240,
        expected="5579c1387b228445", actual="38d2f04c34635345",
        provenance=Provenance(produced_by=Stage.RTL, agent="rtl_stage"),
    )


def _paused_repair_state(store: SqliteArtifactStore) -> DesignState:
    diag_ref = store.put(_diagnosis())
    return DesignState(
        design_id="orig", name="present80", constraints=DesignConstraints(),
        status=DesignStatus.AWAITING_HUMAN, current_stage=Stage.RTL,
        pending_human_repair=PendingHumanRepair(
            module_id="m", stage=Stage.RTL, diagnosis_ref=diag_ref,
        ),
    )


def _build_app(
    *, run_dir: Path, routing_config: Path,
) -> tuple[ChipAgentApp, SqliteArtifactStore, dict[str, Any]]:
    backend = StubBackend(matchers=CHAT_RESPONSES)
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    run_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    )
    settings = Settings.from_yaml(routing_config)
    captured: dict[str, Any] = {"factory_calls": []}

    def _factory(cmd: str, current_design_id: str) -> RunArgs:
        captured["factory_calls"].append((cmd, current_design_id))
        return RunArgs(
            cmd=cmd, spec_path=None, name="present80", run_dir=run_dir,
            design_id=current_design_id, hmac_key=HMAC_KEY,
            config_path=routing_config,
        )

    app = ChipAgentApp(
        router=router, store=store, design_id="orig", name="present80",
        transcript_path=run_dir / "chat.md",
        checkpoint_path=run_dir / "checkpoint.sqlite",
        audit_db_path=run_dir / "audit.sqlite", hmac_key=HMAC_KEY,
        exports_dir=run_dir / "exports" / "orig",
        run_args_factory=_factory, defaults=settings.constraints,
        routing=settings.routing,
    )
    return app, store, captured


def _run(
    drive: Callable[[ChipAgentApp, SqliteArtifactStore, object], Awaitable[dict[str, Any]]],
    *, run_dir: Path, routing_config: Path,
) -> dict[str, Any]:
    app, store, captured = _build_app(run_dir=run_dir, routing_config=routing_config)

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            out = await drive(app, store, pilot)
            out["captured"] = captured
            return out

    return _arun(_go())


# --------------------------------------------------------------------------- #
# format_diagnosis (pure)
# --------------------------------------------------------------------------- #
def test_format_diagnosis_renders_fields() -> None:
    body = format_diagnosis(_diagnosis())
    assert "ciphertext wrong for the all-zero vector" in body
    assert "38d2f04c34635345" in body
    assert "5579c1387b228445" in body


def test_format_diagnosis_tolerates_none() -> None:
    assert "describe what you think is wrong" in format_diagnosis(None)


# --------------------------------------------------------------------------- #
# Modal dismiss round-trip
# --------------------------------------------------------------------------- #
def test_modal_sends_entered_text(tmp_path: Path, routing_config: Path) -> None:
    async def drive(app: ChipAgentApp, store: SqliteArtifactStore, pilot: object) -> dict[str, Any]:
        result: dict[str, Any] = {"value": "UNSET"}
        app.push_screen(
            HumanRepairScreen(module_id="m", body="diag"),
            lambda v: result.__setitem__("value", v),
        )
        await pilot.pause()  # type: ignore[attr-defined]
        modal = app.screen_stack[-1]
        from textual.widgets import TextArea
        modal.query_one(TextArea).text = "add the final addRoundKey"
        modal.action_send()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"value": result["value"]}

    out = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert out["value"] == "add the final addRoundKey"


def test_modal_skip_dismisses_none(tmp_path: Path, routing_config: Path) -> None:
    async def drive(app: ChipAgentApp, store: SqliteArtifactStore, pilot: object) -> dict[str, Any]:
        result: dict[str, Any] = {"value": "UNSET"}
        app.push_screen(
            HumanRepairScreen(module_id="m", body="diag"),
            lambda v: result.__setitem__("value", v),
        )
        await pilot.pause()  # type: ignore[attr-defined]
        app.screen_stack[-1].action_skip()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"value": result["value"]}

    out = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert out["value"] is None


# --------------------------------------------------------------------------- #
# Pause -> modal, and resume-with-hint
# --------------------------------------------------------------------------- #
def test_pending_repair_pause_opens_modal(tmp_path: Path, routing_config: Path) -> None:
    async def drive(app: ChipAgentApp, store: SqliteArtifactStore, pilot: object) -> dict[str, Any]:
        app.on_pipeline_paused(PipelinePaused(state=_paused_repair_state(store)))
        await pilot.pause()  # type: ignore[attr-defined]
        return {"top": type(app.screen_stack[-1]).__name__}

    out = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert out["top"] == "HumanRepairScreen"


def test_on_repair_hint_resumes_with_hint(tmp_path: Path, routing_config: Path) -> None:
    async def drive(app: ChipAgentApp, store: SqliteArtifactStore, pilot: object) -> dict[str, Any]:
        app._paused_state = _paused_repair_state(store)
        with mock.patch("chip_agent.tui.app.resume_pipeline") as resume_mock:
            app._on_repair_hint("hold load_en high for the full load")
            for _ in range(6):
                await pilot.pause()  # type: ignore[attr-defined]
            called = resume_mock.called
            hint = resume_mock.call_args.kwargs["args"].hint if called else None
        return {"called": called, "hint": hint}

    out = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert out["called"] is True
    assert out["hint"] == "hold load_en high for the full load"
    # The resume factory was consulted.
    assert ("resume", "orig") in out["captured"]["factory_calls"]


def test_on_repair_hint_skip_does_not_resume(tmp_path: Path, routing_config: Path) -> None:
    async def drive(app: ChipAgentApp, store: SqliteArtifactStore, pilot: object) -> dict[str, Any]:
        with mock.patch("chip_agent.tui.app.resume_pipeline") as resume_mock:
            app._on_repair_hint(None)
            await pilot.pause()  # type: ignore[attr-defined]
            return {"called": resume_mock.called}

    out = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert out["called"] is False
