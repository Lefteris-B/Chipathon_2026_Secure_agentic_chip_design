"""F14.4 acceptance: audit log timeline pane.

Pins:

* Mounting the pane against a pre-populated :class:`SqliteAuditLog`
  renders one row per event in sequence order.
* The polling tick picks up newly appended events between ticks and
  appends only the new rows (idempotent on re-poll).
* Verifying with the wrong HMAC key (the stand-in for tampering) flips
  the chain-validity badge from "chain valid" to "TAMPERED — …".

Tests drive ``AuditPane`` directly via Textual's :meth:`App.run_test`
harness — no real LangGraph spine, no live router. The SqliteAuditLog
fixture writes events with the same HMAC key the pane mounts with;
tampering tests just open a second :class:`SqliteAuditLog` against the
same db file with a *different* key so ``verify`` sees signature
mismatches everywhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import RichLog, Static

from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.tui.panes.audit import (
    AuditPane,
    _format_badge,
    _format_event_row,
    _payload_summary,
)

HMAC_KEY = b"f14.4-audit-pane-test-hmac-key"
DESIGN_ID = "audit-pane-test"


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Fixture helpers — pre-populate a SqliteAuditLog with N events.
# --------------------------------------------------------------------------- #
def _populate(
    db_path: Path,
    *,
    design_id: str = DESIGN_ID,
    hmac_key: bytes = HMAC_KEY,
    count: int,
) -> None:
    """Append ``count`` synthetic ARTIFACT_PROMOTED events for ``design_id``."""
    log = SqliteAuditLog(db_path=db_path, hmac_key=hmac_key)
    try:
        for i in range(1, count + 1):
            log.append(
                design_id=design_id,
                event_type=EventType.ARTIFACT_PROMOTED,
                payload={
                    "stage": "rtl",
                    "ref": {
                        "artifact_id": f"{design_id}.module.rtl",
                        "version": i,
                    },
                },
            )
    finally:
        log.close()


def _drive(
    drive: Callable[[AuditPane, object], Awaitable[dict[str, Any]]],
    *,
    audit_db_path: Path,
    design_id: str = DESIGN_ID,
    hmac_key: bytes = HMAC_KEY,
    poll_ms: int = 1_000_000,
) -> dict[str, Any]:
    """Mount an AuditPane in a minimal host App and run ``drive``.

    ``poll_ms`` defaults to "essentially never" so tests can trigger
    polls explicitly via ``pane._poll_once()`` — that keeps assertions
    deterministic instead of racing the interval timer.
    """

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield AuditPane(
                design_id=design_id,
                audit_db_path=audit_db_path,
                hmac_key=hmac_key,
                poll_ms=poll_ms,
            )

    async def _go() -> dict[str, Any]:
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            pane = host.query_one(AuditPane)
            return await drive(pane, pilot)

    return _arun(_go())


def _row_count(pane: AuditPane) -> int:
    """Count rendered rows in the RichLog widget."""
    log = pane.query_one("#audit-log", RichLog)
    return len(log.lines)


def _badge_text(pane: AuditPane) -> str:
    badge = pane.query_one("#audit-badge", Static)
    return str(badge.renderable)


def _badge_classes(pane: AuditPane) -> set[str]:
    badge = pane.query_one("#audit-badge", Static)
    return {
        c for c in badge.classes  # type: ignore[attr-defined]
        if c.startswith("audit-")
    }


# --------------------------------------------------------------------------- #
# Pure-function tests: row + badge formatters.
# --------------------------------------------------------------------------- #
def test_format_event_row_contains_seq_type_and_summary() -> None:
    from chip_agent.obs.audit_log import AuditEvent
    ev = AuditEvent(
        design_id=DESIGN_ID,
        sequence=7,
        timestamp=datetime(2026, 6, 11, 12, 34, 56, tzinfo=UTC),
        event_type=EventType.GATE_DECISION,
        payload={"stage": "signoff", "verdict": "await_human"},
        prev_hash="GENESIS",
        content_hash="sha256:00",
        signature="hmac-sha256:00",
    )
    row = _format_event_row(ev)
    assert "  7" in row  # right-aligned sequence
    assert "12:34:56" in row
    assert "gate_decision" in row
    assert "stage=signoff" in row
    assert "verdict=await_human" in row


def test_payload_summary_picks_decision_for_human_events() -> None:
    from chip_agent.obs.audit_log import AuditEvent
    ev = AuditEvent(
        design_id=DESIGN_ID, sequence=1,
        event_type=EventType.HUMAN_DECISION,
        payload={"decision": "approve"},
    )
    assert _payload_summary(ev) == "decision=approve"


def test_payload_summary_renders_routing_changed_tasks_only() -> None:
    """F15.5 ROUTING_CHANGED with only model swaps shows the [m] flag
    per task and no loops bit."""
    from chip_agent.obs.audit_log import AuditEvent
    ev = AuditEvent(
        design_id=DESIGN_ID, sequence=1,
        event_type=EventType.ROUTING_CHANGED,
        payload={
            "kind": "routing",
            "old": {
                "tasks": {
                    "plan": {"model": "frontier"},
                    "spec_intake": {"model": "frontier"},
                },
                "loops": {},
            },
            "new": {
                "tasks": {
                    "plan": {"model": "local-coder"},
                    "spec_intake": {"model": "local-coder"},
                },
                "loops": {},
            },
        },
    )
    summary = _payload_summary(ev)
    assert "kind=routing" in summary
    assert "tasks=plan[m],spec_intake[m]" in summary
    assert "loops" not in summary  # no loop changes → no loops= bit


def test_payload_summary_renders_routing_changed_with_temperature_and_n() -> None:
    """F15.5: a partial binding diff with temperature + n surfaces the
    [Tn] / [mTn] flags so the audit row reflects what kind of change
    landed."""
    from chip_agent.obs.audit_log import AuditEvent
    ev = AuditEvent(
        design_id=DESIGN_ID, sequence=2,
        event_type=EventType.ROUTING_CHANGED,
        payload={
            "kind": "routing",
            "old": {
                "tasks": {
                    "rtl_gen": {"model": "frontier", "temperature": 0.4},
                },
                "loops": {
                    "inner": {"n": 1},
                },
            },
            "new": {
                "tasks": {
                    "rtl_gen": {"model": "local-coder", "temperature": 0.7},
                },
                "loops": {
                    "inner": {"n": 3},
                },
            },
        },
    )
    summary = _payload_summary(ev)
    assert "kind=routing" in summary
    # Two flags set: m (model) + T (temperature) → [mT]
    assert "tasks=rtl_gen[mT]" in summary
    # One flag set on inner: N (n) → [N]
    assert "loops=inner[N]" in summary


def test_format_badge_idle_when_empty() -> None:
    from chip_agent.obs.audit_log import AuditVerification
    text, tone = _format_badge(
        AuditVerification(valid=True, event_count=0, findings=[]),
    )
    assert "no events yet" in text
    assert tone is None


def test_format_badge_valid_for_clean_chain() -> None:
    from chip_agent.obs.audit_log import AuditVerification
    text, tone = _format_badge(
        AuditVerification(valid=True, event_count=8, findings=[]),
    )
    assert "chain valid" in text
    assert "8 events" in text
    assert tone == "valid"


def test_format_badge_tampered_when_invalid() -> None:
    from chip_agent.obs.audit_log import (
        AuditVerification,
        TamperFinding,
    )
    findings = [
        TamperFinding(
            sequence=3, reason="signature_mismatch",
            stored_value="x", expected_value="y",
        ),
    ]
    text, tone = _format_badge(
        AuditVerification(valid=False, event_count=5, findings=findings),
    )
    assert "TAMPERED" in text
    assert "1 findings" in text
    assert "seq 3" in text
    assert tone == "invalid"


# --------------------------------------------------------------------------- #
# Mounted-pane tests: render + append + chain-validity flip.
# --------------------------------------------------------------------------- #
def test_audit_pane_renders_events_in_order(tmp_path: Path) -> None:
    """Pre-populate a SqliteAuditLog with five events, mount the pane,
    assert five rows appear after the eager first poll."""
    audit_db = tmp_path / "audit.sqlite"
    _populate(audit_db, count=5)

    async def drive(pane: AuditPane, pilot: object) -> dict[str, Any]:
        # The pane's on_mount eagerly polls once; pump until the
        # AuditEventBatch message lands + the RichLog renders.
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "rows": _row_count(pane),
            "badge": _badge_text(pane),
            "badge_classes": _badge_classes(pane),
        }

    captured = _drive(drive, audit_db_path=audit_db)
    assert captured["rows"] == 5
    assert "chain valid" in captured["badge"]
    assert "5 events" in captured["badge"]
    assert "audit-valid" in captured["badge_classes"]


def test_audit_pane_starts_idle_when_no_audit_db_yet(tmp_path: Path) -> None:
    """No audit.sqlite file → idle badge, no rows."""
    audit_db = tmp_path / "missing.sqlite"

    async def drive(pane: AuditPane, pilot: object) -> dict[str, Any]:
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "rows": _row_count(pane),
            "badge": _badge_text(pane),
            "badge_classes": _badge_classes(pane),
        }

    captured = _drive(drive, audit_db_path=audit_db)
    assert captured["rows"] == 0
    assert "no events yet" in captured["badge"]
    assert captured["badge_classes"] == set()  # idle, no tone class


def test_audit_pane_appends_new_events_from_poller(tmp_path: Path) -> None:
    """Start with two events; append three more between poll ticks;
    assert the pane shows all five with no duplicates."""
    audit_db = tmp_path / "audit.sqlite"
    _populate(audit_db, count=2)

    async def drive(pane: AuditPane, pilot: object) -> dict[str, Any]:
        # First eager poll on mount should pick up the initial 2 events.
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        initial_rows = _row_count(pane)

        # Simulate the spine appending 3 more events after the pane
        # started polling.
        _populate(audit_db, count=3)  # appends 3 more (sequence continues)
        pane._poll_once()
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "initial_rows": initial_rows,
            "final_rows": _row_count(pane),
            "badge": _badge_text(pane),
        }

    captured = _drive(drive, audit_db_path=audit_db)
    assert captured["initial_rows"] == 2
    assert captured["final_rows"] == 5
    assert "5 events" in captured["badge"]


def test_audit_pane_chain_validity_badge_reflects_verify_result(
    tmp_path: Path,
) -> None:
    """Mount the pane against the audit DB with the WRONG hmac key;
    every event's signature mismatches; badge flips to TAMPERED."""
    audit_db = tmp_path / "audit.sqlite"
    # The writer uses the canonical key; the pane mounts with a
    # different one. That's the simplest deterministic way to make
    # SqliteAuditLog.verify report tamper findings without touching
    # SQLite directly.
    _populate(audit_db, count=3, hmac_key=HMAC_KEY)
    wrong_key = b"f14.4-wrong-hmac-key"

    async def drive(pane: AuditPane, pilot: object) -> dict[str, Any]:
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "rows": _row_count(pane),
            "badge": _badge_text(pane),
            "badge_classes": _badge_classes(pane),
        }

    captured = _drive(
        drive, audit_db_path=audit_db, hmac_key=wrong_key,
    )
    # The rows still render (the data is readable) — only the chain
    # validity badge flips.
    assert captured["rows"] == 3
    assert "TAMPERED" in captured["badge"]
    assert "audit-invalid" in captured["badge_classes"]
