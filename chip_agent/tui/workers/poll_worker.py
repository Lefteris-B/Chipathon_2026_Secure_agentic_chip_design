"""Polling workers + checkpoint reader (F14.2).

The :class:`PipelinePane` polls the LangGraph checkpoint to observe
the spine's progress (no event/callback story in LangGraph — polling
is the documented path). The poll itself runs on the main thread via
:meth:`Widget.set_interval` so SQLite stays single-threaded; the
:class:`SqliteSaver` is held open across ticks so we don't churn
connections.

This module exposes the I/O surface as two helpers:

* :func:`open_checkpoint_saver` — context-managed
  :class:`SqliteSaver` matching the existing CLI pattern in
  ``chip_agent.graph.state_graph.open_sqlite_checkpointer``. Yields the
  saver; the caller uses it across many ticks and lets the context
  manager close it on pane unmount.
* :func:`read_design_state` — synchronously reads the latest checkpoint
  for ``design_id`` and reconstructs a :class:`DesignState` from it.
  Returns ``None`` when no checkpoint exists yet (the run hasn't
  started, or the design_id doesn't match an existing run).

The :class:`PipelinePane` then calls ``set_interval`` with a callback
that runs ``read_design_state`` each tick, compares the snapshot to
the last seen one, and posts :class:`StageAdvanced` when anything
changed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from chip_agent.design_state import DesignState
from chip_agent.obs.audit_log import AuditEvent, SqliteAuditLog
from chip_agent.obs.tracing import SpanKind, SpanRecord, SpanStatus

__all__ = [
    "ReasoningTailState",
    "open_audit_log",
    "open_checkpoint_saver",
    "read_audit_tail",
    "read_design_state",
    "read_reasoning_tail",
]


@contextmanager
def open_checkpoint_saver(
    checkpoint_path: Path,
) -> Iterator[BaseCheckpointSaver[Any] | None]:
    """Yield an open :class:`SqliteSaver` for ``checkpoint_path``.

    Returns ``None`` from the context manager when the checkpoint file
    doesn't exist yet (no run has started). Callers handle the ``None``
    case by skipping reads.
    """
    if not checkpoint_path.exists():
        yield None
        return
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        yield saver


def read_design_state(
    saver: BaseCheckpointSaver[Any] | None, design_id: str,
) -> DesignState | None:
    """Return the latest persisted :class:`DesignState` for ``design_id``.

    ``None`` when ``saver`` is ``None`` (no checkpoint file) OR the
    saver has no row for this ``design_id`` yet (the run started against
    a different id). The reconstructed :class:`DesignState` is fed
    through :meth:`DesignState.model_validate` so every field passes
    Pydantic v2 validation, mirroring what the graph's nodes produce.
    """
    if saver is None:
        return None
    cfg = RunnableConfig(configurable={"thread_id": design_id})
    tup = saver.get_tuple(cfg)
    if tup is None:
        return None
    return DesignState.model_validate(tup.checkpoint["channel_values"])


# --------------------------------------------------------------------------- #
# Audit log polling (F14.4)
# --------------------------------------------------------------------------- #
@contextmanager
def open_audit_log(
    audit_db: Path, *, hmac_key: bytes,
) -> Iterator[SqliteAuditLog | None]:
    """Yield an open :class:`SqliteAuditLog` for ``audit_db``.

    Returns ``None`` when the audit DB file doesn't exist yet (the
    operator hasn't pressed ``[R]un`` so no ``cmd_run`` has had a chance
    to create it). Callers handle the ``None`` case by skipping reads.
    """
    if not audit_db.exists():
        yield None
        return
    log = SqliteAuditLog(db_path=audit_db, hmac_key=hmac_key)
    try:
        yield log
    finally:
        log.close()


def read_audit_tail(
    log: SqliteAuditLog | None,
    *,
    design_id: str,
    since_sequence: int = 0,
) -> list[AuditEvent]:
    """Return audit events whose ``sequence > since_sequence``.

    ``log=None`` is the "no audit DB yet" case (returns an empty list).
    The full chain is queried each tick — F7.2's audit logs are small
    (eight events for a vanilla run) so trimming server-side isn't
    worth the additional API surface.
    """
    if log is None:
        return []
    events = log.events(design_id)
    return [ev for ev in events if ev.sequence > since_sequence]


# --------------------------------------------------------------------------- #
# F22.2: reasoning JSONL tail reader.
# --------------------------------------------------------------------------- #
class ReasoningTailState:
    """Per-pane cursor state for the JSONL tail reader.

    Holds the last byte offset we read up to AND a residual buffer for a
    line that was incomplete at offset boundary (the writer's ``flush``
    is between lines, but a partial line is still possible if the tail
    fires mid-write). Kept tiny so a future reset/seek-to-end is one
    field reassignment.
    """

    def __init__(self) -> None:
        self.offset: int = 0
        self.residual: str = ""


def read_reasoning_tail(
    jsonl_path: Path, state: ReasoningTailState,
) -> list[SpanRecord]:
    """Return every newly-appeared :class:`SpanRecord` since the last tail
    read. Advances ``state.offset`` in place.

    Polling shape mirrors the AuditPane (single-reader, main thread, 500
    ms cadence) so we don't need a watcher / inotify. Returns an empty
    list when the file doesn't exist yet (the run hasn't opened a span
    on a real tracer) or when no new bytes have appeared since the last
    tick.
    """
    if not jsonl_path.exists():
        return []
    size = jsonl_path.stat().st_size
    if size <= state.offset:
        return []
    with jsonl_path.open("rb") as fh:
        fh.seek(state.offset)
        chunk = fh.read(size - state.offset).decode("utf-8", errors="replace")
        state.offset = size
    text = state.residual + chunk
    lines = text.split("\n")
    # Final element after split is the residual (empty when chunk ended
    # on a newline). Carry it forward.
    state.residual = lines[-1]
    records: list[SpanRecord] = []
    for ln in lines[:-1]:
        if not ln.strip():
            continue
        try:
            payload = json.loads(ln)
            records.append(_span_record_from_payload(payload))
        except (json.JSONDecodeError, KeyError, ValueError):
            # Malformed lines are dropped — defensive against a writer
            # crash mid-line that left invalid JSON behind. The next
            # successful close will rewrite a complete line further on.
            continue
    return records


def _span_record_from_payload(payload: dict[str, Any]) -> SpanRecord:
    """Rebuild a :class:`SpanRecord` from a parsed JSONL line.

    The tracer writes via ``dataclasses.asdict`` + ISO-8601 datetime +
    enum.value, so the reverse pass swaps those back to typed forms
    before feeding to the dataclass constructor.
    """
    record = SpanRecord(
        span_id=payload["span_id"],
        parent_id=payload.get("parent_id"),
        name=payload["name"],
        kind=SpanKind(payload["kind"]),
        design_id=payload["design_id"],
        started_at=datetime.fromisoformat(payload["started_at"]),
        ended_at=(
            datetime.fromisoformat(payload["ended_at"])
            if payload.get("ended_at") else None
        ),
        duration_ms=payload.get("duration_ms"),
        prompt_tokens=payload.get("prompt_tokens"),
        completion_tokens=payload.get("completion_tokens"),
        cost_usd=payload.get("cost_usd"),
        status=SpanStatus(payload.get("status", "ok")),
        error=payload.get("error"),
        attributes=payload.get("attributes") or {},
    )
    if payload.get("escalation"):
        # EscalationLevel is a StrEnum; reconstruct by value.
        from chip_agent.design_state import EscalationLevel
        record = replace(record, escalation=EscalationLevel(payload["escalation"]))
    return record
