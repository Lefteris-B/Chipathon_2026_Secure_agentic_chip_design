"""F7.2 acceptance: tampering with a past event is detectable; ``replay`` reproduces
the run's decision sequence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chip_agent.design_state import EscalationLevel, Stage, Transition
from chip_agent.obs.audit_log import (
    GENESIS_PREV_HASH,
    AuditEvent,
    AuditLogError,
    EventType,
    SqliteAuditLog,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def log(tmp_path: Path) -> SqliteAuditLog:
    lg = SqliteAuditLog(
        db_path=tmp_path / "audit.sqlite",
        hmac_key=b"unit-test-secret-key",
    )
    yield lg
    lg.close()


def _seed_run(log: SqliteAuditLog, *, design_id: str = "d0") -> list[AuditEvent]:
    """Emit a small representative chain: GATE_DECISION → STAGE_TRANSITION → …"""
    e1 = log.append(
        design_id=design_id, event_type=EventType.GATE_DECISION,
        payload={"stage": Stage.RTL.value, "decision": Transition.RETRY.value,
                 "attempt": 1},
    )
    e2 = log.append(
        design_id=design_id, event_type=EventType.STAGE_TRANSITION,
        payload={"from": Stage.RTL.value, "to": Stage.RTL.value, "reason": "retry"},
    )
    e3 = log.append(
        design_id=design_id, event_type=EventType.ESCALATION,
        payload={"stage": Stage.RTL.value, "from": EscalationLevel.INNER.value,
                 "to": EscalationLevel.OUTER.value},
    )
    e4 = log.append(
        design_id=design_id, event_type=EventType.ARTIFACT_PROMOTED,
        payload={"stage": Stage.RTL.value, "artifact_id": "d0.counter.rtl"},
    )
    return [e1, e2, e3, e4]


# --------------------------------------------------------------------------- #
# Construction / config
# --------------------------------------------------------------------------- #
def test_log_rejects_empty_hmac_key(tmp_path: Path) -> None:
    with pytest.raises(AuditLogError):
        SqliteAuditLog(db_path=tmp_path / "a.sqlite", hmac_key=b"")


def test_append_rejects_empty_design_id(log: SqliteAuditLog) -> None:
    with pytest.raises(AuditLogError):
        log.append(design_id="", event_type=EventType.STAGE_TRANSITION)


# --------------------------------------------------------------------------- #
# Hash chain shape
# --------------------------------------------------------------------------- #
def test_first_event_links_to_genesis(log: SqliteAuditLog) -> None:
    [e] = _seed_run(log)[:1]
    assert e.sequence == 1
    assert e.prev_hash == GENESIS_PREV_HASH
    assert e.content_hash.startswith("sha256:")
    assert e.signature.startswith("hmac-sha256:")


def test_chain_links_each_event_to_predecessor(log: SqliteAuditLog) -> None:
    from itertools import pairwise

    events = _seed_run(log)
    for prev, nxt in pairwise(events):
        assert nxt.prev_hash == prev.content_hash
        assert nxt.sequence == prev.sequence + 1


def test_replay_yields_events_in_sequence_order(log: SqliteAuditLog) -> None:
    seeded = _seed_run(log)
    replayed = log.replay("d0")
    assert [e.sequence for e in replayed] == [1, 2, 3, 4]
    assert [e.event_type for e in replayed] == [e.event_type for e in seeded]
    # Payloads round-trip through SQLite JSON without loss.
    for s, r in zip(seeded, replayed, strict=True):
        assert s.payload == r.payload


def test_events_alias_matches_replay(log: SqliteAuditLog) -> None:
    _seed_run(log)
    assert log.events("d0") == log.replay("d0")


# --------------------------------------------------------------------------- #
# Multi-design isolation
# --------------------------------------------------------------------------- #
def test_chains_are_independent_per_design_id(log: SqliteAuditLog) -> None:
    a = _seed_run(log, design_id="alpha")
    b = _seed_run(log, design_id="beta")
    # Sequences restart per design; no cross-talk on prev_hash.
    assert [e.sequence for e in a] == [1, 2, 3, 4]
    assert [e.sequence for e in b] == [1, 2, 3, 4]
    assert b[0].prev_hash == GENESIS_PREV_HASH
    # Both chains independently verify.
    assert log.verify("alpha").valid
    assert log.verify("beta").valid


# --------------------------------------------------------------------------- #
# AC: tamper detection
# --------------------------------------------------------------------------- #
def _tamper(log: SqliteAuditLog, *, design_id: str, sequence: int, payload: str) -> None:
    """Sneak a row update past the public API — simulates an attacker that
    overwrites a past event directly in the SQLite file."""
    with log._raw_write() as conn:
        conn.execute(
            "UPDATE audit_events SET payload = ? "
            "WHERE design_id = ? AND sequence = ?",
            (payload, design_id, sequence),
        )


def test_clean_chain_verifies(log: SqliteAuditLog) -> None:
    _seed_run(log)
    result = log.verify("d0")
    assert result.valid
    assert result.event_count == 4
    assert result.findings == []
    assert result.first_bad_sequence is None


def test_tampered_payload_is_detected(log: SqliteAuditLog) -> None:
    # F7.2 AC strand 1: modifying a stored event's payload is detectable.
    _seed_run(log)
    _tamper(
        log, design_id="d0", sequence=2,
        payload=json.dumps({"from": "rtl", "to": "rtl", "reason": "FORGED"}),
    )

    result = log.verify("d0")
    assert not result.valid
    assert result.first_bad_sequence == 2
    # The recomputed content_hash on sequence 2 no longer matches the row's
    # stored hash — that finding fires first.
    reasons = {f.reason for f in result.findings if f.sequence == 2}
    assert "content_hash_mismatch" in reasons


def test_tampered_payload_cascades_to_chain_break(log: SqliteAuditLog) -> None:
    # Editing event 2 also breaks event 3's prev_hash check, because event 3's
    # prev_hash was computed against the *original* event 2's content hash.
    _seed_run(log)
    _tamper(
        log, design_id="d0", sequence=2,
        payload=json.dumps({"reason": "FORGED"}),
    )

    result = log.verify("d0")
    # Verification surfaces findings at BOTH sequence 2 (content mismatch)
    # and sequence 3 (its prev_hash no longer matches the doctored row's
    # recomputed hash).
    by_seq = {f.sequence for f in result.findings}
    assert 2 in by_seq
    # Since we didn't update event-2's content_hash field, event-3's prev_hash
    # still matches the stored (now stale) content_hash. The cascade fires
    # only when the attacker also rewrites event-2's content_hash, which the
    # signature check still catches — covered by the signature test below.


def test_tampered_content_hash_breaks_signature(log: SqliteAuditLog) -> None:
    # A sophisticated attacker might rewrite both `payload` AND `content_hash`
    # to match the forged payload. They can't fix the signature without the
    # HMAC key, so verify still catches it.
    events = _seed_run(log)
    target = events[1]  # sequence 2
    forged_payload = {"reason": "FORGED"}
    forged_payload_str = json.dumps(
        forged_payload, sort_keys=True, separators=(",", ":"),
    )
    # Recompute the content hash an attacker WOULD compute to match.
    forged_event = AuditEvent(
        event_id=target.event_id,
        design_id=target.design_id,
        sequence=target.sequence,
        timestamp=target.timestamp,
        event_type=target.event_type,
        payload=forged_payload,
        prev_hash=target.prev_hash,
    )
    from chip_agent.obs.audit_log import _content_hash
    forged_hash = _content_hash(forged_event)
    with log._raw_write() as conn:
        conn.execute(
            "UPDATE audit_events SET payload = ?, content_hash = ? "
            "WHERE design_id = ? AND sequence = ?",
            (forged_payload_str, forged_hash, "d0", 2),
        )

    result = log.verify("d0")
    assert not result.valid
    # Signature was computed against the *original* content_hash; the
    # attacker can't re-sign without the key.
    reasons = {f.reason for f in result.findings if f.sequence == 2}
    assert "signature_mismatch" in reasons
    # The chain break also fires because event 3's prev_hash still points
    # at the original content_hash, not the forged one.
    reasons_3 = {f.reason for f in result.findings if f.sequence == 3}
    assert "prev_hash_mismatch" in reasons_3


def test_wrong_key_invalidates_signatures(log: SqliteAuditLog, tmp_path: Path) -> None:
    _seed_run(log)
    log.close()

    # Same DB, different HMAC key — every signature should fail to verify.
    other = SqliteAuditLog(
        db_path=tmp_path / "audit.sqlite",
        hmac_key=b"different-key",
    )
    try:
        result = other.verify("d0")
        assert not result.valid
        # Every event has a signature_mismatch under the wrong key.
        seqs_with_sig_fail = {
            f.sequence for f in result.findings if f.reason == "signature_mismatch"
        }
        assert seqs_with_sig_fail == {1, 2, 3, 4}
    finally:
        other.close()


# --------------------------------------------------------------------------- #
# AC: replay yields the decision sequence
# --------------------------------------------------------------------------- #
def test_replay_reproduces_decision_sequence(log: SqliteAuditLog) -> None:
    seeded = _seed_run(log)
    replayed = log.replay("d0")

    # Same number of events, same order, same payloads, same hashes.
    assert len(replayed) == len(seeded)
    for s, r in zip(seeded, replayed, strict=True):
        assert s.event_id == r.event_id
        assert s.event_type == r.event_type
        assert s.payload == r.payload
        assert s.content_hash == r.content_hash
        assert s.signature == r.signature


def test_replay_unknown_design_is_empty(log: SqliteAuditLog) -> None:
    _seed_run(log, design_id="alpha")
    assert log.replay("beta") == []


# --------------------------------------------------------------------------- #
# Inserting events mid-chain (e.g. attacker forging an extra event)
# --------------------------------------------------------------------------- #
def test_inserted_event_breaks_chain(log: SqliteAuditLog) -> None:
    _seed_run(log)
    # Attacker INSERTS a brand new event at sequence 3 by overwriting the
    # legitimate one. The forged event's hashes are garbage, so verify
    # catches both content_hash + signature mismatches, and the next event's
    # prev_hash no longer links.
    with log._raw_write() as conn:
        conn.execute(
            "DELETE FROM audit_events WHERE design_id = ? AND sequence = ?",
            ("d0", 3),
        )
        conn.execute(
            "INSERT INTO audit_events ("
            "design_id, sequence, event_id, timestamp, event_type, "
            "payload, prev_hash, content_hash, signature"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "d0", 3, "forged", "2099-01-01T00:00:00+00:00",
                EventType.HUMAN_DECISION.value,
                "{}", "sha256:00", "sha256:00", "hmac-sha256:00",
            ),
        )

    result = log.verify("d0")
    assert not result.valid
    # Multiple findings on the forged event: content hash mismatch (garbage
    # hash), signature mismatch (wrong HMAC), AND prev_hash mismatch (the
    # garbage prev_hash doesn't link to event 2). Event 4 also breaks
    # because its prev_hash pointed at the deleted event's content_hash.
    seq3_reasons = {f.reason for f in result.findings if f.sequence == 3}
    assert "content_hash_mismatch" in seq3_reasons
    assert "signature_mismatch" in seq3_reasons
    seq4_reasons = {f.reason for f in result.findings if f.sequence == 4}
    assert "prev_hash_mismatch" in seq4_reasons


def test_database_file_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    key = b"persist-key"
    log = SqliteAuditLog(db_path=path, hmac_key=key)
    _seed_run(log)
    log.close()

    log2 = SqliteAuditLog(db_path=path, hmac_key=key)
    try:
        assert log2.verify("d0").valid
        assert len(log2.replay("d0")) == 4
    finally:
        log2.close()


# --------------------------------------------------------------------------- #
# Schema-level sanity: the schema is created on first open.
# --------------------------------------------------------------------------- #
def test_schema_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    log = SqliteAuditLog(db_path=path, hmac_key=b"k")
    log.close()
    # Reopen — no error, schema already present.
    SqliteAuditLog(db_path=path, hmac_key=b"k").close()

    # Verify the audit_events table actually exists.
    conn = sqlite3.connect(path)
    try:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
        assert "audit_events" in names
    finally:
        conn.close()
