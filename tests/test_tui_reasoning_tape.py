"""F22.2-D acceptance: ReasoningTape pane + JSONL tail reader.

The tape is the operator-visible payoff of F22.2 — every MODEL /
TOOL / STAGE span the spine produces shows up here within 500 ms of
the JsonlTracer flushing it to disk.

Tests cover three layers:

* Pure helpers (``_format_span_row``, ``_format_filter_label``,
  ``_matches_filter``) — easy to unit-test without an App.
* JSONL tail reader (``read_reasoning_tail``) — round-trip a
  hand-written JSONL through the parser; pin offset advancement.
* End-to-end pane mount via ``App.run_test`` — drive
  ``apply_events`` + ``action_cycle_filter`` and assert what the
  RichLog shows.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chip_agent.obs.jsonl_tracer import (
    JsonlTracer,
    span_record_to_jsonl_line,
)
from chip_agent.obs.tracing import (
    SpanKind,
    SpanRecord,
    SpanStatus,
)
from chip_agent.tui.panes.reasoning_tape import (
    ReasoningTape,
    _format_filter_label,
    _format_model_row,
    _format_span_row,
    _format_tool_row,
    _matches_filter,
)
from chip_agent.tui.workers.poll_worker import (
    ReasoningTailState,
    read_reasoning_tail,
)


# --------------------------------------------------------------------------- #
# Pure formatters.
# --------------------------------------------------------------------------- #
def test_format_filter_label_says_what_each_cycle_does() -> None:
    assert "ALL" in _format_filter_label("ALL")
    assert "Ctrl+T" in _format_filter_label("ALL")
    assert "MODEL" in _format_filter_label("ALL")


@pytest.mark.parametrize(
    "active,kind,expect",
    [
        ("ALL", SpanKind.MODEL, True),
        ("ALL", SpanKind.STAGE, True),
        ("MODEL", SpanKind.MODEL, True),
        ("MODEL", SpanKind.TOOL, False),
        ("TOOL", SpanKind.TOOL, True),
        ("TOOL", SpanKind.MODEL, False),
        ("TOOL", SpanKind.STAGE, False),
    ],
)
def test_matches_filter(active: str, kind: SpanKind, expect: bool) -> None:
    rec = _record(kind=kind)
    assert _matches_filter(rec, active) is expect


def test_format_model_row_surfaces_model_id_tokens_cost() -> None:
    rec = _record(
        kind=SpanKind.MODEL,
        attributes={"model_id": "claude-opus-4-7"},
        duration_ms=1234.0,
        prompt_tokens=100, completion_tokens=50, cost_usd=0.012,
    )
    row = _format_model_row(rec, ts="12:34:56")
    assert "MODEL" in row
    assert "claude-opus-4-7" in row
    assert "1.2s" in row
    assert "100 in / 50 out" in row
    assert "$0.0120" in row


def test_format_model_row_handles_missing_cost() -> None:
    rec = _record(
        kind=SpanKind.MODEL,
        attributes={"model_id": "qwen"},
        duration_ms=500.0,
        prompt_tokens=None, completion_tokens=None, cost_usd=None,
    )
    row = _format_model_row(rec, ts="12:34:56")
    assert "$" not in row  # no cost rendered
    assert "0 in / 0 out" in row


def test_format_tool_row_surfaces_tool_name_pass_fail() -> None:
    rec = _record(
        kind=SpanKind.TOOL,
        attributes={"tool_name": "verible", "passed": True},
        duration_ms=300.0,
    )
    row = _format_tool_row(rec, ts="12:34:58")
    assert "TOOL verible" in row
    assert "0.3s" in row
    assert "passed" in row


def test_format_tool_row_flags_failed_when_passed_false() -> None:
    rec = _record(
        kind=SpanKind.TOOL,
        attributes={"tool_name": "verilator", "passed": False},
        duration_ms=200.0,
    )
    row = _format_tool_row(rec, ts="12:34:58")
    assert "FAILED" in row


def test_format_span_row_dispatches_by_kind() -> None:
    model = _record(kind=SpanKind.MODEL, attributes={"model_id": "x"})
    tool = _record(kind=SpanKind.TOOL, attributes={"tool_name": "y"})
    stage = _record(kind=SpanKind.STAGE, name="stage:rtl")
    assert "MODEL" in _format_span_row(model)
    assert "TOOL" in _format_span_row(tool)
    # STAGE rows just print kind + name so the operator sees the boundary.
    assert "stage" in _format_span_row(stage).lower()
    assert "stage:rtl" in _format_span_row(stage)


# --------------------------------------------------------------------------- #
# JSONL tail reader — the core IO contract.
# --------------------------------------------------------------------------- #
def test_tail_reader_returns_empty_for_missing_file(tmp_path: Path) -> None:
    state = ReasoningTailState()
    assert read_reasoning_tail(tmp_path / "no-such.jsonl", state) == []
    assert state.offset == 0


def test_tail_reader_returns_empty_when_no_new_bytes(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    path.write_text(span_record_to_jsonl_line(_record()))
    state = ReasoningTailState()
    first = read_reasoning_tail(path, state)
    assert len(first) == 1
    # Second call with no new bytes returns empty.
    second = read_reasoning_tail(path, state)
    assert second == []


def test_tail_reader_picks_up_appended_lines(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    path.write_text(span_record_to_jsonl_line(_record(name="first")))
    state = ReasoningTailState()
    first = read_reasoning_tail(path, state)
    assert [r.name for r in first] == ["first"]
    # Append two more lines.
    with path.open("a") as fh:
        fh.write(span_record_to_jsonl_line(_record(name="second")))
        fh.write(span_record_to_jsonl_line(_record(name="third")))
    new = read_reasoning_tail(path, state)
    assert [r.name for r in new] == ["second", "third"]


def test_tail_reader_round_trips_full_span_fields(tmp_path: Path) -> None:
    """The tail reader must reconstruct every SpanRecord field the
    writer set, so the pane's formatters get full attribute data."""
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with tracer.run("d"), tracer.span("model:claude", kind=SpanKind.MODEL) as s:
        s.set_attribute("model_id", "claude-opus-4-7")
        s.set_attribute("prompt", "hi")
        s.set_tokens(prompt=100, completion=50)
        s.set_cost(0.012)
    state = ReasoningTailState()
    records = read_reasoning_tail(path, state)
    assert len(records) == 2  # MODEL + RUN (both closed by run() exit)
    model = next(r for r in records if r.kind is SpanKind.MODEL)
    assert model.attributes["model_id"] == "claude-opus-4-7"
    assert model.prompt_tokens == 100
    assert model.completion_tokens == 50
    assert model.cost_usd == 0.012
    assert model.status is SpanStatus.OK


