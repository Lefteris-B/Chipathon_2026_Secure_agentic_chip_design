"""F22.1-C — ActivityBanner widget tests.

Covers the pure formatters (``_render_now_line``, ``_render_last_line``,
``_format_age``) plus end-to-end rendering through ``App.run_test()`` so
a future Textual upgrade can't drift the widget tree silently.

The "30 s silent hint" branch is exercised by a pure-function test that
feeds a high ``age_s`` plus a non-terminal DesignStatus — no need to
sleep in the test harness.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from chip_agent.design_state import (
    DesignConstraints,
    DesignState,
    DesignStatus,
    ModuleState,
    Stage,
    StageState,
    StageStatus,
)
from chip_agent.obs.audit_log import AuditEvent, EventType
from chip_agent.tui.panes.activity_banner import (
    ActivityBanner,
    _format_age,
    _render_last_line,
    _render_now_line,
)


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def _state(
    *,
    status: DesignStatus = DesignStatus.PLANNING,
    current_stage: Stage = Stage.SPEC,
    stages: dict[Stage, StageState] | None = None,
    modules: dict[str, ModuleState] | None = None,
) -> DesignState:
    return DesignState(
        design_id="d", name="d",
        constraints=DesignConstraints(),
        status=status, current_stage=current_stage,
        stages=stages or {},
        modules=modules or {},
    )


def _ev(event_type: EventType, payload: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        design_id="d", sequence=1,
        timestamp=datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
        event_type=event_type, payload=payload,
        prev_hash="GENESIS", content_hash="sha256:00",
        signature="hmac-sha256:00",
    )


# --------------------------------------------------------------------------- #
# Pure formatter tests — easy to unit-test without the App.
# --------------------------------------------------------------------------- #
def test_render_now_line_pending_stage() -> None:
    line = _render_now_line(_state(current_stage=Stage.RTL))
    assert line == "Now: RTL pending"


def test_render_now_line_running_stage_from_design_level() -> None:
    line = _render_now_line(_state(
        current_stage=Stage.SYNTH,
        stages={Stage.SYNTH: StageState(
            stage=Stage.SYNTH, status=StageStatus.IN_PROGRESS,
        )},
    ))
    assert line == "Now: SYNTH running"


def test_render_now_line_aggregates_worst_across_modules() -> None:
    """Per-module stage (RTL/M19): the banner reads the worst status
    across modules so a single failing module surfaces — matches the
    pipeline pane's cell-colour aggregation."""
    state = _state(
        current_stage=Stage.RTL,
        modules={
            "a": ModuleState(
                module_id="a", name="a",
                stages={Stage.RTL: StageState(
                    stage=Stage.RTL, status=StageStatus.PASSED,
                )},
            ),
            "b": ModuleState(
                module_id="b", name="b",
                stages={Stage.RTL: StageState(
                    stage=Stage.RTL, status=StageStatus.FAILED,
                )},
            ),
        },
    )
    assert _render_now_line(state) == "Now: RTL failed"


def test_render_now_line_design_status_overrides_per_stage() -> None:
    """AWAITING_HUMAN / COMPLETED / FAILED design-level words override
    the per-stage StageState word — they're the operator-action-relevant
    summary."""
    line = _render_now_line(_state(
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.SIGNOFF,
        stages={Stage.SIGNOFF: StageState(
            stage=Stage.SIGNOFF, status=StageStatus.PASSED,
        )},
    ))
    assert line == "Now: SIGNOFF awaiting human"


def test_render_now_line_design_status_completed() -> None:
    assert _render_now_line(_state(
        status=DesignStatus.COMPLETED, current_stage=Stage.GDSII,
    )) == "Now: GDSII completed"


def test_render_now_line_m19_stage_pending_when_no_modules() -> None:
    """M19 stage with no module state → pending (matches the pipeline
    pane's behaviour for fast-pathed designs)."""
    assert _render_now_line(_state(
        current_stage=Stage.ORACLE,
    )) == "Now: ORACLE pending"


def test_render_now_line_m19_stage_running_via_module() -> None:
    state = _state(
        current_stage=Stage.ORACLE,
        modules={
            "counter": ModuleState(
                module_id="counter", name="counter",
                stages={Stage.ORACLE: StageState(
                    stage=Stage.ORACLE, status=StageStatus.IN_PROGRESS,
                )},
            ),
        },
    )
    assert _render_now_line(state) == "Now: ORACLE running"


# --------------------------------------------------------------------------- #
# _render_last_line uses _payload_summary (extended in F22.1-B).
# --------------------------------------------------------------------------- #
def test_render_last_line_includes_summary_and_age() -> None:
    line = _render_last_line(
        _ev(EventType.M19_FAST_PATH_DECISION, {
            "module_id": "counter", "m19_fast_path_used": False,
            "port_count": 4, "has_clock_or_reset": True, "max_ports": 2,
        }),
        age_s=3.4,
        state=_state(current_stage=Stage.CONTRACT),
    )
    assert "M19 fast-path: counter skipped" in line
    assert "3s ago" in line
    assert "silent" not in line  # below threshold


