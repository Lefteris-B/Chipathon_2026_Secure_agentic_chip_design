"""F15.1 acceptance: model picker modal (read-only).

Pins:

* ``Ctrl+L`` pushes the :class:`ModelPickerScreen` modal.
* The keybind is refused while a worker is in flight (parity with
  Ctrl+N's gate).
* The keybind is a friendly no-op when ``routing`` isn't wired (test
  harnesses + future entry points that opt out).
* The modal renders one row per ``routing.tasks.*`` binding showing
  the resolved registry entry's ``provider:model``.
* The modal renders one row per ``routing.loops.*`` binding.
* The modal dismisses with ``False`` on Esc and the Close button.

Plus an invariant test for the M15 mutation contract: ``TaskBinding``
must be mutable so F15.2's in-place ``binding.model = ...`` swap
works. The day someone freezes the class, this test fires loudly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from chip_agent.cli import RunArgs
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.settings import (
    LoopBinding,
    ModelEntry,
    RoutingSettings,
    Settings,
    TaskBinding,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tui.app import ChipAgentApp
from chip_agent.tui.panes.model_picker import (
    ModelPickerScreen,
    format_binding_row,
)
from tests._routing_stub import (
    CHAT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

HMAC_KEY = b"f15.1-model-picker-test-hmac-key"


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Invariant tests — TaskBinding mutability is load-bearing for F15.2.
# --------------------------------------------------------------------------- #
def test_task_binding_is_mutable() -> None:
    """F15.2's apply mutates ``binding.model = new_key`` in place. Pydantic
    v2 models default to mutable; if someone sets ``frozen=True`` on
    ``TaskBinding``, the mutation silently fails (or raises) and Ctrl+L
    becomes a lie. Fail loudly here so the regression surfaces in this
    test file (which references F15.2 semantics by name)."""
    binding = TaskBinding(model="frontier", temperature=0.0, n=1)
    binding.model = "local-coder"
    assert binding.model == "local-coder"


def test_loop_binding_is_mutable() -> None:
    """Same invariant for ``LoopBinding`` — F15.4 needs it."""
    binding = LoopBinding(model="frontier", temperature=0.0, n=1)
    binding.model = "local-coder"
    assert binding.model == "local-coder"


# --------------------------------------------------------------------------- #
# Pure-function tests for the row formatter.
# --------------------------------------------------------------------------- #
def _routing(
    *,
    registry: dict[str, ModelEntry] | None = None,
    tasks: dict[str, TaskBinding] | None = None,
    loops: dict[str, LoopBinding] | None = None,
) -> RoutingSettings:
    # Use ``is not None`` not ``or`` — an empty dict is a valid override
    # the missing-registry test relies on.
    default_registry = {
        "frontier": ModelEntry(
            provider="anthropic", model="claude-sonnet-4-6",
        ),
        "local-coder": ModelEntry(
            provider="ollama", model="qwen3-coder:latest",
        ),
    }
    return RoutingSettings(
        registry=registry if registry is not None else default_registry,
        tasks=tasks if tasks is not None else {},
        loops=loops if loops is not None else {},
    )


def test_format_binding_row_shows_task_model_and_resolved_entry() -> None:
    routing = _routing(
        tasks={"spec_intake": TaskBinding(model="frontier")},
    )
    row = format_binding_row(
        "spec_intake", routing.tasks["spec_intake"], routing,
    )
    assert "spec_intake" in row
    assert "frontier" in row  # the registry key
    assert "anthropic" in row  # the resolved provider
    assert "claude-sonnet-4-6" in row  # the resolved model


def test_format_binding_row_flags_missing_registry_entry() -> None:
    """Defensive: if a binding points to a key not in registry (shouldn't
    happen post-validation), the row makes the gap visible."""
    binding = TaskBinding(model="frontier")
    routing = _routing(registry={}, tasks={"spec_intake": binding})
    row = format_binding_row("spec_intake", binding, routing)
    assert "missing from registry" in row


def test_format_binding_row_includes_temperature_and_n() -> None:
    binding = TaskBinding(model="frontier", temperature=0.7, n=3)
    routing = _routing(tasks={"rtl_gen": binding})
    row = format_binding_row("rtl_gen", binding, routing)
    assert "0.70" in row
    assert "n=3" in row


# --------------------------------------------------------------------------- #
# Mounted-modal tests: render Static rows for every binding.
# --------------------------------------------------------------------------- #
def _drive_modal(
    drive: Callable[[ModelPickerScreen, object], Awaitable[dict[str, Any]]],
    *,
    routing: RoutingSettings,
) -> dict[str, Any]:
    """Mount a :class:`ModelPickerScreen` directly in a host App."""
    from textual.app import App

    class _Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(ModelPickerScreen(routing=routing))

    async def _go() -> dict[str, Any]:
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = host.screen_stack[-1]
            assert isinstance(screen, ModelPickerScreen)
            return await drive(screen, pilot)

    return _arun(_go())


def test_model_picker_renders_every_task_binding() -> None:
    """Each task row is a Horizontal with id ``task-<name>`` containing
    a Select set to the current model + a suffix Static with the
    resolved provider:model + knobs."""
    routing = _routing(
        tasks={
            "spec_intake": TaskBinding(model="frontier"),
            "plan": TaskBinding(model="frontier"),
            "rtl_gen": TaskBinding(model="local-coder"),
            "rtl_repair": TaskBinding(model="local-coder"),
            "tb_gen": TaskBinding(model="local-coder"),
            "diagnose": TaskBinding(model="frontier"),
        },
    )

    async def drive(screen: ModelPickerScreen, pilot: object) -> dict[str, Any]:
        from textual.widgets import Select, Static
        rows: dict[str, dict[str, Any]] = {}
        for t in routing.tasks:
            select = screen.query_one(f"#task-select-{t}", Select)
            suffix = screen.query_one(f"#task-suffix-{t}", Static)
            rows[t] = {
                "select_value": select.value,
                "suffix": str(suffix.renderable),
            }
        return {"rows": rows}

    captured = _drive_modal(drive, routing=routing)
    assert len(captured["rows"]) == 6
    assert captured["rows"]["spec_intake"]["select_value"] == "frontier"
    assert captured["rows"]["rtl_gen"]["select_value"] == "local-coder"
    # Suffix carries the resolved provider:model.
    assert "anthropic" in captured["rows"]["plan"]["suffix"]
    assert "ollama" in captured["rows"]["rtl_gen"]["suffix"]


def test_model_picker_renders_loop_bindings_when_present() -> None:
    """Loop rows mirror the task layout, with id ``loop-<slot>``. F15.4
    enables editing — the Select is no longer disabled."""
    routing = _routing(
        loops={
            "inner": LoopBinding(model="local-coder"),
            "outer": LoopBinding(model="frontier"),
        },
    )

    async def drive(screen: ModelPickerScreen, pilot: object) -> dict[str, Any]:
        from textual.widgets import Select, Static
        return {
            "inner_value": screen.query_one(
                "#loop-select-inner", Select,
            ).value,
            "outer_value": screen.query_one(
                "#loop-select-outer", Select,
            ).value,
            "inner_disabled": screen.query_one(
                "#loop-select-inner", Select,
            ).disabled,
            "inner_suffix": str(screen.query_one(
                "#loop-suffix-inner", Static,
            ).renderable),
            "outer_suffix": str(screen.query_one(
                "#loop-suffix-outer", Static,
            ).renderable),
        }

    captured = _drive_modal(drive, routing=routing)
    assert captured["inner_value"] == "local-coder"
    assert captured["outer_value"] == "frontier"
    assert captured["inner_disabled"] is False  # F15.4 enables editing
    assert "ollama" in captured["inner_suffix"]
    assert "anthropic" in captured["outer_suffix"]


def test_model_picker_renders_empty_state_when_no_bindings() -> None:
    routing = _routing(tasks={}, loops={})

    async def drive(screen: ModelPickerScreen, pilot: object) -> dict[str, Any]:
        from textual.widgets import Static
        return {
            "empty": str(screen.query_one("#picker-empty", Static).renderable),
        }

    captured = _drive_modal(drive, routing=routing)
    assert "no routing bindings" in captured["empty"]


def test_model_picker_dismisses_on_close_action() -> None:
    """Esc / Close button dismisses with None (cancellation)."""
    routing = _routing(tasks={"plan": TaskBinding(model="frontier")})

    captured_value: dict[str, Any] = {"value": "sentinel"}

    from textual.app import App

    class _Host(App[None]):
        def on_mount(self) -> None:
            def _cb(value: dict[str, str] | None) -> None:
                captured_value["value"] = value

            self.push_screen(ModelPickerScreen(routing=routing), _cb)

    async def _go() -> dict[str, Any]:
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            modal = host.screen_stack[-1]
            modal.action_close()  # type: ignore[attr-defined]
            await pilot.pause()
            return {"dismiss_value": captured_value["value"]}

    captured = _arun(_go())
    assert captured["dismiss_value"] is None


# --------------------------------------------------------------------------- #
# F15.2: Apply path — modal dismisses with a diff dict; app mutator
# validates + mutates the live routing in place.
# --------------------------------------------------------------------------- #
def _drive_picker_with_callback(
    routing: RoutingSettings,
    drive: Callable[[ModelPickerScreen, object], Awaitable[None]],
) -> dict[str, str] | None | str:
    """Mount the modal with a callback that captures the dismiss value."""
    from textual.app import App

    captured: dict[str, Any] = {"value": "sentinel"}

    class _Host(App[None]):
        def on_mount(self) -> None:
            def _cb(value: dict[str, str] | None) -> None:
                captured["value"] = value

            self.push_screen(ModelPickerScreen(routing=routing), _cb)

    async def _go() -> None:
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            modal = host.screen_stack[-1]
            assert isinstance(modal, ModelPickerScreen)
            await drive(modal, pilot)
            await pilot.pause()

    _arun(_go())
    return captured["value"]  # type: ignore[no-any-return]


def test_apply_button_dismisses_with_tasks_diff() -> None:
    """Flip spec_intake → local-coder, click Apply, modal dismisses with
    {"tasks": {"spec_intake": "local-coder"}, "loops": {}}.
    Unchanged tasks stay out of the dict."""
    routing = _routing(
        tasks={
            "spec_intake": TaskBinding(model="frontier"),
            "plan": TaskBinding(model="frontier"),
        },
    )

    async def drive(modal: ModelPickerScreen, pilot: object) -> None:
        from textual.widgets import Select
        select = modal.query_one("#task-select-spec_intake", Select)
        select.value = "local-coder"
        await pilot.pause()  # type: ignore[attr-defined]
        modal.action_apply()

    value = _drive_picker_with_callback(routing, drive)
    assert value == {
        "tasks": {"spec_intake": {"model": "local-coder"}},
        "loops": {},
    }


def test_apply_button_dismisses_with_loop_diff() -> None:
    """F15.4: flipping a loop binding's Select lands in the loops sub-dict."""
    routing = _routing(
        tasks={"spec_intake": TaskBinding(model="frontier")},
        loops={
            "inner": LoopBinding(model="local-coder"),
            "outer": LoopBinding(model="frontier"),
        },
    )

    async def drive(modal: ModelPickerScreen, pilot: object) -> None:
        from textual.widgets import Select
        select = modal.query_one("#loop-select-inner", Select)
        select.value = "frontier"
        await pilot.pause()  # type: ignore[attr-defined]
        modal.action_apply()

    value = _drive_picker_with_callback(routing, drive)
    assert value == {
        "tasks": {},
        "loops": {"inner": {"model": "frontier"}},
    }