def test_tail_reader_skips_malformed_json_lines(tmp_path: Path) -> None:
    """A crash-mid-write left an invalid line. The reader drops it
    silently and keeps reading subsequent valid lines instead of
    raising into the TUI."""
    path = tmp_path / "reasoning.jsonl"
    with path.open("w") as fh:
        fh.write(span_record_to_jsonl_line(_record(name="ok-before")))
        fh.write("garbage that isn't json\n")
        fh.write(span_record_to_jsonl_line(_record(name="ok-after")))
    state = ReasoningTailState()
    records = read_reasoning_tail(path, state)
    assert [r.name for r in records] == ["ok-before", "ok-after"]


def test_tail_reader_holds_residual_partial_line(tmp_path: Path) -> None:
    """A write that hasn't flushed its newline yet leaves a partial
    line. The reader buffers it and emits the full record once the
    newline lands on the next tick."""
    path = tmp_path / "reasoning.jsonl"
    full_line = span_record_to_jsonl_line(_record(name="streamed"))
    # Write everything except the trailing newline.
    path.write_text(full_line[:-1])
    state = ReasoningTailState()
    first = read_reasoning_tail(path, state)
    assert first == []
    assert state.residual.startswith('{"span_id"')  # buffered
    # Finish the line on disk.
    with path.open("a") as fh:
        fh.write("\n")
    second = read_reasoning_tail(path, state)
    assert len(second) == 1
    assert second[0].name == "streamed"


# --------------------------------------------------------------------------- #
# End-to-end: mount the pane in App.run_test.
# --------------------------------------------------------------------------- #
def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def _drive(
    drive: Callable[[ReasoningTape, object], Awaitable[dict[str, Any]]],
    *,
    jsonl_path: Path,
) -> dict[str, Any]:
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield ReasoningTape(jsonl_path=jsonl_path)

    async def _go() -> dict[str, Any]:
        app = _Host()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            pane = app.query_one(ReasoningTape)
            return await drive(pane, pilot)

    return _arun(_go())