def test_render_last_line_appends_silent_hint_after_threshold() -> None:
    """≥30s + non-terminal status → silent-state hint."""
    line = _render_last_line(
        _ev(EventType.M19_FAST_PATH_DECISION, {
            "module_id": "counter", "m19_fast_path_used": True,
        }),
        age_s=45.0,
        state=_state(current_stage=Stage.ORACLE),
    )
    assert "silent — agent may be running" in line


def test_render_last_line_no_silent_hint_at_awaiting_human() -> None:
    """Silence at AWAITING_HUMAN is expected — the spine is correctly
    waiting on the operator, not an agent. No hint."""
    line = _render_last_line(
        _ev(EventType.HUMAN_DECISION, {"decision": "pending"}),
        age_s=600.0,
        state=_state(
            status=DesignStatus.AWAITING_HUMAN,
            current_stage=Stage.SIGNOFF,
        ),
    )
    assert "silent" not in line


def test_render_last_line_no_silent_hint_when_state_unknown() -> None:
    """Defensive: a None state can't be classified as terminal-or-not, so
    we suppress the hint rather than mislead."""
    line = _render_last_line(
        _ev(EventType.HUMAN_DECISION, {"decision": "pending"}),
        age_s=600.0,
        state=None,
    )
    assert "silent" not in line


def test_render_last_line_falls_back_to_event_type_when_no_summary() -> None:
    """Empty payload → no summary → use event_type.value so the row
    still shows something readable."""
    line = _render_last_line(
        _ev(EventType.GATE_DECISION, {}),
        age_s=1.0,
        state=_state(),
    )
    assert "gate_decision" in line.lower()


# --------------------------------------------------------------------------- #
# _format_age.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.4, "just now"),
        (0.0, "just now"),
        (1.0, "1s ago"),
        (3.4, "3s ago"),
        (59.9, "59s ago"),
        (60.0, "1m 0s ago"),
        (75.0, "1m 15s ago"),
        (3725.0, "62m 5s ago"),  # >1h still in minutes; honest about scope
    ],
)
def test_format_age_shapes(seconds: float, expected: str) -> None:
    assert _format_age(seconds) == expected


# --------------------------------------------------------------------------- #
# End-to-end: mount the banner in a minimal App and drive it via the
# public update_from_state / apply_events methods.
# --------------------------------------------------------------------------- #
def _drive(
    drive: Callable[[ActivityBanner, object], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield ActivityBanner()

    async def _go() -> dict[str, Any]:
        app = _Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.query_one(ActivityBanner)
            return await drive(banner, pilot)

    return _arun(_go())


def test_banner_initial_empty_state() -> None:
    """Fresh banner shows '—' placeholders before any messages land."""
    from textual.widgets import Static

    async def drive(banner: ActivityBanner, pilot: object) -> dict[str, Any]:
        now = banner.query_one("#activity-now", Static)
        last = banner.query_one("#activity-last", Static)
        return {"now": str(now.renderable), "last": str(last.renderable)}

    captured = _drive(drive)
    assert captured["now"] == "Now: —"
    assert captured["last"] == "Last: —"


def test_banner_updates_now_line_on_state() -> None:
    from textual.widgets import Static

    target = _state(
        current_stage=Stage.RTL,
        modules={
            "counter": ModuleState(
                module_id="counter", name="counter",
                stages={Stage.RTL: StageState(
                    stage=Stage.RTL, status=StageStatus.IN_PROGRESS,
                )},
            ),
        },
    )

    async def drive(banner: ActivityBanner, pilot: object) -> dict[str, Any]:
        banner.update_from_state(target)
        await pilot.pause()  # type: ignore[attr-defined]
        now = banner.query_one("#activity-now", Static)
        return {"now": str(now.renderable)}

    captured = _drive(drive)
    assert captured["now"] == "Now: RTL running"


def test_banner_updates_last_line_on_events() -> None:
    from textual.widgets import Static

    async def drive(banner: ActivityBanner, pilot: object) -> dict[str, Any]:
        banner.update_from_state(_state(current_stage=Stage.PHYSICAL))
        banner.apply_events([
            _ev(EventType.PHYSICAL_REPAIR_ROUTED, {
                "kind": "relax_clock_period", "reason": "...", "attempt": 2,
            }),
        ])
        await pilot.pause()  # type: ignore[attr-defined]
        last = banner.query_one("#activity-last", Static)
        return {"last": str(last.renderable)}

    captured = _drive(drive)
    assert "physical repair (attempt 2): relax_clock_period" in captured["last"]
    assert "ago" in captured["last"] or "just now" in captured["last"]


def test_banner_empty_event_batch_is_noop() -> None:
    """An empty events list doesn't disturb the Last line."""
    from textual.widgets import Static

    async def drive(banner: ActivityBanner, pilot: object) -> dict[str, Any]:
        banner.apply_events([])
        await pilot.pause()  # type: ignore[attr-defined]
        last = banner.query_one("#activity-last", Static)
        return {"last": str(last.renderable)}

    captured = _drive(drive)
    assert captured["last"] == "Last: —"
