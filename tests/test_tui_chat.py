"""F14.1 acceptance: TUI scaffolding + chat pane vertical slice.

Drives the new ``chip_agent.tui.ChipAgentApp`` through Textual's
``App.run_test()`` harness against the existing stub backend
(``tests._routing_stub.CHAT_RESPONSES``), and pins the same invariants
the F11.3 CLI REPL tests pin — only this time via the TUI.

ACs (from the M14 plan):

* The ``tui`` subcommand parses with the same shared flags as ``chat``.
* The chat pane streams chunks from ``router.stream`` into its
  scrollable log as they arrive.
* ``/run`` mints a typed :class:`Spec` via :class:`SpecIntakeAgent` and
  stores it under ``<design_id>.spec``; the app exits with the spec.
* ``/exit`` quits the app cleanly without minting a Spec.

The async ``App.run_test()`` harness is driven via ``asyncio.run`` in
sync test bodies so we don't need a pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from chip_agent.cli import RunArgs, TuiOutcome, build_arg_parser, cmd_tui
from chip_agent.design_state import ArtifactKind, Spec
from chip_agent.settings import Settings
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tui.app import ChipAgentApp
from chip_agent.tui.panes.chat import ChatPane
from tests._routing_stub import (
    CHAT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

HMAC_KEY = b"f14.1-tui-test-hmac-key"


def _arun(coro: Awaitable[Any]) -> Any:
    """Run an async coroutine to completion from a sync test body."""
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


@pytest.fixture
def stub_backend() -> StubBackend:
    """Backend that recognises both the chat persona AND SpecIntake prompts."""
    return StubBackend(matchers=CHAT_RESPONSES)


@pytest.fixture
def patched_router(
    monkeypatch: pytest.MonkeyPatch,
    routing_config: Path,
    stub_backend: StubBackend,
) -> StubBackend:
    """Make ``cli._resolve_router`` return our stub-backed router."""
    router, _ = make_test_router(config_path=routing_config, backend=stub_backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router",
        lambda _args, *, settings: router,
    )
    return stub_backend


# --------------------------------------------------------------------------- #
# Argparse — the `tui` subcommand parses and dispatches correctly.
# --------------------------------------------------------------------------- #
def test_arg_parser_accepts_tui_subcommand(tmp_path: Path) -> None:
    parser = build_arg_parser()
    ns = parser.parse_args([
        "tui",
        "--name", "counter",
        "--design-id", "smoke",
        "--run-dir", str(tmp_path / "rd"),
        "--config", str(tmp_path / "routing.yaml"),
    ])
    assert ns.cmd == "tui"
    assert ns.name == "counter"
    assert ns.design_id == "smoke"
    assert ns.run_dir == tmp_path / "rd"


def test_arg_parser_tui_supplies_defaults() -> None:
    """Bare ``chip-agent tui`` falls back to the documented defaults.

    The TUI is the zero-friction entry point: ``--name``, ``--run-dir``,
    ``--config``, and ``--sandbox`` all have sensible defaults so a new
    user can launch the app without any flags. ``--sandbox docker`` is
    the default because stub mode silently produces placeholder GDS +
    signoff bodies that look indistinguishable from real output in the
    audit log. The other subcommands (run/chat/resume) stay strict.
    """
    parser = build_arg_parser()
    ns = parser.parse_args(["tui"])
    assert ns.cmd == "tui"
    assert ns.name == "untitled"
    assert ns.run_dir == Path("./runs")
    assert ns.config == Path("configs/local-only.yaml")
    assert ns.sandbox == "docker"


# --------------------------------------------------------------------------- #
# App-level harness
# --------------------------------------------------------------------------- #
def _build_app(
    *, run_dir: Path, routing_config: Path, design_id: str = "tui-smoke",
) -> tuple[ChipAgentApp, SqliteArtifactStore]:
    """Build a ChipAgentApp wired against the stub-backed test router."""
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
        design_id=design_id,
        name="counter",
        transcript_path=run_dir / "chat.transcript.md",
        checkpoint_path=run_dir / "checkpoint.sqlite",
        audit_db_path=run_dir / "audit.sqlite",
        hmac_key=HMAC_KEY,
        exports_dir=run_dir / "exports" / design_id,
        run_args_factory=_factory,
        defaults=settings.constraints,
        routing=settings.routing,
    )
    return app, store


async def _drain(pane: ChatPane, pilot: object, *, max_ticks: int = 30) -> None:
    """Pump the event loop until the pane's streaming worker drains."""
    for _ in range(max_ticks):
        await pilot.pause()  # type: ignore[attr-defined]
        if not pane._streaming:
            return


