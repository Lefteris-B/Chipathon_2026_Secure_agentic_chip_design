"""Append-only, hash-chained, HMAC-signed audit log (F7.2).

Records every stage transition + gate decision in an SQLite-backed table
whose rows form a chain: each event's ``prev_hash`` is the previous
event's ``content_hash``, and each row carries an ``signature`` =
``HMAC(key, content_hash)``. Two properties fall out:

* **Tamper detection.** Modifying a stored event changes its
  recomputed ``content_hash``, which (a) no longer matches the row's
  stored hash and (b) breaks the chain with the next event. The
  HMAC also fails — without the key, an attacker can't fix it.
* **Replay.** Walking the chain in ``sequence`` order yields the run's
  full decision sequence in the order it happened.

The signing key is supplied by the caller; in production it would come
from :class:`Settings`. For tests we pass it in directly so the unit
tests can exercise key-rotation / wrong-key scenarios cleanly.

Per-design chains are independent (different ``design_id`` -> separate
sequence + genesis), so a single SQLite file can host many runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GENESIS_PREV_HASH",
    "AuditEvent",
    "AuditLogError",
    "AuditVerification",
    "EventType",
    "SqliteAuditLog",
    "TamperFinding",
]


GENESIS_PREV_HASH = "GENESIS"


class EventType(StrEnum):
    """The event kinds the control plane records.

    Limited on purpose: a small vocabulary keeps the replay table
    predictable. New decision kinds get added as the control graph
    grows; the schema is forward-compatible because the payload is a
    typed JSON dict.
    """

    STAGE_TRANSITION = "stage_transition"   # current_stage moved (advance / retry)
    GATE_DECISION = "gate_decision"         # decide_transition's verdict
    ESCALATION = "escalation"               # INNER -> OUTER, OUTER -> HUMAN
    FEEDBACK_FIRED = "feedback_fired"       # F5.4 cross-stage feedback fired
    HUMAN_DECISION = "human_decision"       # user resumed / approved a run
    ARTIFACT_PROMOTED = "artifact_promoted"  # promote_to_head set a stage's head
    BACKEND_FALLBACK = "backend_fallback"   # F11.5 router demoted to a fallback model
    RTL_FRONTIER_FALLBACK = "rtl_frontier_fallback"  # F12.5 RTL outer loop exhausted; one EXHAUSTED attempt
    ROUTING_CHANGED = "routing_changed"     # F15.3 operator changed task→model bindings via the picker
    # F19.13: per-module trivial-module fast-path decision. One event
    # per module fires at the CONTRACT node entry (when the M19 path
    # is active) carrying ``m19_fast_path_used: true|false``. Trivial
    # modules then skip CONTRACT / ORACLE / ASSERTIONS /
    # ORACLE_VERIFICATION and fall through to the LLM TB + single-call
    # RTL gen path.
    M19_FAST_PATH_DECISION = "m19_fast_path_decision"
    # F21.2: per-corner STA harvest was requested but the LibreLane run
    # tripped a known toolchain bug (sky130A multi-corner STA segfault,
    # OpenROAD #6227). SIGNOFF degrades cleanly to the single-corner
    # path. Payload: ``{"reason": "openroad_6227_segfault",
    # "stderr_excerpt": "..."}`` so the operator can correlate without
    # reading the raw librelane log.
    MULTI_CORNER_FALLBACK = "multi_corner_fallback"
    # F21.3: SIGNOFF timing-failure dispatcher picked a recovery route.
    # One event per route applied; payload carries
    # ``{"kind": "<route>", "reason": "<one-line>",
    #   "attempt": <history-length-after-append>}`` so the audit trail
    # shows the agent's reasoning + cumulative attempt count without
    # joining against the route artifact (no artifact in v1; the route
    # list lives on DesignState's checkpoint).
    PHYSICAL_REPAIR_ROUTED = "physical_repair_routed"
    # F23.3: interactive human-repair turn lifecycle. HUMAN_TURN_OPENED
    # fires when a stage escalation opens a conversation instead of
    # dead-ending; HUMAN_HINT_RECORDED when the operator's chat is
    # distilled + persisted as a HumanHint (payload carries the hint ref +
    # hint_kind); HUMAN_RETRY_DISPATCHED when a validated route re-enters
    # the graph (payload: route kind + human_turns_used). None of these
    # touch the gate — they only trace the operator-in-the-loop path.
    HUMAN_TURN_OPENED = "human_turn_opened"
    HUMAN_HINT_RECORDED = "human_hint_recorded"
    HUMAN_RETRY_DISPATCHED = "human_retry_dispatched"
    # F23.6: a differential sim PASSED but its window never let the design
    # finish (a completion output port stayed 0 across the whole EXPECTED
    # trace) — a vacuous pass. The RTL node refuses to silently advance and
    # opens an interactive-repair turn instead. Payload:
    # {"module_id", "completion_port"}.
    SIM_VACUOUS_PASS = "sim_vacuous_pass"


class AuditEvent(BaseModel):
    """One row of the audit log.

    ``content_hash`` and ``signature`` are filled in by the log on
    :meth:`SqliteAuditLog.append` — callers construct the event with
    just ``design_id`` / ``event_type`` / ``payload`` (and let the log
    assign ``sequence`` / ``prev_hash`` / ``timestamp``).
    """

    model_config = ConfigDict(frozen=False)

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    design_id: str
    sequence: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = GENESIS_PREV_HASH
    content_hash: str = ""
    signature: str = ""


@dataclass(frozen=True)
class TamperFinding:
    """Specific tamper instance flagged by :meth:`SqliteAuditLog.verify`."""

    sequence: int
    reason: str  # "content_hash_mismatch" / "prev_hash_mismatch" / "signature_mismatch"
    stored_value: str
    expected_value: str


@dataclass(frozen=True)
class AuditVerification:
    """Result of :meth:`SqliteAuditLog.verify`."""

    valid: bool
    event_count: int
    findings: list[TamperFinding]

    @property
    def first_bad_sequence(self) -> int | None:
        return self.findings[0].sequence if self.findings else None


class AuditLogError(RuntimeError):
    """SQL or schema-level audit-log failure (missing row, corrupt data, …)."""


# --------------------------------------------------------------------------- #
# Canonical hashing — same shape for produce-side + verify-side.
# --------------------------------------------------------------------------- #
def _canonical_payload(event: AuditEvent) -> bytes:
    """Return the bytes the ``content_hash`` is computed over.

    Excludes ``content_hash`` and ``signature`` themselves so a populated
    event re-hashes to the same value the original ``append`` computed.
    Includes everything else (event_id, design_id, sequence, timestamp,
    event_type, payload, prev_hash) — tampering with any of those will
    change the recomputed hash.
    """
    canonical = {
        "event_id": event.event_id,
        "design_id": event.design_id,
        "sequence": event.sequence,
        "timestamp": event.timestamp.isoformat(),
        "event_type": event.event_type.value,
        "payload": event.payload,
        "prev_hash": event.prev_hash,
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()


def _content_hash(event: AuditEvent) -> str:
    return "sha256:" + hashlib.sha256(_canonical_payload(event)).hexdigest()


def _hmac_signature(content_hash: str, *, key: bytes) -> str:
    return "hmac-sha256:" + hmac.new(
        key, content_hash.encode(), hashlib.sha256,
    ).hexdigest()


# --------------------------------------------------------------------------- #
# SQLite store
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    design_id     TEXT NOT NULL,
    sequence      INTEGER NOT NULL,
    event_id      TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    prev_hash     TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    signature     TEXT NOT NULL,
    PRIMARY KEY (design_id, sequence)
);

CREATE INDEX IF NOT EXISTS audit_events_design
    ON audit_events (design_id, sequence);
"""


