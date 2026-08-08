"""Reasoning tape pane (F22.2-D).

The "what is the agent thinking" surface. Tails the per-run
``runs/<design_id>/reasoning.jsonl`` file written by
:class:`~chip_agent.obs.jsonl_tracer.JsonlTracer` and renders one line
per closed :class:`SpanRecord` — every LLM prompt+completion, every
typed tool service invocation, every stage/run boundary.

Polls at 500 ms on the main thread (same cadence + threading as the
AuditPane — see ``chip_agent/tui/panes/audit.py:89,104,123,142``) so
the single-writer (run worker thread)/single-reader (TUI main thread)
contract stays simple. Tail strategy: track a byte offset into the
JSONL, ``seek(offset).read()`` the new chunk, split on newlines.

A ``Ctrl+T`` keybind cycles the kind filter between ALL → MODEL →
TOOL → ALL so the operator can hide STAGE/RUN scaffolding and read
just the agent calls, or just the tool calls. The filter is a pure
display filter — the underlying JSONL stays complete.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from chip_agent.obs.tracing import SpanKind, SpanRecord
from chip_agent.tui.messages import ReasoningEventBatch
from chip_agent.tui.workers.poll_worker import (
    ReasoningTailState,
    read_reasoning_tail,
)

__all__ = ["ReasoningTape"]


_IDLE_LABEL = "Reasoning: no events yet — start a run."
_FILTER_LABELS: tuple[str, ...] = ("ALL", "MODEL", "TOOL")


class ReasoningTape(Vertical):
    """Streaming view of the JSONL reasoning log.

    Layout (top to bottom):

    * ``#tape-filter`` — single-line static showing the current
      :class:`SpanKind` filter; cycled by ``Ctrl+T``.
    * ``#tape-log`` — scrollable :class:`RichLog` of one row per span,
      newest at the bottom.
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+t", "cycle_filter", "Reasoning filter"),
    ]

    DEFAULT_CSS = """
    ReasoningTape {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
    }

    ReasoningTape > #tape-filter {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    ReasoningTape > #tape-log {
        height: 1fr;
        background: $surface;
    }
    """

    DEFAULT_POLL_MS: ClassVar[int] = 500

    def __init__(
        self,
        *,
        jsonl_path: Path,
        poll_ms: int = DEFAULT_POLL_MS,
    ) -> None:
        super().__init__()
        self.jsonl_path = jsonl_path
        self.poll_ms = poll_ms
        self._tail_state = ReasoningTailState()
        self._filter_idx = 0  # index into _FILTER_LABELS
        self._buffered: list[SpanRecord] = []

    def compose(self) -> ComposeResult:
        yield Static(
            _format_filter_label(_FILTER_LABELS[self._filter_idx]),
            id="tape-filter",
        )
        yield RichLog(
            id="tape-log", wrap=False, highlight=False, markup=False,
        )

    def on_mount(self) -> None:
        # Eager first poll so a resumed run's existing log renders
        # immediately instead of waiting one ``poll_ms`` interval.
        self._poll_once()
        self.set_interval(self.poll_ms / 1000, self._poll_once)
        # Show a friendly idle hint when the file doesn't exist yet.
        if not self.jsonl_path.exists():
            self.query_one("#tape-log", RichLog).write(_IDLE_LABEL)

    # ---------------------------------------------------------------- polling
    def _poll_once(self) -> None:
        """One tick — pull new spans, post a batch message to ourselves."""
        new = read_reasoning_tail(self.jsonl_path, self._tail_state)
        if not new:
            return
        self.post_message(ReasoningEventBatch(events=new))

    # --------------------------------------------------------- message handlers
    def on_reasoning_event_batch(self, message: ReasoningEventBatch) -> None:
        self.apply_events(message.events)

    # ----------------------------------------------------------- public API
    def apply_events(self, events: list[SpanRecord]) -> None:
        """Append ``events`` to the buffer + render those that pass
        the current filter.

        Public so tests can drive the pane without going through the
        polling worker.
        """
        self._buffered.extend(events)
        log = self.query_one("#tape-log", RichLog)
        active = _FILTER_LABELS[self._filter_idx]
        for ev in events:
            if _matches_filter(ev, active):
                log.write(_format_span_row(ev))

    def action_cycle_filter(self) -> None:
        """``Ctrl+T``: cycle ALL → MODEL → TOOL → ALL.

        On each cycle, clear + re-render the log against the new
        filter so the operator sees the buffered history through the
        new lens (no need to wait for the next event)."""
        self._filter_idx = (self._filter_idx + 1) % len(_FILTER_LABELS)
        label = _FILTER_LABELS[self._filter_idx]
        self.query_one("#tape-filter", Static).update(_format_filter_label(label))
        log = self.query_one("#tape-log", RichLog)
        log.clear()
        for ev in self._buffered:
            if _matches_filter(ev, label):
                log.write(_format_span_row(ev))


# --------------------------------------------------------------------------- #
# Pure helpers — easy to unit-test.
# --------------------------------------------------------------------------- #
def _format_filter_label(label: str) -> str:
    """The single-line static at the top: 'Filter: ALL (Ctrl+T cycles)'."""
    return f"Filter: {label}  (Ctrl+T cycles MODEL/TOOL/ALL)"


def _matches_filter(rec: SpanRecord, active: str) -> bool:
    if active == "ALL":
        return True
    if active == "MODEL":
        return rec.kind is SpanKind.MODEL
    if active == "TOOL":
        return rec.kind is SpanKind.TOOL
    return True  # forward-compat for new filter labels


def _format_span_row(rec: SpanRecord) -> str:
    """One-line summary for the tape — terse, readable in a narrow column.

    MODEL spans surface model_id + token cost; TOOL spans surface tool
    name + pass/fail summary; STAGE/RUN/LOOP/GATE/HUMAN are scaffolding
    and render as ``[<time>] <kind> <name>`` so the operator can see
    the structural boundary without clutter.
    """
    ts = rec.started_at.strftime("%H:%M:%S")
    if rec.kind is SpanKind.MODEL:
        return _format_model_row(rec, ts)
    if rec.kind is SpanKind.TOOL:
        return _format_tool_row(rec, ts)
    return f"[{ts}] {rec.kind.value} {rec.name}"


def _format_model_row(rec: SpanRecord, ts: str) -> str:
    model = rec.attributes.get("model_id", rec.name)
    duration = f"{rec.duration_ms / 1000:.1f}s" if rec.duration_ms else "?s"
    tokens = (
        f"{rec.prompt_tokens or 0} in / {rec.completion_tokens or 0} out"
    )
    cost_part = f", ${rec.cost_usd:.4f}" if rec.cost_usd is not None else ""
    return f"[{ts}] MODEL {model} → {duration}, {tokens}{cost_part}"


def _format_tool_row(rec: SpanRecord, ts: str) -> str:
    tool = rec.attributes.get("tool_name", rec.name)
    duration = f"{rec.duration_ms / 1000:.1f}s" if rec.duration_ms else "?s"
    passed = rec.attributes.get("passed")
    pass_part = ""
    if passed is True:
        pass_part = ", passed"
    elif passed is False:
        pass_part = ", FAILED"
    return f"[{ts}] TOOL {tool} → {duration}{pass_part}"