def _run_app_test(
    app_factory: Callable[[], tuple[ChipAgentApp, SqliteArtifactStore]],
    drive: Callable[[ChipAgentApp, ChatPane, object], Awaitable[dict[str, Any]]],
) -> tuple[dict[str, Any], SqliteArtifactStore]:
    """Run an App.run_test() session and return state captured inside it.

    Querying the app's widgets AFTER ``run_test()`` exits returns nothing —
    Textual tears the screen down on exit. The ``drive`` callback must
    therefore capture everything the test needs into a ``dict`` and return
    it; we surface that dict plus the still-open store.
    """

    async def _go() -> tuple[dict[str, Any], SqliteArtifactStore]:
        app, store = app_factory()
        async with app.run_test() as pilot:
            await pilot.pause()
            pane = app.query_one(ChatPane)
            captured = await drive(app, pane, pilot)
        return captured, store

    return _arun(_go())


# --------------------------------------------------------------------------- #
# AC: streaming chunks reach the chat log in order.
# --------------------------------------------------------------------------- #
def test_chat_pane_streams_stub_chunks_to_log(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Type one user line, assert the streamed assistant reply lands."""
    run_dir = tmp_path / "run"

    async def drive(
        app: ChipAgentApp, pane: ChatPane, pilot: object,
    ) -> dict[str, Any]:
        from textual.widgets import Input
        input_widget = pane.query_one("#chat-input", Input)
        input_widget.value = "make me an 8-bit counter"
        await pilot.press("enter")  # type: ignore[attr-defined]
        await _drain(pane, pilot)
        return {"turns": [(t.role, t.text) for t in pane.turns]}

    captured, store = _run_app_test(
        lambda: _build_app(run_dir=run_dir, routing_config=routing_config),
        drive,
    )
    try:
        turns = captured["turns"]
        assert [r for r, _ in turns] == ["user", "assistant"]
        assert "make me an 8-bit counter" in turns[0][1]
        assert turns[1][1]  # assistant text non-empty
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: /run mints a Spec and exits the app with the Spec as result.
# --------------------------------------------------------------------------- #
def test_run_slash_command_mints_spec_and_stores_on_app(
    tmp_path: Path, routing_config: Path,
) -> None:
    """F14.3: ``/run`` no longer exits the app — it stores the minted
    Spec on the app so the operator can press ``[R]`` to drive the
    spine in-place. The spec is still persisted to the store."""
    run_dir = tmp_path / "run"

    async def drive(
        app: ChipAgentApp, pane: ChatPane, pilot: object,
    ) -> dict[str, Any]:
        from textual.widgets import Input
        input_widget = pane.query_one("#chat-input", Input)

        # First, describe the module so /run has a non-empty transcript.
        input_widget.value = "8-bit counter with active-low async reset"
        await pilot.press("enter")  # type: ignore[attr-defined]
        await _drain(pane, pilot)

        # Then /run. The app should NOT exit — it should store the spec.
        input_widget.value = "/run"
        await pilot.press("enter")  # type: ignore[attr-defined]
        for _ in range(30):
            await pilot.pause()  # type: ignore[attr-defined]
            if app._spec is not None:
                break

        return {
            "app_spec": app._spec,
            "return_value": app.return_value,
        }

    captured, store = _run_app_test(
        lambda: _build_app(run_dir=run_dir, routing_config=routing_config),
        drive,
    )
    try:
        # /run mints the Spec onto app._spec.
        app_spec = captured["app_spec"]
        assert isinstance(app_spec, Spec)
        assert app_spec.kind is ArtifactKind.SPEC
        # The app is still running — no return_value yet.
        assert captured["return_value"] is None
        # The spec was persisted to the store under <design_id>.spec.
        loaded = store.get_by_id("tui-smoke.spec")
        assert isinstance(loaded, Spec)
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: /exit quits cleanly without minting a Spec.
# --------------------------------------------------------------------------- #
def test_exit_slash_command_quits_without_spec(
    tmp_path: Path, routing_config: Path,
) -> None:
    run_dir = tmp_path / "run"

    async def drive(
        app: ChipAgentApp, pane: ChatPane, pilot: object,
    ) -> dict[str, Any]:
        from textual.widgets import Input
        input_widget = pane.query_one("#chat-input", Input)
        input_widget.value = "/exit"
        await pilot.press("enter")  # type: ignore[attr-defined]
        for _ in range(10):
            await pilot.pause()  # type: ignore[attr-defined]
        return {"return_value": app.return_value}

    captured, store = _run_app_test(
        lambda: _build_app(run_dir=run_dir, routing_config=routing_config),
        drive,
    )
    try:
        assert captured["return_value"] is None
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: /run -> ClarifyingQuestion -> user answer -> Spec materialised.
# --------------------------------------------------------------------------- #
def test_run_multi_round_clarifies_then_mints(
    tmp_path: Path, routing_config: Path,
) -> None:
    """The TUI's /run loop should survive a clarifying-question round.

    Reproduces the frontier-side failure mode where the user pressed
    /run once, got a clarifying question, answered it, and the answer
    went to the chat persona instead of re-entering intake. With the
    multi-round patch, the pane keeps the :class:`SpecIntakeAgent`
    alive across rounds — the answer re-enters intake and the next
    backend call returns the normalised Spec.
    """
    import json

    from tests._routing_stub import COUNTER_SPEC_NORMALISED, _PromptMatcher

    run_dir = tmp_path / "run"
    question_json = json.dumps({
        "type": "question",
        "question": "What port width should the counter have?",
        "missing_field": "ports",
        "rationale": "no width stated",
    })
    # Reshape the CHAT_RESPONSES tuple so the SpecIntake matcher cycles
    # [question_json, COUNTER_SPEC_NORMALISED] across two calls.
    matchers = tuple(
        _PromptMatcher(
            needle=m.needle,
            response=m.response,
            response_sequence=(question_json, COUNTER_SPEC_NORMALISED)
            if "normalise natural-language" in m.needle else None,
            stream_chunks=m.stream_chunks,
            prompt_tokens=m.prompt_tokens,
            completion_tokens=m.completion_tokens,
            cost_usd=m.cost_usd,
        )
        for m in CHAT_RESPONSES
    )

    def _factory() -> tuple[ChipAgentApp, SqliteArtifactStore]:
        backend = StubBackend(matchers=matchers)
        router, _ = make_test_router(config_path=routing_config, backend=backend)
        run_dir.mkdir(parents=True, exist_ok=True)
        store = SqliteArtifactStore(
            db_path=run_dir / "store.sqlite",
            content_dir=run_dir / "content",
        )
        settings = Settings.from_yaml(routing_config)

        def _run_args(cmd: str, did: str) -> RunArgs:
            return RunArgs(
                cmd=cmd, spec_path=None, name="counter",
                run_dir=run_dir, design_id=did,
                hmac_key=HMAC_KEY, config_path=routing_config,
            )

        app = ChipAgentApp(
            router=router, store=store, design_id="tui-multiround",
            name="counter",
            transcript_path=run_dir / "chat.transcript.md",
            checkpoint_path=run_dir / "checkpoint.sqlite",
            audit_db_path=run_dir / "audit.sqlite",
            hmac_key=HMAC_KEY,
            exports_dir=run_dir / "exports" / "tui-multiround",
            run_args_factory=_run_args,
            defaults=settings.constraints,
            routing=settings.routing,
        )
        return app, store

    async def drive(
        app: ChipAgentApp, pane: ChatPane, pilot: object,
    ) -> dict[str, Any]:
        from textual.widgets import Input
        input_widget = pane.query_one("#chat-input", Input)

        # Round 0 — seed the transcript so /run has user content.
        input_widget.value = "Make me a counter."
        await pilot.press("enter")  # type: ignore[attr-defined]
        await _drain(pane, pilot)

        # /run — backend's first SpecIntake reply is the clarifying question.
        input_widget.value = "/run"
        await pilot.press("enter")  # type: ignore[attr-defined]
        await _drain(pane, pilot)

        intake_active_after_q = pane._intake_agent is not None

        # User answers — should re-enter intake (NOT the chat streamer).
        input_widget.value = "8 bits, active-low async reset."
        await pilot.press("enter")  # type: ignore[attr-defined]
        for _ in range(40):
            await pilot.pause()  # type: ignore[attr-defined]
            if app._spec is not None:
                break

        return {
            "intake_active_after_q": intake_active_after_q,
            "intake_active_after_spec": pane._intake_agent is not None,
            "app_spec": app._spec,
        }

    captured, store = _run_app_test(_factory, drive)
    try:
        assert captured["intake_active_after_q"] is True
        # Spec landed: the answer routed back to intake, not the chat streamer.
        assert isinstance(captured["app_spec"], Spec)
        # And intake state was cleared after the Spec was materialised.
        assert captured["intake_active_after_spec"] is False
        # The store holds it under the documented id.
        loaded = store.get_by_id("tui-multiround.spec")
        assert isinstance(loaded, Spec)
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: /cancel exits an in-progress intake without minting a Spec.
# --------------------------------------------------------------------------- #
def test_cancel_clears_intake_state_mid_round(
    tmp_path: Path, routing_config: Path,
) -> None:
    """After a clarifying question, ``/cancel`` should drop the agent so
    the next plain user message goes through the chat streamer again."""
    import json

    from tests._routing_stub import COUNTER_SPEC_NORMALISED, _PromptMatcher

    run_dir = tmp_path / "run"
    question_json = json.dumps({
        "type": "question",
        "question": "How many bits?",
        "missing_field": "ports",
        "rationale": "no width stated",
    })
    matchers = tuple(
        _PromptMatcher(
            needle=m.needle,
            response=m.response,
            response_sequence=(question_json, COUNTER_SPEC_NORMALISED)
            if "normalise natural-language" in m.needle else None,
            stream_chunks=m.stream_chunks,
            prompt_tokens=m.prompt_tokens,
            completion_tokens=m.completion_tokens,
            cost_usd=m.cost_usd,
        )
        for m in CHAT_RESPONSES
    )

    def _factory() -> tuple[ChipAgentApp, SqliteArtifactStore]:
        backend = StubBackend(matchers=matchers)
        router, _ = make_test_router(config_path=routing_config, backend=backend)
        run_dir.mkdir(parents=True, exist_ok=True)
        store = SqliteArtifactStore(
            db_path=run_dir / "store.sqlite",
            content_dir=run_dir / "content",
        )
        settings = Settings.from_yaml(routing_config)

        def _run_args(cmd: str, did: str) -> RunArgs:
            return RunArgs(
                cmd=cmd, spec_path=None, name="counter",
                run_dir=run_dir, design_id=did,
                hmac_key=HMAC_KEY, config_path=routing_config,
            )

        app = ChipAgentApp(
            router=router, store=store, design_id="tui-cancel",
            name="counter",
            transcript_path=run_dir / "chat.transcript.md",
            checkpoint_path=run_dir / "checkpoint.sqlite",
            audit_db_path=run_dir / "audit.sqlite",
            hmac_key=HMAC_KEY,
            exports_dir=run_dir / "exports" / "tui-cancel",
            run_args_factory=_run_args,
            defaults=settings.constraints,
            routing=settings.routing,
        )
        return app, store

    async def drive(
        app: ChipAgentApp, pane: ChatPane, pilot: object,
    ) -> dict[str, Any]:
        from textual.widgets import Input
        input_widget = pane.query_one("#chat-input", Input)
        input_widget.value = "Make me a counter."
        await pilot.press("enter")  # type: ignore[attr-defined]
        await _drain(pane, pilot)
        input_widget.value = "/run"
        await pilot.press("enter")  # type: ignore[attr-defined]
        await _drain(pane, pilot)
        active_before = pane._intake_agent is not None
        input_widget.value = "/cancel"
        await pilot.press("enter")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "active_before": active_before,
            "active_after": pane._intake_agent is not None,
            "app_spec": app._spec,
        }

    captured, store = _run_app_test(_factory, drive)
    try:
        assert captured["active_before"] is True
        assert captured["active_after"] is False
        assert captured["app_spec"] is None
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: /run before any user turn is a friendly no-op (no Spec, no crash).
# --------------------------------------------------------------------------- #
def test_run_before_any_user_turn_is_a_noop(
    tmp_path: Path, routing_config: Path,
) -> None:
    run_dir = tmp_path / "run"

    async def drive(
        app: ChipAgentApp, pane: ChatPane, pilot: object,
    ) -> dict[str, Any]:
        from textual.widgets import Input
        input_widget = pane.query_one("#chat-input", Input)
        input_widget.value = "/run"
        await pilot.press("enter")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"return_value": app.return_value, "turns": list(pane.turns)}

    captured, store = _run_app_test(
        lambda: _build_app(run_dir=run_dir, routing_config=routing_config),
        drive,
    )
    try:
        assert captured["return_value"] is None
        assert captured["turns"] == []
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# cmd_tui entry point: end-to-end through the CLI layer.
# --------------------------------------------------------------------------- #
def test_cmd_tui_returns_outcome_on_exit(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """``cmd_tui`` builds the app, runs it, returns a ``TuiOutcome``.

    We monkeypatch ``ChipAgentApp.run`` to skip the actual TUI loop and
    return ``None`` (simulating the operator quitting via ``/exit``).
    The CLI plumbing should still produce a ``TuiOutcome`` with the
    right shape AND print a handoff hint so the operator knows whether
    a Spec was minted.
    """
    from chip_agent.tui.app import TuiResult
    monkeypatch.setattr(
        "chip_agent.tui.app.ChipAgentApp.run", lambda self, **kwargs: TuiResult(),
    )
    args = RunArgs(
        cmd="tui",
        spec_path=None, name="counter",
        run_dir=tmp_path / "run", design_id="tui-cli-smoke",
        hmac_key=HMAC_KEY, config_path=routing_config,
    )
    out = cmd_tui(args)
    assert isinstance(out, TuiOutcome)
    assert out.design_id == "tui-cli-smoke"
    assert out.spec_ref is None  # operator quit without minting
    assert out.exports_dir == tmp_path / "run" / "exports" / "tui-cli-smoke"
    # Handoff hint should make it clear no Spec was minted.
    captured = capsys.readouterr()
    assert "no spec materialised" in captured.out


def test_cmd_tui_prints_handoff_hint_when_spec_minted(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``/run`` mints a Spec and the operator quits before pressing
    [R], the post-exit print surfaces the spec ref + exports path + the
    ``chip-agent run`` handoff command — same shape ``cmd_chat`` uses
    so the operator's mental model carries across both entry points."""
    from chip_agent.design_state import DesignConstraints, Provenance, Stage
    from chip_agent.tui.app import TuiResult
    spec = Spec(
        artifact_id="tui-cli-handoff.spec",
        design_id="tui-cli-handoff",
        raw_text="raw", normalized="norm",
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC),
    )

    def _fake_run(self, **kwargs):  # type: ignore[no-untyped-def]
        # Persist the spec the way the real app would; quit by returning
        # a TuiResult that has the spec but no paused/final state.
        self._store.put(spec)
        return TuiResult(spec=spec)

    monkeypatch.setattr(
        "chip_agent.tui.app.ChipAgentApp.run", _fake_run,
    )
    args = RunArgs(
        cmd="tui",
        spec_path=None, name="counter",
        run_dir=tmp_path / "run", design_id="tui-cli-handoff",
        hmac_key=HMAC_KEY, config_path=routing_config,
    )
    out = cmd_tui(args)
    assert out.spec_ref is not None
    captured = capsys.readouterr()
    assert "spec_ref:" in captured.out
    assert "tui-cli-handoff.spec" in captured.out
    assert "exports:" in captured.out
    assert "chip-agent run --design-id tui-cli-handoff" in captured.out
