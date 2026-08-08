"""Tracing primitives for the observability plane (F7.1).

The chip-agent runs are sprawls of model + tool + gate calls; the trace
captures the tree of those calls so a single ``design_id`` can be replayed,
costed, and audited. CLAUDE.md pins **Langfuse** as the storage backend,
but the surface is small enough to expose as a :class:`Tracer` Protocol
so the codebase doesn't depend on the SDK — an :class:`InMemoryTracer`
backs the unit tests, a :class:`NoopTracer` skips recording for the
production-disabled path, and an adapter (out of scope here) forwards
spans to a real Langfuse client.

Every recorded span captures the F7.1-required signals:

* **Tokens** — prompt + completion (model spans only).
* **Latency** — wall-clock duration in milliseconds.
* **Cost** — USD if the span has a cost setter wired.
* **Escalation level** — :class:`EscalationLevel` of the loop the span
  ran inside, so the trace tree can be sliced by loop tier.

Spans form a tree by parent pointer. ``design_id`` is inherited from the
enclosing :meth:`Tracer.run` so child spans don't have to thread it; a
single tracer can host multiple concurrent runs (different ``design_id``)
and the :func:`build_trace_tree` helper slices the recorded spans into
one tree per run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from chip_agent.design_state import EscalationLevel

__all__ = [
    "InMemoryTracer",
    "NoopTracer",
    "Span",
    "SpanKind",
    "SpanRecord",
    "SpanStatus",
    "TraceNode",
    "Tracer",
    "build_trace_tree",
]


class SpanKind(StrEnum):
    """What kind of work a span describes — used for trace-tree slicing."""

    RUN = "run"           # root span: one design's end-to-end execution
    STAGE = "stage"       # SPEC / PLAN / RTL / SYNTH / PHYSICAL / SIGNOFF / GDSII
    LOOP = "loop"         # INNER / OUTER repair attempt
    MODEL = "model"       # one LLM call (tokens + cost live here)
    TOOL = "tool"         # one sandboxed EDA tool invocation
    GATE = "gate"         # one decide_transition call
    HUMAN = "human"       # human-review pause (no timing meaningful)


class SpanStatus(StrEnum):
    """Terminal state of a span."""

    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class SpanRecord:
    """Immutable snapshot of a closed span — what a backend persists."""

    span_id: str
    parent_id: str | None
    name: str
    kind: SpanKind
    design_id: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    escalation: EscalationLevel | None = None
    status: SpanStatus = SpanStatus.OK
    error: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


class Span:
    """Live span returned by :meth:`Tracer.span` — closed by the
    context manager. Most setters are no-ops outside the call; callers
    only see this object inside ``with tracer.span(...) as span:``.
    """

    def __init__(
        self,
        *,
        span_id: str,
        parent_id: str | None,
        name: str,
        kind: SpanKind,
        design_id: str,
        started_at: datetime,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self._record = SpanRecord(
            span_id=span_id,
            parent_id=parent_id,
            name=name,
            kind=kind,
            design_id=design_id,
            started_at=started_at,
            attributes=dict(attributes or {}),
        )
        self._monotonic_start = time.perf_counter()

    # --- Setters the production code calls ---------------------------------- #
    def set_tokens(self, *, prompt: int, completion: int) -> None:
        self._record.prompt_tokens = prompt
        self._record.completion_tokens = completion

    def set_cost(self, cost_usd: float) -> None:
        self._record.cost_usd = cost_usd

    def set_escalation(self, level: EscalationLevel) -> None:
        self._record.escalation = level

    def set_attribute(self, key: str, value: Any) -> None:
        self._record.attributes[key] = value

    def record_error(self, exc: BaseException) -> None:
        self._record.status = SpanStatus.ERROR
        self._record.error = f"{type(exc).__name__}: {exc}"

    # --- Read-only accessors the tracer / tests use ------------------------- #
    @property
    def id(self) -> str:
        return self._record.span_id

    @property
    def design_id(self) -> str:
        return self._record.design_id

    @property
    def parent_id(self) -> str | None:
        return self._record.parent_id

    @property
    def kind(self) -> SpanKind:
        return self._record.kind

    @property
    def attributes(self) -> dict[str, Any]:
        return self._record.attributes

    @property
    def record(self) -> SpanRecord:
        return self._record

    def _finalize(self) -> SpanRecord:
        self._record.ended_at = datetime.now(UTC)
        self._record.duration_ms = (time.perf_counter() - self._monotonic_start) * 1000.0
        return self._record


class Tracer(Protocol):
    """Open a run, then open child spans inside it.

    Implementations are free to drop spans (:class:`NoopTracer`), keep
    them in memory (:class:`InMemoryTracer`), or forward to a real
    backend like Langfuse. The Protocol is intentionally narrow so the
    production code doesn't depend on which one is wired.
    """

    def run(self, design_id: str, *, name: str = ...) -> AbstractContextManager[Span]:
        """Open the root span for one design's end-to-end execution."""
        ...

    def span(
        self,
        name: str,
        *,
        kind: SpanKind,
        attributes: dict[str, Any] | None = ...,
    ) -> AbstractContextManager[Span]:
        """Open a child span under the currently-active run."""
        ...


