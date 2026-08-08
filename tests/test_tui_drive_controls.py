"""F14.3 acceptance: Run + Resume keybinds + drive workers.

Pins:

* ``[R]`` without a Spec is a friendly no-op (notification, no worker
  spawned).
* ``[R]`` after ``/run`` minted a Spec spawns the run worker which
  calls ``cmd_run`` and posts ``PipelinePaused`` with the produced
  ``DesignState``.
* ``[A]`` before the pipeline pauses is a friendly no-op.
* ``[A]`` after ``PipelinePaused`` spawns the resume worker which calls
  ``cmd_resume`` and posts ``PipelineCompleted`` with the final state.
* A drive worker that raises surfaces a ``RunFailed`` notification —
  the worker doesn't crash the app silently.

Drive tests don't need a real spine: we feed ``SpecMaterialised`` /
``PipelinePaused`` / ``PipelineCompleted`` messages directly via
``app.post_message`` and stub the worker entry points
(``run_pipeline`` / ``resume_pipeline``) to assert they were called
with the right ``RunArgs``.
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
from chip_agent.tui.app import ChipAgentApp, TuiResult
from chip_agent.tui.messages import (
    PipelineCompleted,
    PipelinePaused,
    RunFailed,
    SpecMaterialised,
)
from tests._routing_stub import (
    CHAT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

HMAC_KEY = b"f14.3-drive-controls-hmac-key"


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


def _spec() -> Spec:
    return Spec(
        artifact_id="d.spec", design_id="d",
        raw_text="raw", normalized="norm",
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


def _paused_state() -> DesignState:
    return DesignState(
        design_id="d", name="d",
        constraints=DesignConstraints(),
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.SIGNOFF,
    )


def _completed_state() -> DesignState:
    return DesignState(
        design_id="d", name="d",
        constraints=DesignConstraints(),
        status=DesignStatus.COMPLETED,
        current_stage=Stage.GDSII,
    )


def _build_app(
    *, run_dir: Path, routing_config: Path,
) -> tuple[ChipAgentApp, SqliteArtifactStore]:
    backend = StubBackend(matchers=CHAT_RESPONSES)
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    run_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteArtifactStore(
        db_path=run_dir / "store.sqlite",
        content_dir=run_dir / "content",
    )
    settings = Settings.from_yaml(routing_config)

    def _factory(cmd: str, current_design_id: str) -> RunArgs:
        return RunArgs(
            cmd=cmd, spec_path=None, name="counter",
            run_dir=run_dir, design_id=current_design_id,
            hmac_key=HMAC_KEY, config_path=routing_config,
        )

    app = ChipAgentApp(
        router=router,
        store=store,
        design_id="drive-test",
        name="counter",
        transcript_path=run_dir / "chat.transcript.md",
        checkpoint_path=run_dir / "checkpoint.sqlite",
        audit_db_path=run_dir / "audit.sqlite",
        hmac_key=HMAC_KEY,
        exports_dir=run_dir / "exports" / "drive-test",
        run_args_factory=_factory,
        defaults=settings.constraints,
        routing=settings.routing,
    )
    return app, store


def _run(
    drive: Callable[[ChipAgentApp, object], Awaitable[dict[str, Any]]],
    *,
    run_dir: Path,
    routing_config: Path,
) -> tuple[dict[str, Any], SqliteArtifactStore]:
    async def _go() -> tuple[dict[str, Any], SqliteArtifactStore]:
        app, store = _build_app(run_dir=run_dir, routing_config=routing_config)
        async with app.run_test() as pilot:
            await pilot.pause()
            captured = await drive(app, pilot)
        return captured, store

    return _arun(_go())


# --------------------------------------------------------------------------- #
# AC: [R] without a Spec is a friendly no-op.
# --------------------------------------------------------------------------- #
def test_r_without_spec_is_a_noop(
    tmp_path: Path, routing_config: Path,
) -> None:
    spawned: list[str] = []

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        await pilot.press("ctrl+r")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "driving": app._driving,
            "paused": app._paused_state,
            "final": app._final_state,
        }

    captured, store = _run(
        drive, run_dir=tmp_path / "run", routing_config=routing_config,
    )
    try:
        # No worker spawned, no pipeline state advanced.
        assert spawned == []
        assert captured["driving"] is False
        assert captured["paused"] is None
        assert captured["final"] is None
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: [R] after a Spec lands spawns the run worker.
# --------------------------------------------------------------------------- #
def test_r_after_spec_spawns_run_pipeline_worker(
    tmp_path: Path, routing_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub ``run_pipeline`` so the test doesn't drive the real spine.
    Verify the worker is called with the RunArgs built by the factory."""
    spawned_args: list[RunArgs] = []

    def _stub_run_pipeline(*, app, pane, args: RunArgs) -> None:  # type: ignore[no-untyped-def]
        spawned_args.append(args)
        # Don't post anything — we just want to confirm the worker ran.

    monkeypatch.setattr(
        "chip_agent.tui.app.run_pipeline", _stub_run_pipeline,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        # Feed the SpecMaterialised message directly (skip the real chat
        # flow — separately tested in test_tui_chat).
        app.post_message(SpecMaterialised(spec=_spec()))
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.press("ctrl+r")  # type: ignore[attr-defined]
        # Wait for the worker scheduler to dispatch.
        for _ in range(20):
            await pilot.pause()  # type: ignore[attr-defined]
            if spawned_args:
                break
        return {"spawned_args": list(spawned_args), "driving": app._driving}

    captured, store = _run(
        drive, run_dir=tmp_path / "run", routing_config=routing_config,
    )
    try:
        assert len(captured["spawned_args"]) == 1
        args = captured["spawned_args"][0]
        assert args.cmd == "run"
        assert args.design_id == "drive-test"
        assert args.name == "counter"
        assert captured["driving"] is True  # worker is "in flight"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: PipelinePaused message populates app._paused_state and the
# subsequent [A] press spawns the resume worker.
# --------------------------------------------------------------------------- #
def test_pipeline_paused_message_enables_approve(
    tmp_path: Path, routing_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned_args: list[RunArgs] = []

    def _stub_resume_pipeline(*, app, pane, args: RunArgs) -> None:  # type: ignore[no-untyped-def]
        spawned_args.append(args)

    monkeypatch.setattr(
        "chip_agent.tui.app.resume_pipeline", _stub_resume_pipeline,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        # Simulate the run worker posting PipelinePaused.
        app.post_message(PipelinePaused(state=_paused_state()))
        await pilot.pause()  # type: ignore[attr-defined]
        # [A] should spawn the resume worker.
        await pilot.press("ctrl+a")  # type: ignore[attr-defined]
        for _ in range(20):
            await pilot.pause()  # type: ignore[attr-defined]
            if spawned_args:
                break
        return {
            "spawned_args": list(spawned_args),
            "paused": app._paused_state,
        }

    captured, store = _run(
        drive, run_dir=tmp_path / "run", routing_config=routing_config,
    )
    try:
        assert captured["paused"] is not None
        assert len(captured["spawned_args"]) == 1
        assert captured["spawned_args"][0].cmd == "resume"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: [A] before the pipeline pauses is a friendly no-op.
# --------------------------------------------------------------------------- #
def test_a_before_pause_is_a_noop(
    tmp_path: Path, routing_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[RunArgs] = []

    def _stub_resume_pipeline(*, app, pane, args: RunArgs) -> None:  # type: ignore[no-untyped-def]
        spawned.append(args)

    monkeypatch.setattr(
        "chip_agent.tui.app.resume_pipeline", _stub_resume_pipeline,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        await pilot.press("ctrl+a")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"spawned": list(spawned), "driving": app._driving}

    captured, store = _run(
        drive, run_dir=tmp_path / "run", routing_config=routing_config,
    )
    try:
        assert captured["spawned"] == []
        assert captured["driving"] is False
    finally:
        store.close()


def test_a_refuses_to_resume_when_paused_at_pre_signoff_stage(
    tmp_path: Path, routing_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the spine paused at RTL because escalation walked all the way
    up to HUMAN (F12.5 frontier-fallback also failed), ``cmd_resume``
    can't recover — driving past the await_human node into ``gdsii_emit``
    would crash with MissingHeadError because no PHYSICAL head was
    promoted. Refuse the keybind with a clear error notification."""
    spawned: list[RunArgs] = []

    def _stub_resume_pipeline(*, app, pane, args: RunArgs) -> None:  # type: ignore[no-untyped-def]
        spawned.append(args)

    monkeypatch.setattr(
        "chip_agent.tui.app.resume_pipeline", _stub_resume_pipeline,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        # Simulate the run worker reporting "paused at RTL, status=AWAITING_HUMAN".
        rtl_stuck = DesignState(
            design_id="d", name="d",
            constraints=DesignConstraints(),
            status=DesignStatus.AWAITING_HUMAN,
            current_stage=Stage.RTL,
        )
        app.post_message(PipelinePaused(state=rtl_stuck))
        await pilot.pause()  # type: ignore[attr-defined]
        # [A] should NOT spawn a worker — current_stage isn't SIGNOFF.
        await pilot.press("ctrl+a")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"spawned": list(spawned), "driving": app._driving}

    captured, store = _run(
        drive, run_dir=tmp_path / "run", routing_config=routing_config,
    )
    try:
        assert captured["spawned"] == [], (
            "Ctrl+A must refuse to resume when the spine paused before SIGNOFF"
        )
        assert captured["driving"] is False
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: PipelineCompleted stores the final state but does NOT auto-exit. The
# operator wants a beat to inspect signoff / exports / chat scrollback when
# the GDS lands — auto-closing the TUI immediately destroyed all of that
# context. A subsequent Ctrl+Q triggers ``action_quit`` which snapshots the
# same TuiResult shape that the auto-exit used to emit, so ``cmd_tui``
# post-exit hints still fire correctly.
# --------------------------------------------------------------------------- #
def test_pipeline_completed_stays_open_until_manual_quit(
    tmp_path: Path, routing_config: Path,
) -> None:
    """The completion message records the final state, drops the driving
    flag, and surfaces a "Ctrl+Q to quit" notification — but the app stays
    alive so the operator can inspect the signoff dashboard, the export
    manifest, and the chat scrollback before closing the terminal."""
    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app.post_message(PipelineCompleted(state=_completed_state()))
        # Give the message loop several ticks; the app must NOT exit.
        for _ in range(20):
            await pilot.pause()  # type: ignore[attr-defined]
        return {
            "return_value": app.return_value,
            "final_state": app._final_state,
            "driving": app._driving,
        }

    captured, store = _run(
        drive, run_dir=tmp_path / "run", routing_config=routing_config,
    )
    try:
        # Critical: no auto-exit. ``return_value`` only becomes non-None
        # when the operator chooses to quit.
        assert captured["return_value"] is None
        # But the final state is recorded so a subsequent Ctrl+Q produces
        # a fully-populated TuiResult.
        assert captured["final_state"] is not None
        assert captured["final_state"].status is DesignStatus.COMPLETED
        # And the driving flag has been cleared so [R]/[A] become inert
        # again (no double-spawn if the operator hits a hotkey mid-look).
        assert captured["driving"] is False
    finally:
        store.close()


def test_manual_quit_after_completion_returns_populated_tui_result(
    tmp_path: Path, routing_config: Path,
) -> None:
    """After completion + Ctrl+Q, the TuiResult carries the final state
    so ``cmd_tui``'s post-exit hint code still gets the same signal it
    used to get from the auto-exit path."""
    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app.post_message(PipelineCompleted(state=_completed_state()))
        await pilot.pause()  # type: ignore[attr-defined]
        # Now press Ctrl+Q — the operator is done inspecting.
        await pilot.press("ctrl+q")  # type: ignore[attr-defined]
        for _ in range(20):
            await pilot.pause()  # type: ignore[attr-defined]
            if app.return_value is not None:
                break
        return {"return_value": app.return_value}

    captured, store = _run(
        drive, run_dir=tmp_path / "run", routing_config=routing_config,
    )
    try:
        result = captured["return_value"]
        assert isinstance(result, TuiResult)
        assert result.final_state is not None
        assert result.final_state.status is DesignStatus.COMPLETED
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: RunFailed surfaces as a notification + restores _driving.
# --------------------------------------------------------------------------- #
def test_run_failed_message_clears_driving_flag(
    tmp_path: Path, routing_config: Path,
) -> None:
    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        # Simulate the worker tripping while _driving was True.
        app._driving = True
        app.post_message(
            RunFailed(stage="run", message="ValueError: boom"),
        )
        await pilot.pause()  # type: ignore[attr-defined]
        return {"driving": app._driving}

    captured, store = _run(
        drive, run_dir=tmp_path / "run", routing_config=routing_config,
    )
    try:
        assert captured["driving"] is False
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# cmd_tui handoff print: paused / completed states produce distinct hints.
# --------------------------------------------------------------------------- #
def test_cmd_tui_prints_resume_hint_when_paused(
    tmp_path: Path, routing_config: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When [R] paused the pipeline at AWAITING_HUMAN and the operator
    quit without pressing [A], the post-exit print should say so and
    show the `chip-agent resume` command."""
    from chip_agent.cli import cmd_tui

    def _fake_run(self, **kwargs):  # type: ignore[no-untyped-def]
        self._store.put(_spec())
        return TuiResult(spec=_spec(), paused_state=_paused_state())

    monkeypatch.setattr(
        "chip_agent.tui.app.ChipAgentApp.run", _fake_run,
    )
    # _resolve_router patch so the CLI plumbing doesn't try to talk to
    # the real API.
    backend = StubBackend()
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router", lambda _a, *, settings: router,
    )

    # The _paused_state() fixture carries design_id="d"; cmd_tui now
    # reads that out of the state when reporting handoff. Match the args
    # to the fixture so the assertions are checking real coupling, not
    # the fixture's hard-coded id.
    args = RunArgs(
        cmd="tui",
        spec_path=None, name="counter",
        run_dir=tmp_path / "run", design_id="d",
        hmac_key=HMAC_KEY, config_path=routing_config,
    )
    cmd_tui(args)
    captured = capsys.readouterr().out
    assert "awaiting_human" in captured
    assert "chip-agent resume --design-id d" in captured


def test_cmd_tui_prints_completed_hint_when_final(
    tmp_path: Path, routing_config: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the pipeline completed in-app, the post-exit print should
    say so and point at the exports/gds/ dir."""
    from chip_agent.cli import cmd_tui

    def _fake_run(self, **kwargs):  # type: ignore[no-untyped-def]
        self._store.put(_spec())
        return TuiResult(
            spec=_spec(),
            paused_state=_paused_state(),
            final_state=_completed_state(),
        )

    monkeypatch.setattr(
        "chip_agent.tui.app.ChipAgentApp.run", _fake_run,
    )
    backend = StubBackend()
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router", lambda _a, *, settings: router,
    )

    args = RunArgs(
        cmd="tui",
        spec_path=None, name="counter",
        run_dir=tmp_path / "run", design_id="d",
        hmac_key=HMAC_KEY, config_path=routing_config,
    )
    cmd_tui(args)
    captured = capsys.readouterr().out
    assert "completed" in captured
    assert "exports/d/gds" in captured
