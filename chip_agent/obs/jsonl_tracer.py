"""JSONL-backed tracer for the F22.2 reasoning surface.

The :class:`Tracer` Protocol in :mod:`chip_agent.obs.tracing` exposes a
span tree, but production has historically wired :class:`NoopTracer` —
every prompt, completion, and tool invocation has been silently
dropped. F22.2 swaps in :class:`JsonlTracer` for TUI-invoked runs so
the operator (and the post-hoc auditor) can see what each agent was
actually reasoning over.

Design — locked in `docs/AGENT_REASONING_AUDIT_RESEARCH.md` (Shape A):

* The JSONL file is the **source of truth** for reasoning, modeled on
  Claude Code's per-session JSONL and OpenHands' event store. One
  :class:`SpanRecord` per line, NDJSON.
* Composition over inheritance — :class:`JsonlTracer` wraps an
  :class:`InMemoryTracer` so tests can still inspect
  ``tracer.spans`` after a run, and the JSONL is a pure side-effect on
  span close. Two sinks, one tracer.
* Append-only. Re-opening the same path appends; the line ordering is
  the close ordering, which matches the existing in-memory
  ``spans`` list and the recorded-tree semantics that
  :func:`build_trace_tree` already assumes.
* Concurrency: LiteLLM streaming finalizes MODEL spans from background
  threads (see F22.2 Phase 2). A :class:`threading.Lock` around the
  append keeps lines from interleaving. The file handle is held open
  for the run lifetime so per-span ``open()/close()`` cost stays out
  of the hot path.

The serialized line shape is **NoeSI-native** (not OpenInference) — a
direct Pydantic dump of :class:`SpanRecord` with ``datetime`` → ISO
8601, :class:`enum.Enum` → ``.value``. A future
``noesi-to-openinference`` converter is a ~50 LOC pure function over
the same lines.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import IO, Any

from chip_agent.obs.tracing import (
    InMemoryTracer,
    Span,
    SpanKind,
    SpanRecord,
)

__all__ = ["JsonlTracer", "span_record_to_jsonl_line"]


class JsonlTracer:
    """Tracer that mirrors every closed span to an NDJSON file.

    Wraps an :class:`InMemoryTracer` so callers retain ``tracer.spans``
    (used by tests and :func:`build_trace_tree`) AND a streaming
    append-only file the TUI can tail at 500 ms. The file handle is
    opened lazily on the first span close so a no-op session (caller
    opened ``tracer.run()`` then exited immediately) doesn't litter
    the run dir with an empty ``reasoning.jsonl``.
    """

    def __init__(self, jsonl_path: Path) -> None:
        self.jsonl_path = jsonl_path
        self._inner = InMemoryTracer()
        self._fh: IO[str] | None = None
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- spans ##
    @property
    def spans(self) -> list[SpanRecord]:
        """Pass-through to the wrapped in-memory tracer's span list.

        Exposed so tests, :func:`build_trace_tree`, and existing call
        sites that introspect ``tracer.spans`` keep working without
        knowing they hold a :class:`JsonlTracer`.
        """
        return self._inner.spans

    # --------------------------------------------------------------- protocol ##
    @contextmanager
    def run(self, design_id: str, *, name: str = "run") -> Iterator[Span]:
        """Open the root span; flush + close the file when the run ends.

        The mirror runs **after** the wrapped ``_inner.run`` exits — the
        inner's ``finally`` calls ``_close`` which appends the span to
        ``_inner.spans``. Mirroring before that would miss every span
        the run produced.
        """
        try:
            with self._inner.run(design_id, name=name) as span:
                yield span
        finally:
            self._mirror_latest()
            self._close_file()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Open a child span; mirror it to the JSONL after the inner
        contextmanager closes (which is when ``_inner.spans`` grows)."""
        try:
            with self._inner.span(
                name, kind=kind, attributes=attributes,
            ) as span:
                yield span
        finally:
            self._mirror_latest()

    # -------------------------------------------------------------- internal ##
    def _mirror_latest(self) -> None:
        """Append every newly-appeared span to the JSONL.

        The wrapped ``_inner`` may have closed several spans since the
        last mirror (the ``_close`` cleanup pops any forgotten
        children off the stack — see ``InMemoryTracer._close`` lines
        268-277). Walk forward from the file's last-written cursor so
        a bulk close still produces one line per span in order.
        """
        with self._lock:
            self._ensure_open()
            assert self._fh is not None
            written = getattr(self, "_written_count", 0)
            new = self._inner.spans[written:]
            for record in new:
                self._fh.write(span_record_to_jsonl_line(record))
            self._fh.flush()
            self._written_count = written + len(new)

    def _ensure_open(self) -> None:
        if self._fh is not None:
            return
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode: re-opening the same path on a resumed run picks
        # up where the previous tracer left off. The TUI's tail reader
        # uses byte offsets so prior content stays intact.
        self._fh = self.jsonl_path.open("a", encoding="utf-8")

    def _close_file(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
            self._fh.close()
        finally:
            self._fh = None


def span_record_to_jsonl_line(record: SpanRecord) -> str:
    """Serialize a :class:`SpanRecord` to one NDJSON line (terminated).

    ``datetime`` → ISO-8601 with timezone, ``Enum`` → ``.value``, every
    other field as-is. Attributes pass through; callers are responsible
    for keeping their values JSON-serializable (the MODEL span
    population in Phase 2 only stores str/int/float).
    """
    payload = asdict(record)
    return json.dumps(payload, default=_json_default, separators=(",", ":")) + "\n"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(
        f"don't know how to serialize {type(value).__name__} to JSON: {value!r}"
    )