def test_pane_starts_empty(tmp_path: Path) -> None:
    """No JSONL on disk → pane shows the idle hint, no events buffered."""
    from textual.widgets import Static

    async def drive(pane: ReasoningTape, pilot: object) -> dict[str, Any]:
        await pilot.pause()  # type: ignore[attr-defined]
        label = pane.query_one("#tape-filter", Static)
        return {"filter": str(label.renderable), "buffered": len(pane._buffered)}

    captured = _drive(drive, jsonl_path=tmp_path / "no-such.jsonl")
    assert "ALL" in captured["filter"]
    assert captured["buffered"] == 0


def test_apply_events_buffers_and_renders(tmp_path: Path) -> None:
    """Direct apply_events buffers + renders rows for events that pass
    the active filter (default ALL)."""
    async def drive(pane: ReasoningTape, pilot: object) -> dict[str, Any]:
        pane.apply_events([
            _record(kind=SpanKind.MODEL, attributes={"model_id": "claude"}),
            _record(kind=SpanKind.TOOL, attributes={"tool_name": "verible"}),
            _record(kind=SpanKind.STAGE, name="stage:rtl"),
        ])
        await pilot.pause()  # type: ignore[attr-defined]
        return {"buffered": len(pane._buffered)}

    captured = _drive(drive, jsonl_path=tmp_path / "x.jsonl")
    assert captured["buffered"] == 3


def test_ctrl_t_cycles_filter(tmp_path: Path) -> None:
    """Three cycles of action_cycle_filter walks ALL → MODEL → TOOL → ALL."""
    from textual.widgets import Static

    async def drive(pane: ReasoningTape, pilot: object) -> dict[str, Any]:
        labels: list[str] = []
        labels.append(str(pane.query_one("#tape-filter", Static).renderable))
        pane.action_cycle_filter()
        await pilot.pause()  # type: ignore[attr-defined]
        labels.append(str(pane.query_one("#tape-filter", Static).renderable))
        pane.action_cycle_filter()
        await pilot.pause()  # type: ignore[attr-defined]
        labels.append(str(pane.query_one("#tape-filter", Static).renderable))
        pane.action_cycle_filter()
        await pilot.pause()  # type: ignore[attr-defined]
        labels.append(str(pane.query_one("#tape-filter", Static).renderable))
        return {"labels": labels}

    captured = _drive(drive, jsonl_path=tmp_path / "x.jsonl")
    # The 4 labels should be: ALL, MODEL, TOOL, ALL.
    assert "ALL" in captured["labels"][0]
    assert "MODEL" in captured["labels"][1]
    assert "TOOL" in captured["labels"][2]
    assert "ALL" in captured["labels"][3]


def test_polling_picks_up_jsonl_writes(tmp_path: Path) -> None:
    """End-to-end: write to the JSONL on disk, force one poll tick,
    assert the pane buffered the new records."""
    jsonl = tmp_path / "reasoning.jsonl"

    async def drive(pane: ReasoningTape, pilot: object) -> dict[str, Any]:
        # Write 2 records to disk.
        jsonl.write_text(
            span_record_to_jsonl_line(_record(name="r1"))
            + span_record_to_jsonl_line(_record(name="r2"))
        )
        # Force one poll tick.
        pane._poll_once()
        await pilot.pause()  # type: ignore[attr-defined]
        return {"buffered": len(pane._buffered)}

    captured = _drive(drive, jsonl_path=jsonl)
    assert captured["buffered"] == 2


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _record(
    *,
    name: str = "model:test",
    kind: SpanKind = SpanKind.MODEL,
    attributes: dict[str, Any] | None = None,
    duration_ms: float | None = 100.0,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
) -> SpanRecord:
    return SpanRecord(
        span_id="abc",
        parent_id=None,
        name=name,
        kind=kind,
        design_id="d",
        started_at=datetime(2026, 6, 17, 12, 34, 56, tzinfo=UTC),
        ended_at=datetime(2026, 6, 17, 12, 34, 57, tzinfo=UTC),
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        status=SpanStatus.OK,
        attributes=attributes or {},
    )


def test_jsonl_round_trip_via_payload(tmp_path: Path) -> None:
    """Smoke: writer line + parser produce a record with matching fields."""
    rec = _record(
        kind=SpanKind.MODEL,
        attributes={"model_id": "claude"},
        prompt_tokens=100, completion_tokens=50, cost_usd=0.012,
    )
    line = span_record_to_jsonl_line(rec)
    payload = json.loads(line)
    assert payload["kind"] == "model"
    assert payload["prompt_tokens"] == 100
    assert payload["attributes"]["model_id"] == "claude"