class InMemoryTracer:
    """Tracer that records every span into ``spans`` for later inspection.

    Concurrent runs on the same tracer instance are allowed: each
    :meth:`run` opens a new root with its own ``design_id``, and
    :meth:`span` looks up the **current** parent via the thread-local
    span stack so nested ``with`` blocks compose without callers having
    to thread parent ids manually.
    """

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []
        self._active: list[Span] = []
        self._open: list[Span] = []  # not-yet-closed spans, for safety on errors

    # ------------------------------------------------------------------ run ##
    @contextmanager
    def run(self, design_id: str, *, name: str = "run") -> Iterator[Span]:
        if not design_id:
            raise ValueError("design_id must be non-empty")
        span = Span(
            span_id=_new_id(),
            parent_id=None,
            name=name,
            kind=SpanKind.RUN,
            design_id=design_id,
            started_at=datetime.now(UTC),
        )
        self._active.append(span)
        self._open.append(span)
        try:
            yield span
        except BaseException as exc:
            span.record_error(exc)
            raise
        finally:
            self._close(span)

    # ----------------------------------------------------------------- span ##
    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        if not self._active:
            raise RuntimeError(
                f"span({name!r}) opened with no enclosing run — call "
                f"`with tracer.run(design_id): ...` first"
            )
        parent = self._active[-1]
        span = Span(
            span_id=_new_id(),
            parent_id=parent.id,
            name=name,
            kind=kind,
            design_id=parent.design_id,
            started_at=datetime.now(UTC),
            attributes=attributes,
        )
        self._active.append(span)
        self._open.append(span)
        try:
            yield span
        except BaseException as exc:
            span.record_error(exc)
            raise
        finally:
            self._close(span)

    # -------------------------------------------------------------- internal ##
    def _close(self, span: Span) -> None:
        # Pop spans off the active stack until we find this one — a robust
        # close that survives a caller forgetting to wrap an inner span.
        while self._active:
            top = self._active.pop()
            self.spans.append(top._finalize())
            if top is span:
                break
        # Drop from the open list if still present.
        self._open = [s for s in self._open if s is not span]


class NoopTracer:
    """Tracer that records nothing — for the "tracing disabled" path."""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []  # always empty, exposed for API parity

    @contextmanager
    def run(self, design_id: str, *, name: str = "run") -> Iterator[Span]:
        if not design_id:
            raise ValueError("design_id must be non-empty")
        yield _noop_span(name=name, kind=SpanKind.RUN, design_id=design_id)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        del attributes
        yield _noop_span(name=name, kind=kind, design_id="-")


def _noop_span(*, name: str, kind: SpanKind, design_id: str) -> Span:
    return Span(
        span_id="noop",
        parent_id=None,
        name=name,
        kind=kind,
        design_id=design_id,
        started_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# Trace-tree helpers — F7.1 AC ("one run produces a complete trace tree")
# --------------------------------------------------------------------------- #
@dataclass
class TraceNode:
    """Typed view of one span plus its children in the recorded tree."""

    record: SpanRecord
    children: list[TraceNode] = field(default_factory=list)

    def walk(self) -> Iterator[SpanRecord]:
        """Pre-order traversal — yields self then each child's subtree."""
        yield self.record
        for c in self.children:
            yield from c.walk()


def build_trace_tree(
    spans: list[SpanRecord],
    *,
    design_id: str,
) -> list[TraceNode]:
    """Filter ``spans`` by ``design_id`` and return the recorded forest.

    Spans without a parent become roots; child lists are sorted by
    ``started_at`` to give the tree a deterministic shape.
    """
    if not design_id:
        raise ValueError("design_id must be non-empty")
    nodes: dict[str, TraceNode] = {
        s.span_id: TraceNode(record=s)
        for s in spans
        if s.design_id == design_id
    }
    roots: list[TraceNode] = []
    for node in nodes.values():
        parent_id = node.record.parent_id
        if parent_id is not None and parent_id in nodes:
            nodes[parent_id].children.append(node)
        else:
            roots.append(node)
    for node in nodes.values():
        node.children.sort(key=lambda n: n.record.started_at)
    roots.sort(key=lambda n: n.record.started_at)
    return roots


def _new_id() -> str:
    return uuid.uuid4().hex
