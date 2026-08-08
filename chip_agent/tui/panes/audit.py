"""Audit log timeline pane (F14.4).

Renders every F7.2 audit event for the active design as a single-line
row in a scrolling timeline, plus a bottom chain-validity badge driven
by :meth:`SqliteAuditLog.verify`. Updates within 500 ms of an event
being appended to ``<run-dir>/audit.sqlite`` — every gate decision,
escalation, artifact promotion, human approval, or RTL frontier-fallback
lands here in real time.

Polls on the main thread via :meth:`Widget.set_interval` so the
single-writer/single-reader SQLite contract stays simple (cmd_run /
cmd_resume write from worker threads; this pane only reads). The
poller opens a fresh :class:`SqliteAuditLog` per tick — the cost is a
WAL-mode SQLite connection open + close at 2 Hz, which is well under
the pipeline pane's checkpoint poll overhead.

The badge text is intentionally plain ASCII ("chain valid (N events)"
/ "TAMPERED — N findings, broken at seq M") so it round-trips through
Textual's CSS + the test harness without font-fallback surprises.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from chip_agent.design_state import DesignState
from chip_agent.obs.audit_log import AuditEvent, AuditVerification, EventType
from chip_agent.tui.messages import AuditChainStatus, AuditEventBatch
from chip_agent.tui.panes.activity_banner import ActivityBanner
from chip_agent.tui.workers.poll_worker import (
    open_audit_log,
    read_audit_tail,
)

__all__ = ["AuditPane"]


_IDLE_BADGE = "Audit: no events yet — start chat or /run."


class AuditPane(Vertical):
    """Append-only audit timeline + chain-validity badge.

    Layout (top to bottom):

    * ``#audit-log`` — scrollable :class:`RichLog` of one row per event,
      newest-at-bottom (matches the spine's chronological order so the
      operator's eye tracks the run downward).
    * ``#audit-badge`` — single-line badge: "chain valid (N events)"
      (green) or "TAMPERED — …" (red). Idle ("no events yet…") until
      the first event lands.
    """

    DEFAULT_CSS = """
    AuditPane {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
    }

    AuditPane > #audit-log {
        height: 1fr;
        background: $surface;
    }

    AuditPane > #audit-badge {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    AuditPane > #audit-badge.audit-valid {
        color: $success;
    }

    AuditPane > #audit-badge.audit-invalid {
        color: $error;
        text-style: bold;
    }
    """

    DEFAULT_POLL_MS: ClassVar[int] = 500

    def __init__(
        self,
        *,
        design_id: str,
        audit_db_path: Path,
        hmac_key: bytes,
        poll_ms: int = DEFAULT_POLL_MS,
    ) -> None:
        super().__init__()
        self.design_id = design_id
        self.audit_db_path = audit_db_path
        self.hmac_key = hmac_key
        self.poll_ms = poll_ms
        self._last_seen_seq = 0
        self._last_badge_text: str | None = None
        self._last_badge_tone: str | None = None

    def compose(self) -> ComposeResult:
        # F22.1-D: the Now/Last activity banner sits at the top of the
        # audit pane so the operator sees current activity at a glance
        # without scrolling. The 2-row banner takes its space out of the
        # log's 1fr height — the log is still scrollable.
        yield ActivityBanner()
        yield RichLog(
            id="audit-log", wrap=False, highlight=False, markup=False,
        )
        yield Static(_IDLE_BADGE, id="audit-badge")

    def on_mount(self) -> None:
        # Single eager poll so a pre-populated log (e.g. resumed session)
        # renders immediately instead of waiting one ``poll_ms`` interval.
        self._poll_once()
        self.set_interval(self.poll_ms / 1000, self._poll_once)

    # ---------------------------------------------------------------- polling
    def _poll_once(self) -> None:
        """One polling tick — read appended events + refresh the badge."""
        with open_audit_log(
            self.audit_db_path, hmac_key=self.hmac_key,
        ) as log:
            if log is None:
                return
            new_events = read_audit_tail(
                log,
                design_id=self.design_id,
                since_sequence=self._last_seen_seq,
            )
            verification = log.verify(self.design_id)

        if new_events:
            self._last_seen_seq = new_events[-1].sequence
            self.post_message(AuditEventBatch(events=new_events))

        text, tone = _format_badge(verification)
        if (text, tone) != (self._last_badge_text, self._last_badge_tone):
            self._last_badge_text = text
            self._last_badge_tone = tone
            self.post_message(AuditChainStatus(text=text, tone=tone))

    # --------------------------------------------------------- message handlers
    def on_audit_event_batch(self, message: AuditEventBatch) -> None:
        self.apply_events(message.events)

    def on_audit_chain_status(self, message: AuditChainStatus) -> None:
        self._render_badge(message.text, message.tone)

    # ------------------------------------------------------------- public API
    def apply_events(self, events: list[AuditEvent]) -> None:
        """Append ``events`` to the scrolling log widget.

        Public so tests can render rows without going through the
        polling worker. F22.1-D: also forwards the batch to the
        activity banner so the "Last" line stays in sync with the
        timeline.
        """
        log = self.query_one("#audit-log", RichLog)
        for ev in events:
            log.write(_format_event_row(ev))
        self.query_one(ActivityBanner).apply_events(events)

    def apply_design_state(self, design: DesignState) -> None:
        """F22.1-D: forward a freshly-polled DesignState to the activity
        banner so the "Now" line stays in sync with the pipeline pane.

        Public so the app's ``on_stage_advanced`` handler can call this
        without reaching into private state (``StageAdvanced`` is posted
        by the pipeline pane's poller; it bubbles up to the app, which
        forwards here)."""
        self.query_one(ActivityBanner).update_from_state(design)

    def apply_verification(self, verification: AuditVerification) -> None:
        """Update the badge directly from an :class:`AuditVerification`.

        Public so tests can drive the badge without going through the
        polling worker.
        """
        text, tone = _format_badge(verification)
        self._render_badge(text, tone)

    # ---------------------------------------------------------------- helpers
    def _render_badge(self, text: str, tone: str | None) -> None:
        badge = self.query_one("#audit-badge", Static)
        badge.update(text)
        for c in ("audit-valid", "audit-invalid"):
            badge.remove_class(c)
        if tone:
            badge.add_class(f"audit-{tone}")


# --------------------------------------------------------------------------- #
# Pure formatters — easy to unit-test.
# --------------------------------------------------------------------------- #
def _format_event_row(ev: AuditEvent) -> str:
    """One-line row for the timeline: seq, time, type, payload summary."""
    ts = _short_ts(ev.timestamp)
    summary = _payload_summary(ev)
    seq = f"{ev.sequence:>3}"
    etype = ev.event_type.value
    if summary:
        return f"[{seq}] {ts}  {etype:<22}  {summary}"
    return f"[{seq}] {ts}  {etype}"


def _short_ts(ts: datetime) -> str:
    return ts.strftime("%H:%M:%S")


def _typed_summary(ev: AuditEvent) -> str | None:
    """F22.1 — plain-English one-liner for the EventTypes that previously
    fell through to JSON.

    Returns ``None`` when the event_type isn't one of the F22.1-covered
    cases — the caller falls through to the shape-based handlers and the
    final JSON-truncated fallback.

    Every handler is defensive: missing keys or unexpected payload shapes
    produce ``None`` (so the JSON fallback shows the operator what's there)
    rather than a misleading "looks right but isn't" summary.
    """
    p = ev.payload
    et = ev.event_type

    if et is EventType.STAGE_TRANSITION:
        # Two payload shapes seen in the wild: {"from": "rtl", "to": "synth"}
        # (advance) and {"stage": "rtl", "kind": "retry"} (retry). Both surface
        # as a one-liner the operator can scan.
        fr = p.get("from")
        to = p.get("to")
        if isinstance(fr, str) and isinstance(to, str):
            return f"advanced {fr} → {to}"
        stage = p.get("stage")
        kind = p.get("kind")
        if isinstance(stage, str) and isinstance(kind, str):
            return f"{kind} {stage}"
        if isinstance(stage, str):
            return f"transition at {stage}"
        return None

    if et is EventType.ESCALATION:
        stage = p.get("stage")
        fr = p.get("from")
        to = p.get("to")
        if isinstance(stage, str) and isinstance(fr, str) and isinstance(to, str):
            return f"escalated {stage} {fr} → {to}"
        if isinstance(fr, str) and isinstance(to, str):
            return f"escalated {fr} → {to}"
        return None

    if et is EventType.FEEDBACK_FIRED:
        fr = p.get("from")
        to = p.get("to")
        if isinstance(fr, str) and isinstance(to, str):
            return f"cross-stage feedback: {fr} → {to}"
        return None

    if et is EventType.BACKEND_FALLBACK:
        # F11.5: router demoted to a fallback model. Typical payload:
        # {"task": "rtl_gen", "from": "frontier", "to": "local-coder",
        #  "reason": "..."} — surface task + new model.
        task = p.get("task")
        to = p.get("to") or p.get("fallback")
        if isinstance(task, str) and isinstance(to, str):
            return f"router fell back to {to} for {task}"
        if isinstance(to, str):
            return f"router fell back to {to}"
        return None

    if et is EventType.RTL_FRONTIER_FALLBACK:
        # F12.5: payload carries module_id; the message itself is the signal.
        module = p.get("module_id")
        if isinstance(module, str):
            return f"RTL outer-loop exhausted: one frontier attempt on {module}"
        return "RTL outer-loop exhausted: one frontier attempt"

    if et is EventType.M19_FAST_PATH_DECISION:
        module = p.get("module_id")
        used = p.get("m19_fast_path_used")
        if isinstance(module, str) and isinstance(used, bool):
            verb = "used" if used else "skipped"
            return f"M19 fast-path: {module} {verb}"
        return None

    if et is EventType.MULTI_CORNER_FALLBACK:
        # F21.2: only one reason in the wild today (openroad_6227_segfault).
        reason = p.get("reason")
        if reason == "openroad_6227_segfault":
            return "multi-corner STA fallback: OpenROAD #6227 (sky130A segfault)"
        if isinstance(reason, str):
            return f"multi-corner STA fallback: {reason}"
        return "multi-corner STA fallback"

    if et is EventType.PHYSICAL_REPAIR_ROUTED:
        # F21.3: payload is {"kind": "lower_density", "reason": "...",
        # "attempt": 1}. Show attempt number + the picked route in plain
        # English so the operator can see what knob the agent flipped.
        kind = p.get("kind")
        attempt = p.get("attempt")
        if isinstance(kind, str) and isinstance(attempt, int):
            return f"physical repair (attempt {attempt}): {kind}"
        if isinstance(kind, str):
            return f"physical repair: {kind}"
        return None

    return None


def _payload_summary(ev: AuditEvent) -> str:
    """Compact summary of the event payload for the row.

    Picks the load-bearing field for each :class:`EventType`; falls back
    to compact JSON capped at 60 chars so the row stays single-line on a
    narrow terminal.

    F22.1: extended to cover the 8 EventType members that previously fell
    through to JSON. The dispatch is by ``ev.event_type`` first (typed,
    payload-shape-independent) so a schema drift surfaces as the generic
    fallback rather than silently picking the wrong handler.
    """
    p = ev.payload
    if not p:
        return ""
    # F22.1: typed dispatch on event_type for the 8 fall-through cases.
    typed = _typed_summary(ev)
    if typed is not None:
        return typed
    if "stage" in p and "ref" in p:
        ref = p["ref"]
        if isinstance(ref, dict):
            aid = ref.get("artifact_id", "")
            ver = ref.get("version", "")
            return f"stage={p['stage']} {aid}@v{ver}"
        return f"stage={p['stage']} {ref}"
    if "stage" in p and "verdict" in p:
        return f"stage={p['stage']} verdict={p['verdict']}"
    if "decision" in p:
        return f"decision={p['decision']}"
    if "kind" in p and p.get("kind") == "routing" and isinstance(
        p.get("new"), dict,
    ):
        # F15.5 ROUTING_CHANGED row: list each binding name + the set of
        # changed field initials. Example:
        #   "kind=routing  tasks=spec_intake[mTn] loops=inner[T]"
        new = p["new"]
        bits: list[str] = []
        for block in ("tasks", "loops"):
            entries: list[str] = []
            for name, partial in sorted((new.get(block) or {}).items()):
                if isinstance(partial, dict):
                    flags = "".join(
                        c for k, c in (
                            ("model", "m"),
                            ("temperature", "T"),
                            ("n", "N"),
                        )
                        if k in partial
                    )
                    entries.append(f"{name}[{flags}]" if flags else name)
                else:
                    entries.append(str(name))
            if entries:
                bits.append(f"{block}={','.join(entries)}")
        return f"kind=routing  {'  '.join(bits)}" if bits else "kind=routing"
    s = json.dumps(p, sort_keys=True, separators=(",", ":"))
    return s if len(s) <= 60 else s[:57] + "..."


def _format_badge(v: AuditVerification) -> tuple[str, str | None]:
    """Map an :class:`AuditVerification` to ``(text, tone)`` for the badge.

    ``tone`` is ``"valid" | "invalid" | None`` (None for the empty-log
    idle state; the badge stays in its default muted color).
    """
    if v.event_count == 0:
        return _IDLE_BADGE, None
    if v.valid:
        return f"chain valid ({v.event_count} events)", "valid"
    return (
        f"TAMPERED — {len(v.findings)} findings, broken at seq "
        f"{v.first_bad_sequence}",
        "invalid",
    )
