"""F22.1-C — ActivityBanner widget.

A compact two-line widget that sits at the top of the right-column
(inside :class:`AuditPane`) and surfaces:

* **Top line ("Now")** — ``Now: {stage} {status}`` (e.g.
  ``Now: RTL running``, ``Now: SIGNOFF awaiting human``,
  ``Now: PHYSICAL running (repair attempt 1)``).
* **Bottom line ("Last")** — ``Last: {plain-english summary} · {age}``
  (e.g. ``Last: M19 fast-path: counter used · 3s ago``).

The banner consumes the same two messages the audit + pipeline panes
already produce — :class:`~chip_agent.tui.messages.StageAdvanced` and
:class:`~chip_agent.tui.messages.AuditEventBatch` — via public methods
the parent pane (or the app) forwards. No new poller.

A 1-second timer refreshes the ``Last`` age column from in-memory
state without re-reading SQLite. If the most-recent event is older
than 30 seconds AND the design status is not AWAITING_HUMAN /
COMPLETED, the banner appends ``(silent — agent may be running)`` so
the operator knows the pane isn't broken — granular per-agent progress
is the F22.2 (Option C) work this banner doesn't pretend to solve.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from chip_agent.design_state import DesignState, DesignStatus, StageStatus
from chip_agent.obs.audit_log import AuditEvent

__all__ = ["ActivityBanner"]


_EMPTY_NOW = "Now: —"
_EMPTY_LAST = "Last: —"
_SILENT_THRESHOLD_S = 30.0


class ActivityBanner(Vertical):
    """Two-line "Now / Last" banner above the audit log.

    Public API:

    * :meth:`update_from_state` — called by the app on every
      ``StageAdvanced`` message; refreshes the "Now" line.
    * :meth:`apply_events` — called by the parent pane on every
      ``AuditEventBatch`` message; updates the "Last" line from the
      newest event in the batch.
    """

    DEFAULT_CSS = """
    ActivityBanner {
        height: 2;
        padding: 0 1;
        background: $surface;
    }

    ActivityBanner > #activity-now {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    ActivityBanner > #activity-last {
        height: 1;
        color: $text-muted;
    }
    """

    REFRESH_INTERVAL_S: ClassVar[float] = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._latest_state: DesignState | None = None
        self._latest_event: AuditEvent | None = None
        # Frozen at the moment the event was first seen so the age
        # column counts wall-clock time since arrival regardless of
        # what the SQLite ``timestamp`` column says (clock skew between
        # the spine and the TUI is real on slow disks).
        self._latest_event_seen_at: datetime | None = None

    def compose(self) -> ComposeResult:
        yield Static(_EMPTY_NOW, id="activity-now")
        yield Static(_EMPTY_LAST, id="activity-last")

    def on_mount(self) -> None:
        self.set_interval(self.REFRESH_INTERVAL_S, self._refresh_last_age)

    # ------------------------------------------------------------- public API
    def update_from_state(self, design: DesignState) -> None:
        """Refresh the "Now" line from a freshly-polled DesignState.

        The pipeline pane's poll worker fires this every checkpoint
        diff; the app forwards by calling this method.
        """
        self._latest_state = design
        now_widget = self.query_one("#activity-now", Static)
        now_widget.update(_render_now_line(design))

    def apply_events(self, events: list[AuditEvent]) -> None:
        """Refresh the "Last" line from the newest event in the batch.

        Empty batches are no-ops (matches AuditEventBatch semantics —
        the pane only posts batches when ``new_events`` is non-empty).
        """
        if not events:
            return
        self._latest_event = events[-1]
        self._latest_event_seen_at = datetime.now(UTC)
        self._redraw_last_widget()

    # ---------------------------------------------------------------- helpers
    def _refresh_last_age(self) -> None:
        """1-second tick: re-render the "Last" line so the age column
        ticks forward without touching SQLite. No-op when no event
        has landed yet."""
        if self._latest_event is None:
            return
        self._redraw_last_widget()

    def _redraw_last_widget(self) -> None:
        if self._latest_event is None or self._latest_event_seen_at is None:
            return
        last_widget = self.query_one("#activity-last", Static)
        age = (datetime.now(UTC) - self._latest_event_seen_at).total_seconds()
        last_widget.update(_render_last_line(
            self._latest_event, age, self._latest_state,
        ))


# --------------------------------------------------------------------------- #
# Pure formatters — easy to unit-test.
# --------------------------------------------------------------------------- #
def _render_now_line(design: DesignState) -> str:
    """Format the "Now" line.

    Reads ``design.current_stage`` + the matching stage status to produce
    a short status word the operator can scan at a glance. The status word
    favours the design-level status (AWAITING_HUMAN / COMPLETED / FAILED)
    when set; otherwise it reads the per-stage StageState.

    Format: ``Now: {stage} {word}``; e.g. ``Now: RTL running``.
    """
    stage = design.current_stage
    if design.status is DesignStatus.AWAITING_HUMAN:
        return f"Now: {stage.value.upper()} awaiting human"
    if design.status is DesignStatus.COMPLETED:
        return f"Now: {stage.value.upper()} completed"
    if design.status is DesignStatus.FAILED:
        return f"Now: {stage.value.upper()} failed"

    # Per-stage StageState read — design-level stages live on
    # ``design.stages``; M19 + RTL stages live on ``ModuleState.stages``.
    # The banner shows the worst-status word across modules for per-module
    # stages so the operator sees the attention-worthy state, matching
    # the pipeline pane's per-cell aggregation.
    statuses: list[StageStatus] = []
    ss = design.stages.get(stage)
    if ss is not None:
        statuses.append(ss.status)
    for module in design.modules.values():
        ms_ss = module.stages.get(stage)
        if ms_ss is not None:
            statuses.append(ms_ss.status)

    if not statuses:
        return f"Now: {stage.value.upper()} pending"

    # Worst-first order, matching pipeline.py's _STATUS_ORDER.
    if any(s is StageStatus.FAILED for s in statuses):
        word = "failed"
    elif any(s is StageStatus.BLOCKED for s in statuses):
        word = "blocked"
    elif any(s is StageStatus.ESCALATED for s in statuses):
        word = "escalated"
    elif any(s is StageStatus.IN_PROGRESS for s in statuses):
        word = "running"
    elif any(s is StageStatus.PENDING for s in statuses):
        word = "pending"
    else:
        word = "passed"

    return f"Now: {stage.value.upper()} {word}"


def _render_last_line(
    event: AuditEvent, age_s: float, state: DesignState | None,
) -> str:
    """Format the "Last" line.

    Reuses :func:`chip_agent.tui.panes.audit._payload_summary` so the same
    plain-English mapping the audit log uses (extended by F22.1-B for the
    8 previously-JSON-only event types) drives the banner.

    Appends a "silent — agent may be running" hint when the most recent
    event is older than 30 seconds AND the design status isn't a terminal
    one. The hint is honest about the limitation — granular per-agent
    progress events are out of scope for F22.1.
    """
    # Lazy import to avoid the circular ``audit.py ↔ activity_banner.py``
    # dependency — the banner is mounted INSIDE AuditPane and the pane's
    # module-level imports load before the banner's module-level imports.
    from chip_agent.tui.panes.audit import _payload_summary
    summary = _payload_summary(event)
    if not summary:
        summary = event.event_type.value
    age = _format_age(age_s)
    body = f"Last: {summary} · {age}"
    if (
        age_s >= _SILENT_THRESHOLD_S
        and state is not None
        and state.status
        not in (DesignStatus.AWAITING_HUMAN, DesignStatus.COMPLETED,
                DesignStatus.FAILED)
    ):
        body = f"{body}  (silent — agent may be running)"
    return body


def _format_age(seconds: float) -> str:
    """``3s ago`` / ``1m 12s ago`` / ``just now`` shapes."""
    if seconds < 1:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    minutes = int(seconds // 60)
    remainder = int(seconds - minutes * 60)
    return f"{minutes}m {remainder}s ago"