class SqliteAuditLog:
    """Append-only, hash-chained, HMAC-signed audit log on SQLite.

    Concurrent ``append`` calls for the **same** ``design_id`` are not
    supported (the next-sequence read isn't transactional); concurrent
    appends for **different** ``design_id`` are fine. F7.2 only needs
    single-writer semantics, which match the control graph's run model.
    """

    def __init__(
        self,
        *,
        db_path: Path | str,
        hmac_key: bytes,
    ) -> None:
        if not hmac_key:
            raise AuditLogError("hmac_key must be non-empty")
        self._hmac_key = hmac_key
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ----------------------------------------------------------------- append #
    def append(
        self,
        *,
        design_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> AuditEvent:
        """Append one event for ``design_id``; return the persisted row."""
        if not design_id:
            raise AuditLogError("design_id must be non-empty")

        head = self._head(design_id)
        sequence = (head.sequence + 1) if head else 1
        prev_hash = head.content_hash if head else GENESIS_PREV_HASH

        event = AuditEvent(
            design_id=design_id,
            sequence=sequence,
            event_type=event_type,
            payload=dict(payload or {}),
            timestamp=timestamp or datetime.now(UTC),
            prev_hash=prev_hash,
        )
        event.content_hash = _content_hash(event)
        event.signature = _hmac_signature(event.content_hash, key=self._hmac_key)

        self._conn.execute(
            "INSERT INTO audit_events ("
            "design_id, sequence, event_id, timestamp, event_type, "
            "payload, prev_hash, content_hash, signature"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.design_id,
                event.sequence,
                event.event_id,
                event.timestamp.isoformat(),
                event.event_type.value,
                json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
                event.prev_hash,
                event.content_hash,
                event.signature,
            ),
        )
        return event

    # --------------------------------------------------------------- queries #
    def events(self, design_id: str) -> list[AuditEvent]:
        """Return all events for ``design_id`` in ``sequence`` order."""
        rows = self._conn.execute(
            "SELECT design_id, sequence, event_id, timestamp, event_type, "
            "payload, prev_hash, content_hash, signature "
            "FROM audit_events WHERE design_id = ? ORDER BY sequence",
            (design_id,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def replay(self, design_id: str) -> list[AuditEvent]:
        """Alias for :meth:`events` — F7.2 AC reads the "replay" verb."""
        return self.events(design_id)

    # ---------------------------------------------------------------- verify #
    def verify(self, design_id: str) -> AuditVerification:
        """Walk the chain for ``design_id`` and report any tampering.

        Three checks per event, evaluated in order:

        1. Recompute ``content_hash`` over the row's payload + headers.
           A mismatch means the row itself was modified.
        2. The row's ``prev_hash`` must match the previous row's
           ``content_hash`` (or ``GENESIS`` at sequence 1). A mismatch
           means an event was inserted, dropped, or reordered.
        3. The HMAC ``signature`` must match ``HMAC(key, content_hash)``.
           A mismatch means the signing key changed (or the signature
           field was edited).
        """
        events = self.events(design_id)
        findings: list[TamperFinding] = []
        expected_prev = GENESIS_PREV_HASH

        for ev in events:
            recomputed = _content_hash(ev)
            if recomputed != ev.content_hash:
                findings.append(TamperFinding(
                    sequence=ev.sequence,
                    reason="content_hash_mismatch",
                    stored_value=ev.content_hash,
                    expected_value=recomputed,
                ))
            if ev.prev_hash != expected_prev:
                findings.append(TamperFinding(
                    sequence=ev.sequence,
                    reason="prev_hash_mismatch",
                    stored_value=ev.prev_hash,
                    expected_value=expected_prev,
                ))
            expected_sig = _hmac_signature(ev.content_hash, key=self._hmac_key)
            if not hmac.compare_digest(expected_sig, ev.signature):
                findings.append(TamperFinding(
                    sequence=ev.sequence,
                    reason="signature_mismatch",
                    stored_value=ev.signature,
                    expected_value=expected_sig,
                ))
            expected_prev = ev.content_hash

        findings.sort(key=lambda f: (f.sequence, f.reason))
        return AuditVerification(
            valid=not findings,
            event_count=len(events),
            findings=findings,
        )

    # ----------------------------------------------------------------- helpers
    def _head(self, design_id: str) -> AuditEvent | None:
        row = self._conn.execute(
            "SELECT design_id, sequence, event_id, timestamp, event_type, "
            "payload, prev_hash, content_hash, signature "
            "FROM audit_events WHERE design_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (design_id,),
        ).fetchone()
        return self._row_to_event(row) if row else None

    def _row_to_event(self, row: tuple[Any, ...]) -> AuditEvent:
        return AuditEvent(
            design_id=row[0],
            sequence=row[1],
            event_id=row[2],
            timestamp=datetime.fromisoformat(row[3]),
            event_type=EventType(row[4]),
            payload=json.loads(row[5]),
            prev_hash=row[6],
            content_hash=row[7],
            signature=row[8],
        )

    @contextmanager
    def _raw_write(self) -> Iterator[sqlite3.Connection]:
        """Escape hatch for tampering tests — production code never uses it."""
        yield self._conn