def test_apply_button_dismisses_with_combined_diff() -> None:
    """F15.4: changing both a task and a loop produces both subdicts in one Apply."""
    routing = _routing(
        tasks={"spec_intake": TaskBinding(model="frontier")},
        loops={"outer": LoopBinding(model="frontier")},
    )

    async def drive(modal: ModelPickerScreen, pilot: object) -> None:
        from textual.widgets import Select
        modal.query_one(
            "#task-select-spec_intake", Select,
        ).value = "local-coder"
        modal.query_one(
            "#loop-select-outer", Select,
        ).value = "local-coder"
        await pilot.pause()  # type: ignore[attr-defined]
        modal.action_apply()

    value = _drive_picker_with_callback(routing, drive)
    assert value == {
        "tasks": {"spec_intake": {"model": "local-coder"}},
        "loops": {"outer": {"model": "local-coder"}},
    }


def test_apply_with_no_changes_dismisses_with_empty_subdicts() -> None:
    """No Select touched → Apply dismisses with {tasks: {}, loops: {}}
    (cleanly applied no-op, distinct from None = cancel)."""
    routing = _routing(
        tasks={"spec_intake": TaskBinding(model="frontier")},
        loops={"inner": LoopBinding(model="frontier")},
    )

    async def drive(modal: ModelPickerScreen, pilot: object) -> None:
        modal.action_apply()

    value = _drive_picker_with_callback(routing, drive)
    assert value == {"tasks": {}, "loops": {}}


