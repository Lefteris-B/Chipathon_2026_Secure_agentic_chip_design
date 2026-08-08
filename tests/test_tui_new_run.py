"""Ctrl+N new-run acceptance tests.

Pins:

* ``Ctrl+N`` pushes the confirm modal; confirming mints a fresh
  design_id and resets the app's in-memory spec / paused / final
  snapshots.
* ``Ctrl+N`` is a friendly no-op while a worker is in flight (we
  don't want to orphan the worker by remounting its target pane).
* ``Ctrl+N`` is a friendly no-op when ``mint_design_id`` isn't wired
  (test harnesses + future entry points that opt out).
* The confirm modal returns ``True`` on Enter / button + ``False`` on
  Esc / cancel button.
* After reset, the panes target the new design_id (PipelinePane,
  AuditPane), and the per-design path callables are consulted.

Tests mount :class:`ChipAgentApp` via Textual's :meth:`App.run_test`
and drive the keybind + modal interactions directly. No real spine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from chip_agent.cli import RunArgs
from chip_agent.design_state import (
    DesignConstraints,
    DesignState,
    DesignStatus,
    Provenance,
    Spec,
    Stage,
)
from chip_agent.settings import Settings
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tui.app import ChipAgentApp
from chip_agent.tui.panes.audit import AuditPane
from chip_agent.tui.panes.chat import ChatPane
from chip_agent.tui.panes.exports import ExportsPane
from chip_agent.tui.panes.new_run import NewRunConfirmScreen
from chip_agent.tui.panes.pipeline import PipelinePane
from tests._routing_stub import (
    CHAT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

HMAC_KEY = b"ctrl-n-tests-hmac-key"


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


def _spec(*, design_id: str = "orig") -> Spec:
    return Spec(
        artifact_id=f"{design_id}.spec", design_id=design_id,
        raw_text="raw", normalized="norm",
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


def _paused_state(*, design_id: str = "orig") -> DesignState:
    return DesignState(
        design_id=design_id, name=design_id,
        constraints=DesignConstraints(),
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.SIGNOFF,
    )


def _build_app(
    *,
    run_dir: Path,
    routing_config: Path,
    design_id: str = "orig",
    minted: list[str] | None = None,
    with_mint: bool = True,
) -> tuple[ChipAgentApp, SqliteArtifactStore, dict[str, Any]]:
    backend = StubBackend(matchers=CHAT_RESPONSES)
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    run_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteArtifactStore(
        db_path=run_dir / "store.sqlite",
        content_dir=run_dir / "content",
    )
    settings = Settings.from_yaml(routing_config)

    captured: dict[str, Any] = {"factory_calls": []}

    def _factory(cmd: str, current_design_id: str) -> RunArgs:
        captured["factory_calls"].append((cmd, current_design_id))
        return RunArgs(
            cmd=cmd, spec_path=None, name="counter",
            run_dir=run_dir, design_id=current_design_id,
            hmac_key=HMAC_KEY, config_path=routing_config,
        )

    minted = minted if minted is not None else ["new-1", "new-2", "new-3"]
    counter = {"i": -1}

    def _mint_id() -> str:
        counter["i"] += 1
        return minted[counter["i"]]

    def _transcript_for(did: str) -> Path:
        return run_dir / f"chat.transcript.{did}.md"

    def _exports_for(did: str) -> Path:
        return run_dir / "exports" / did

    app = ChipAgentApp(
        router=router,
        store=store,
        design_id=design_id,
        name="counter",
        transcript_path=_transcript_for(design_id),
        checkpoint_path=run_dir / "checkpoint.sqlite",
        audit_db_path=run_dir / "audit.sqlite",
        hmac_key=HMAC_KEY,
        exports_dir=_exports_for(design_id),
        run_args_factory=_factory,
        defaults=settings.constraints,
        routing=settings.routing,
        mint_design_id=_mint_id if with_mint else None,
        transcript_path_fn=_transcript_for if with_mint else None,
        exports_dir_fn=_exports_for if with_mint else None,
    )
    return app, store, captured


def _run(
    drive: Callable[[ChipAgentApp, object], Awaitable[dict[str, Any]]],
    *,
    run_dir: Path,
    routing_config: Path,
    with_mint: bool = True,
) -> dict[str, Any]:
    app, _store, captured = _build_app(
        run_dir=run_dir, routing_config=routing_config, with_mint=with_mint,
    )

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    out = _arun(_go())
    out["captured"] = captured
    return out


# --------------------------------------------------------------------------- #
# Modal behaviour — pure pushScreen / dismiss round-trip.
# --------------------------------------------------------------------------- #
def test_ctrl_n_pushes_confirm_modal(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Pressing Ctrl+N should land a NewRunConfirmScreen on the screen stack."""

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        await pilot.press("ctrl+n")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        screen = app.screen_stack[-1]
        return {"top_screen": type(screen).__name__}

    captured = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert captured["top_screen"] == "NewRunConfirmScreen"


def test_ctrl_n_noop_when_mint_design_id_unwired(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Without mint_design_id, Ctrl+N just notifies — no modal pushes."""

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        await pilot.press("ctrl+n")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        screen = app.screen_stack[-1]
        return {"top_screen": type(screen).__name__}

    captured = _run(
        drive, run_dir=tmp_path, routing_config=routing_config,
        with_mint=False,
    )
    # No modal — we're still on the main screen.
    assert captured["top_screen"] != "NewRunConfirmScreen"


def test_ctrl_n_refused_while_driving(
    tmp_path: Path, routing_config: Path,
) -> None:
    """While a worker is in flight, Ctrl+N notifies + doesn't push."""

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app._driving = True
        await pilot.press("ctrl+n")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"top_screen": type(app.screen_stack[-1]).__name__}

    captured = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert captured["top_screen"] != "NewRunConfirmScreen"


def test_confirm_modal_dismisses_with_true_on_action(
    tmp_path: Path, routing_config: Path,
) -> None:
    """The modal's action_confirm dismisses with True."""

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        captured_arg: dict[str, Any] = {"value": None}

        def _cb(value: bool | None) -> None:
            captured_arg["value"] = value

        app.push_screen(NewRunConfirmScreen(), _cb)
        await pilot.pause()  # type: ignore[attr-defined]
        modal = app.screen_stack[-1]
        modal.action_confirm()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"callback_value": captured_arg["value"]}

    captured = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert captured["callback_value"] is True


def test_confirm_modal_dismisses_with_false_on_cancel(
    tmp_path: Path, routing_config: Path,
) -> None:
    """The modal's action_cancel dismisses with False — reset never runs."""

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        captured_arg: dict[str, Any] = {"value": None}

        def _cb(value: bool | None) -> None:
            captured_arg["value"] = value

        app.push_screen(NewRunConfirmScreen(), _cb)
        await pilot.pause()  # type: ignore[attr-defined]
        modal = app.screen_stack[-1]
        modal.action_cancel()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"callback_value": captured_arg["value"]}

    captured = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert captured["callback_value"] is False


# --------------------------------------------------------------------------- #
# Reset behaviour — confirmed callback flips state + remounts panes.
# --------------------------------------------------------------------------- #
def test_confirm_resets_state_and_remounts_panes_with_fresh_id(
    tmp_path: Path, routing_config: Path,
) -> None:
    """End-to-end: simulate prior progress, confirm new-run, assert state
    cleared + panes show the freshly-minted design_id."""

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        # Pretend the operator already minted a spec + paused at signoff.
        app._spec = _spec(design_id="orig")
        app._paused_state = _paused_state(design_id="orig")
        original_id = app._design_id

        # Trigger the reset directly — the modal round-trip is covered
        # by the dismiss tests above.
        app._on_new_run_confirmed(True)
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]

        # State cleared.
        spec_after = app._spec
        paused_after = app._paused_state
        final_after = app._final_state
        new_id = app._design_id

        # Pane introspection — each pane's design_id matches the new id.
        pipeline = app.query_one(PipelinePane)
        audit = app.query_one(AuditPane)
        chat = app.query_one(ChatPane)
        exports = app.query_one(ExportsPane)
        return {
            "original_id": original_id,
            "new_id": new_id,
            "spec_cleared": spec_after is None,
            "paused_cleared": paused_after is None,
            "final_cleared": final_after is None,
            "pipeline_design_id": pipeline.design_id,
            "audit_design_id": audit.design_id,
            "chat_design_id": chat.design_id,
            "exports_root": str(exports.exports_root),
        }

    captured = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert captured["original_id"] == "orig"
    assert captured["new_id"] == "new-1"
    assert captured["spec_cleared"]
    assert captured["paused_cleared"]
    assert captured["final_cleared"]
    assert captured["pipeline_design_id"] == "new-1"
    assert captured["audit_design_id"] == "new-1"
    assert captured["chat_design_id"] == "new-1"
    assert captured["exports_root"].endswith("/exports/new-1")


def test_confirm_callback_with_false_is_a_noop(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Cancel → no remint, no state change."""

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app._spec = _spec(design_id="orig")
        original_id = app._design_id
        app._on_new_run_confirmed(False)
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "id_after": app._design_id,
            "original_id": original_id,
            "spec_intact": app._spec is not None,
        }

    captured = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    assert captured["id_after"] == captured["original_id"]
    assert captured["spec_intact"]


# --------------------------------------------------------------------------- #
# Factory signature: workers receive the *current* design_id.
# --------------------------------------------------------------------------- #
def test_run_worker_receives_current_design_id_after_reset(
    tmp_path: Path, routing_config: Path,
) -> None:
    """After Ctrl+N, action_run's factory call should carry the NEW id."""

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        # Reset first.
        app._spec = _spec(design_id="orig")
        app._on_new_run_confirmed(True)
        await pilot.pause()  # type: ignore[attr-defined]
        # Re-mint a spec for the new id so action_run will fire.
        app._spec = _spec(design_id=app._design_id)
        # Stub out the worker so we don't actually drive the spine.
        from unittest import mock
        with mock.patch(
            "chip_agent.tui.app.run_pipeline",
        ) as run_mock:
            app.action_run()
            await pilot.pause()  # type: ignore[attr-defined]
            # Let the worker task settle.
            for _ in range(5):
                await pilot.pause()  # type: ignore[attr-defined]
        return {
            "run_mock_called": run_mock.called,
        }

    captured = _run(drive, run_dir=tmp_path, routing_config=routing_config)
    # The factory recorded a call with the new id (the first reset minted "new-1").
    factory_calls = captured["captured"]["factory_calls"]
    assert factory_calls
    cmd, did = factory_calls[-1]
    assert cmd == "run"
    assert did == "new-1"