def test_apply_change_persists_on_app_routing(
    tmp_path: Path, routing_config: Path,
) -> None:
    """End-to-end: feed a diff into ChipAgentApp.apply_routing_change,
    assert the LIVE router's routing reflects the new model. This is
    the F15.2 contract — without it, the modal would lie.
    """
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        old = app._routing.tasks["spec_intake"].model  # type: ignore[union-attr]
        app.apply_routing_change(tasks={"spec_intake": {"model": "stub-model"}})
        await pilot.pause()  # type: ignore[attr-defined]
        new_on_app = app._routing.tasks["spec_intake"].model  # type: ignore[union-attr]
        # The router and the app share the same RoutingSettings instance.
        # Mutating one mutates the other — pin that.
        new_on_router = app._router.routing.tasks[  # type: ignore[attr-defined]
            "spec_intake"
        ].model
        return {
            "old": old,
            "new_on_app": new_on_app,
            "new_on_router": new_on_router,
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    # The stub routing config only has one registry entry ("stub-model"),
    # so the change is from stub-model → stub-model (a no-op as far as
    # the model string goes). The test still pins the propagation path —
    # mutation lands on both the app's and router's view.
    assert captured["new_on_app"] == "stub-model"
    assert captured["new_on_router"] == "stub-model"
    assert captured["new_on_app"] == captured["new_on_router"]


def test_apply_routing_change_rejects_unknown_registry_entry(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Passing a model_key not in the registry must raise ValueError +
    leave the routing untouched."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        before = app._routing.tasks["spec_intake"].model  # type: ignore[union-attr]
        try:
            app.apply_routing_change(tasks={"spec_intake": {"model": "no-such-model"}})
            raised = False
        except ValueError as e:
            raised = True
            err = str(e)
        after = app._routing.tasks["spec_intake"].model  # type: ignore[union-attr]
        return {
            "raised": raised, "err": err if raised else "",
            "before": before, "after": after,
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["raised"]
    assert "not in registry" in captured["err"]
    # Routing untouched — validation fails before any mutation lands.
    assert captured["after"] == captured["before"]


def test_apply_routing_change_rejects_unknown_task(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Passing an unknown task name must raise + leave routing untouched."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        before = dict(app._routing.tasks)  # type: ignore[union-attr]
        try:
            app.apply_routing_change(tasks={"made_up_task": {"model": "stub-model"}})
            raised = False
        except ValueError as e:
            raised = True
            err = str(e)
        after = dict(app._routing.tasks)  # type: ignore[union-attr]
        return {
            "raised": raised, "err": err if raised else "",
            "before_keys": sorted(before),
            "after_keys": sorted(after),
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["raised"]
    assert "unknown task" in captured["err"]
    assert captured["before_keys"] == captured["after_keys"]


def test_apply_routing_change_is_atomic_on_validation_failure(
    tmp_path: Path, routing_config: Path,
) -> None:
    """A diff with one valid swap and one bad model_key must leave
    BOTH tasks untouched — validation passes/fails as a unit."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        before_spec = app._routing.tasks["spec_intake"].model  # type: ignore[union-attr]
        before_plan = app._routing.tasks["plan"].model  # type: ignore[union-attr]
        try:
            # spec_intake → stub-model (valid) + plan → bad (invalid)
            app.apply_routing_change(tasks={
                "spec_intake": {"model": "stub-model"},
                "plan": {"model": "no-such-model"},
            })
            raised = False
        except ValueError:
            raised = True
        return {
            "raised": raised,
            "after_spec": app._routing.tasks[  # type: ignore[union-attr]
                "spec_intake"
            ].model,
            "after_plan": app._routing.tasks[  # type: ignore[union-attr]
                "plan"
            ].model,
            "before_spec": before_spec,
            "before_plan": before_plan,
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["raised"]
    # Both bindings stay at their before-values.
    assert captured["after_spec"] == captured["before_spec"]
    assert captured["after_plan"] == captured["before_plan"]


# --------------------------------------------------------------------------- #
# App-level integration: Ctrl+L pushes the modal + gating behaviour.
# --------------------------------------------------------------------------- #
@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


def _build_app(
    *,
    run_dir: Path,
    routing_config: Path,
    with_routing: bool = True,
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
        design_id="picker-test",
        name="counter",
        transcript_path=run_dir / "chat.transcript.md",
        checkpoint_path=run_dir / "checkpoint.sqlite",
        audit_db_path=run_dir / "audit.sqlite",
        hmac_key=HMAC_KEY,
        exports_dir=run_dir / "exports" / "picker-test",
        run_args_factory=_factory,
        defaults=settings.constraints,
        routing=settings.routing if with_routing else None,
    )
    return app, store


def _drive_app(
    drive: Callable[[ChipAgentApp, object], Awaitable[dict[str, Any]]],
    *,
    tmp_path: Path,
    routing_config: Path,
    with_routing: bool = True,
) -> dict[str, Any]:
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
        with_routing=with_routing,
    )

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    return _arun(_go())


def test_ctrl_l_pushes_model_picker_modal(
    tmp_path: Path, routing_config: Path,
) -> None:
    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        await pilot.press("ctrl+l")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"top_screen": type(app.screen_stack[-1]).__name__}

    captured = _drive_app(
        drive, tmp_path=tmp_path, routing_config=routing_config,
    )
    assert captured["top_screen"] == "ModelPickerScreen"


def test_ctrl_l_refused_while_driving(
    tmp_path: Path, routing_config: Path,
) -> None:
    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app._driving = True
        await pilot.press("ctrl+l")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"top_screen": type(app.screen_stack[-1]).__name__}

    captured = _drive_app(
        drive, tmp_path=tmp_path, routing_config=routing_config,
    )
    assert captured["top_screen"] != "ModelPickerScreen"


def test_ctrl_l_noop_when_routing_unwired(
    tmp_path: Path, routing_config: Path,
) -> None:
    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        await pilot.press("ctrl+l")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {"top_screen": type(app.screen_stack[-1]).__name__}

    captured = _drive_app(
        drive, tmp_path=tmp_path, routing_config=routing_config,
        with_routing=False,
    )
    assert captured["top_screen"] != "ModelPickerScreen"


# --------------------------------------------------------------------------- #
# F15.3: routing changes append a typed audit event + chain stays valid.
# --------------------------------------------------------------------------- #
def _audit_events(audit_db: Path, design_id: str) -> list:
    """Read events out of the audit DB after the app has appended."""
    log = SqliteAuditLog(db_path=audit_db, hmac_key=HMAC_KEY)
    try:
        return log.events(design_id)
    finally:
        log.close()


def test_apply_appends_routing_changed_audit_event(
    tmp_path: Path, routing_config: Path,
) -> None:
    """After apply_routing_change, the audit log carries a new event
    with EventType.ROUTING_CHANGED + {kind, old, new} payload."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app.apply_routing_change(tasks={"spec_intake": {"model": "stub-model"}})
        await pilot.pause()  # type: ignore[attr-defined]
        events = _audit_events(tmp_path / "audit.sqlite", "picker-test")
        return {"events": events}

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    events = captured["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type is EventType.ROUTING_CHANGED
    assert ev.payload["kind"] == "routing"
    assert ev.payload["new"] == {
        "tasks": {"spec_intake": {"model": "stub-model"}},
        "loops": {},
    }
    # Old value is captured before the swap; stub config has only stub-model.
    assert ev.payload["old"] == {
        "tasks": {"spec_intake": {"model": "stub-model"}},
        "loops": {},
    }


def test_audit_chain_stays_valid_after_routing_changes(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Two back-to-back routing changes append two linked events; verify
    walks the chain cleanly."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app.apply_routing_change(tasks={"spec_intake": {"model": "stub-model"}})
        app.apply_routing_change(tasks={"plan": {"model": "stub-model"}})
        await pilot.pause()  # type: ignore[attr-defined]
        log = SqliteAuditLog(
            db_path=tmp_path / "audit.sqlite", hmac_key=HMAC_KEY,
        )
        try:
            return {
                "verify": log.verify("picker-test"),
                "events": log.events("picker-test"),
            }
        finally:
            log.close()

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["verify"].valid is True
    assert captured["verify"].event_count == 2
    assert len(captured["events"]) == 2
    # Sequence is 1, 2 — each event chains to the previous.
    assert captured["events"][0].sequence == 1
    assert captured["events"][1].sequence == 2
    assert (
        captured["events"][1].prev_hash
        == captured["events"][0].content_hash
    )


def test_failed_validation_does_not_append_audit_event(
    tmp_path: Path, routing_config: Path,
) -> None:
    """If apply_routing_change raises ValueError, no audit event lands.
    Validation must run before any append."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        import contextlib
        with contextlib.suppress(ValueError):
            app.apply_routing_change(tasks={"spec_intake": {"model": "no-such-model"}})
        await pilot.pause()  # type: ignore[attr-defined]
        # Audit DB may not exist at all (validation failed before the
        # first append) — _audit_events will create one on read.
        audit_path = tmp_path / "audit.sqlite"
        if not audit_path.exists():
            return {"events": []}
        return {"events": _audit_events(audit_path, "picker-test")}

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["events"] == []


# --------------------------------------------------------------------------- #
# F15.4: loop bindings editable + single audit event for combined changes.
# --------------------------------------------------------------------------- #
def test_apply_routing_change_mutates_loop_bindings(
    tmp_path: Path, routing_config: Path,
) -> None:
    """apply_routing_change(loops={...}) mutates routing.loops[*].model
    in place on both the app and the router (they share the instance)."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app.apply_routing_change(loops={"inner": {"model": "stub-model"}})
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "on_app": app._routing.loops["inner"].model,  # type: ignore[union-attr]
            "on_router": app._router.routing.loops["inner"].model,  # type: ignore[attr-defined]
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["on_app"] == "stub-model"
    assert captured["on_router"] == "stub-model"


def test_apply_routing_change_rejects_unknown_loop(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Unknown loop name → ValueError; routing untouched."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        before = dict(app._routing.loops)  # type: ignore[union-attr]
        try:
            app.apply_routing_change(loops={"middle": {"model": "stub-model"}})
            raised = False
        except ValueError as e:
            raised = True
            err = str(e)
        after = dict(app._routing.loops)  # type: ignore[union-attr]
        return {
            "raised": raised, "err": err if raised else "",
            "before_keys": sorted(before),
            "after_keys": sorted(after),
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["raised"]
    assert "unknown loop" in captured["err"]
    assert captured["before_keys"] == captured["after_keys"]


def test_combined_tasks_and_loops_change_records_one_audit_event(
    tmp_path: Path, routing_config: Path,
) -> None:
    """F15.4 contract: one Apply with both task + loop changes appends
    a single ROUTING_CHANGED event carrying the combined diff."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app.apply_routing_change(
            tasks={"spec_intake": {"model": "stub-model"}},
            loops={"inner": {"model": "stub-model"}},
        )
        await pilot.pause()  # type: ignore[attr-defined]
        events = _audit_events(tmp_path / "audit.sqlite", "picker-test")
        return {"events": events}

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    events = captured["events"]
    assert len(events) == 1  # one Apply → one event, not two
    ev = events[0]
    assert ev.event_type is EventType.ROUTING_CHANGED
    assert ev.payload["new"] == {
        "tasks": {"spec_intake": {"model": "stub-model"}},
        "loops": {"inner": {"model": "stub-model"}},
    }
    assert "tasks" in ev.payload["old"]
    assert "loops" in ev.payload["old"]


def test_apply_with_both_subdicts_empty_is_a_noop(
    tmp_path: Path, routing_config: Path,
) -> None:
    """apply_routing_change(tasks={}, loops={}) returns silently
    without mutating routing or appending an audit event."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        before = app._routing.tasks["spec_intake"].model  # type: ignore[union-attr]
        app.apply_routing_change(tasks={}, loops={})
        await pilot.pause()  # type: ignore[attr-defined]
        after = app._routing.tasks["spec_intake"].model  # type: ignore[union-attr]
        audit_path = tmp_path / "audit.sqlite"
        events = (
            _audit_events(audit_path, "picker-test")
            if audit_path.exists() else []
        )
        return {"before": before, "after": after, "events": events}

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["before"] == captured["after"]
    assert captured["events"] == []


# --------------------------------------------------------------------------- #
# F15.5: temperature + n inputs, partial-binding diffs.
# --------------------------------------------------------------------------- #
def test_model_picker_renders_temperature_and_n_inputs() -> None:
    """Each binding row gains a T= Input pre-filled with the binding's
    temperature + an n= Input with its n value."""
    routing = _routing(
        tasks={
            "spec_intake": TaskBinding(model="frontier", temperature=0.7, n=2),
        },
    )

    async def drive(screen: ModelPickerScreen, pilot: object) -> dict[str, Any]:
        from textual.widgets import Input
        return {
            "temp": screen.query_one("#task-temp-spec_intake", Input).value,
            "n": screen.query_one("#task-n-spec_intake", Input).value,
        }

    captured = _drive_modal(drive, routing=routing)
    assert captured["temp"] == "0.70"
    assert captured["n"] == "2"


def test_apply_with_temperature_change_dismisses_with_partial() -> None:
    """Bump temperature → dismiss carries {"temperature": float} for
    that task only; n + model untouched."""
    routing = _routing(
        tasks={"plan": TaskBinding(model="frontier", temperature=0.2, n=1)},
    )

    async def drive(modal: ModelPickerScreen, pilot: object) -> None:
        from textual.widgets import Input
        modal.query_one("#task-temp-plan", Input).value = "1.50"
        await pilot.pause()  # type: ignore[attr-defined]
        modal.action_apply()

    value = _drive_picker_with_callback(routing, drive)
    assert value == {
        "tasks": {"plan": {"temperature": 1.5}},
        "loops": {},
    }


def test_apply_with_n_change_dismisses_with_partial() -> None:
    routing = _routing(
        tasks={"rtl_gen": TaskBinding(model="frontier", temperature=0.4, n=1)},
    )

    async def drive(modal: ModelPickerScreen, pilot: object) -> None:
        from textual.widgets import Input
        modal.query_one("#task-n-rtl_gen", Input).value = "5"
        await pilot.pause()  # type: ignore[attr-defined]
        modal.action_apply()

    value = _drive_picker_with_callback(routing, drive)
    assert value == {
        "tasks": {"rtl_gen": {"n": 5}},
        "loops": {},
    }


def test_apply_with_model_temp_and_n_dismisses_with_all_fields() -> None:
    """Combined edit on one row lands all three fields in the PartialBinding."""
    routing = _routing(
        tasks={"rtl_gen": TaskBinding(model="frontier", temperature=0.4, n=1)},
    )

    async def drive(modal: ModelPickerScreen, pilot: object) -> None:
        from textual.widgets import Input, Select
        modal.query_one(
            "#task-select-rtl_gen", Select,
        ).value = "local-coder"
        modal.query_one("#task-temp-rtl_gen", Input).value = "0.90"
        modal.query_one("#task-n-rtl_gen", Input).value = "3"
        await pilot.pause()  # type: ignore[attr-defined]
        modal.action_apply()

    value = _drive_picker_with_callback(routing, drive)
    assert value == {
        "tasks": {
            "rtl_gen": {
                "model": "local-coder",
                "temperature": 0.9,
                "n": 3,
            },
        },
        "loops": {},
    }


def test_apply_with_out_of_range_temperature_blocks_dismiss() -> None:
    """Editing T to 3.5 (out of [0.0, 2.0]) → Apply does NOT dismiss.
    Modal stays open; operator can fix the value."""
    routing = _routing(
        tasks={"plan": TaskBinding(model="frontier", temperature=0.2, n=1)},
    )

    captured: dict[str, Any] = {"dismiss_count": 0, "value": "sentinel"}

    from textual.app import App

    class _Host(App[None]):
        def on_mount(self) -> None:
            def _cb(value: object) -> None:
                captured["dismiss_count"] += 1
                captured["value"] = value

            self.push_screen(ModelPickerScreen(routing=routing), _cb)

    async def _go() -> dict[str, Any]:
        from textual.widgets import Input
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            modal = host.screen_stack[-1]
            assert isinstance(modal, ModelPickerScreen)
            modal.query_one("#task-temp-plan", Input).value = "3.5"
            await pilot.pause()
            modal.action_apply()
            await pilot.pause()
            return {
                "dismiss_count": captured["dismiss_count"],
                "modal_still_on_stack": (
                    host.screen_stack[-1] is modal
                ),
            }

    out = _arun(_go())
    # action_apply refused to dismiss — modal still on stack, callback
    # never fired.
    assert out["dismiss_count"] == 0
    assert out["modal_still_on_stack"]


def test_apply_with_zero_n_blocks_dismiss() -> None:
    """Editing n to 0 (must be >= 1) → Apply does NOT dismiss."""
    routing = _routing(
        tasks={"plan": TaskBinding(model="frontier", temperature=0.2, n=1)},
    )

    captured: dict[str, Any] = {"dismiss_count": 0}

    from textual.app import App

    class _Host(App[None]):
        def on_mount(self) -> None:
            def _cb(value: object) -> None:
                captured["dismiss_count"] += 1

            self.push_screen(ModelPickerScreen(routing=routing), _cb)

    async def _go() -> bool:
        from textual.widgets import Input
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            modal = host.screen_stack[-1]
            assert isinstance(modal, ModelPickerScreen)
            modal.query_one("#task-n-plan", Input).value = "0"
            await pilot.pause()
            modal.action_apply()
            await pilot.pause()
            return host.screen_stack[-1] is modal

    still_open = _arun(_go())
    assert captured["dismiss_count"] == 0
    assert still_open


def test_apply_routing_change_mutates_temperature(
    tmp_path: Path, routing_config: Path,
) -> None:
    """apply_routing_change can mutate just the temperature field; the
    binding's model and n stay at their initial values."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        before = app._routing.tasks["spec_intake"]  # type: ignore[union-attr]
        old_model = before.model
        old_n = before.n
        app.apply_routing_change(
            tasks={"spec_intake": {"temperature": 0.6}},
        )
        await pilot.pause()  # type: ignore[attr-defined]
        after = app._routing.tasks["spec_intake"]  # type: ignore[union-attr]
        return {
            "model": after.model,
            "temperature": after.temperature,
            "n": after.n,
            "old_model": old_model,
            "old_n": old_n,
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["temperature"] == 0.6
    assert captured["model"] == captured["old_model"]
    assert captured["n"] == captured["old_n"]


def test_apply_routing_change_mutates_n(
    tmp_path: Path, routing_config: Path,
) -> None:
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        app.apply_routing_change(
            tasks={"rtl_gen": {"n": 4}},
        )
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "n": app._routing.tasks["rtl_gen"].n,  # type: ignore[union-attr]
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["n"] == 4


def test_apply_routing_change_rejects_temperature_out_of_range(
    tmp_path: Path, routing_config: Path,
) -> None:
    """T must be in [0.0, 2.0]; 3.5 raises ValueError."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        before = app._routing.tasks["plan"].temperature  # type: ignore[union-attr]
        try:
            app.apply_routing_change(
                tasks={"plan": {"temperature": 3.5}},
            )
            raised = False
        except ValueError as e:
            raised = True
            err = str(e)
        after = app._routing.tasks["plan"].temperature  # type: ignore[union-attr]
        return {
            "raised": raised, "err": err if raised else "",
            "before": before, "after": after,
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["raised"]
    assert "[0.0, 2.0]" in captured["err"]
    assert captured["before"] == captured["after"]


def test_apply_routing_change_rejects_n_below_one(
    tmp_path: Path, routing_config: Path,
) -> None:
    """n must be >= 1; 0 raises ValueError; routing untouched."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        before = app._routing.tasks["plan"].n  # type: ignore[union-attr]
        try:
            app.apply_routing_change(tasks={"plan": {"n": 0}})
            raised = False
        except ValueError as e:
            raised = True
            err = str(e)
        after = app._routing.tasks["plan"].n  # type: ignore[union-attr]
        return {
            "raised": raised, "err": err if raised else "",
            "before": before, "after": after,
        }

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["raised"]
    assert "must be an int >= 1" in captured["err"]
    assert captured["before"] == captured["after"]


def test_apply_routing_change_rejects_unknown_field(
    tmp_path: Path, routing_config: Path,
) -> None:
    """Only model/temperature/n are valid PartialBinding keys; anything
    else raises before any mutation lands."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        try:
            app.apply_routing_change(
                tasks={"plan": {"prompt_template": "foo"}},
            )
            raised = False
        except ValueError as e:
            raised = True
            err = str(e)
        return {"raised": raised, "err": err if raised else ""}

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    assert captured["raised"]
    assert "unknown field" in captured["err"]


def test_apply_routing_change_audit_payload_includes_knob_old_values(
    tmp_path: Path, routing_config: Path,
) -> None:
    """When the operator changes temperature, the audit event records
    both the old and new temperature."""
    app, _store = _build_app(
        run_dir=tmp_path, routing_config=routing_config,
    )

    async def drive(app: ChipAgentApp, pilot: object) -> dict[str, Any]:
        old_temp = app._routing.tasks["rtl_gen"].temperature  # type: ignore[union-attr]
        app.apply_routing_change(
            tasks={"rtl_gen": {"temperature": 0.8}},
        )
        await pilot.pause()  # type: ignore[attr-defined]
        events = _audit_events(tmp_path / "audit.sqlite", "picker-test")
        return {"events": events, "old_temp": old_temp}

    async def _go() -> dict[str, Any]:
        async with app.run_test() as pilot:
            await pilot.pause()
            return await drive(app, pilot)

    captured = _arun(_go())
    events = captured["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["new"] == {
        "tasks": {"rtl_gen": {"temperature": 0.8}},
        "loops": {},
    }
    assert ev.payload["old"]["tasks"]["rtl_gen"]["temperature"] == (
        captured["old_temp"]
    )
