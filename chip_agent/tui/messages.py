"""Custom Textual ``Message`` types for cross-pane communication.

The TUI runs every blocking call (``router.stream``, ``cmd_run``,
``audit.events``, …) inside a worker thread. Workers post these
messages back to the main thread via ``App.call_from_thread`` so
widgets can react without touching the worker's thread directly.

Message subclasses are plain classes (not dataclasses) — Textual's
``Message`` base does its own metaclass + bubbling work in
``__init__``/``__init_subclass__``, and stacking ``@dataclass`` on top
trips ``RecursionError`` in some Textual versions. The plain-class
shape is simpler and matches the Textual examples.

F14.1 only needs the chat-streaming messages; later features extend
this module with ``StageAdvanced`` (F14.2), ``PipelineStarted`` /
``PipelinePaused`` / ``RunFailed`` (F14.3), ``AuditEventBatch`` (F14.4),
``ExportsTreeRefreshed`` (F14.5), and ``SignoffReportReady`` (F14.6).
"""

from __future__ import annotations

from pathlib import Path

from textual.message import Message

from chip_agent.design_state import DesignState, ModelInvocation, Spec
from chip_agent.obs.audit_log import AuditEvent
from chip_agent.obs.tracing import SpanRecord

__all__ = [
    "AuditChainStatus",
    "AuditEventBatch",
    "ChatChunk",
    "ChatStreamDone",
    "ChatStreamError",
    "ExportPreviewRequested",
    "PipelineCompleted",
    "PipelinePaused",
    "ReasoningEventBatch",
    "RunFailed",
    "SpecMaterialised",
    "StageAdvanced",
]


class ChatChunk(Message):
    """One streaming token (or batch of tokens) from ``router.stream``.

    Posted by the chat worker after each ``StreamChunk`` yields. ``delta``
    is the new text fragment; ``invocation`` is only set on the terminal
    chunk and carries the finalised ``ModelInvocation`` (token counts,
    cost). Mid-stream chunks have ``invocation=None``.
    """

    def __init__(
        self, *, delta: str, invocation: ModelInvocation | None = None,
    ) -> None:
        super().__init__()
        self.delta = delta
        self.invocation = invocation


class ChatStreamDone(Message):
    """The chat worker finished streaming this assistant turn.

    Posted exactly once per ``router.stream`` call after the terminal
    chunk. ``reply`` is the accumulated text — the ChatPane uses it to
    append the assistant turn to its in-memory transcript before
    persisting to ``<run-dir>/chat.transcript.md``.
    """

    def __init__(self, *, reply: str) -> None:
        super().__init__()
        self.reply = reply


class ChatStreamError(Message):
    """Streaming bailed (connection, unreachable backend, etc.).

    The ChatPane renders the message in a notification + restores the
    input field so the operator can retry.
    """

    def __init__(self, *, message: str) -> None:
        super().__init__()
        self.message = message


class SpecMaterialised(Message):
    """``/run`` produced a :class:`Spec`. App should advance state.

    F14.1: the app exits with the Spec ref on its outcome dataclass.
    F14.3 will instead transition to a "spec ready" state and enable
    the ``[R]`` keybind.
    """

    def __init__(self, *, spec: Spec) -> None:
        super().__init__()
        self.spec = spec


class StageAdvanced(Message):
    """The polling worker observed a fresh :class:`DesignState`.

    Posted on every tick where the checkpoint contents differ from the
    previous tick (or, on the first tick that finds a checkpoint at
    all). The :class:`PipelinePane` updates its stage cells from the
    contained ``DesignState`` snapshot. F14.4's audit pane will also
    listen for this message so audit + pipeline stay in lockstep.
    """

    def __init__(self, *, design: DesignState) -> None:
        super().__init__()
        self.design = design


class PipelinePaused(Message):
    """``cmd_run`` returned — the spine paused at the human gate.

    F14.3: posted by the run worker when ``cmd_run`` completes
    successfully. ``state`` is the :class:`DesignState` returned by
    ``cmd_run`` (status will be ``AWAITING_HUMAN``, current_stage will
    be ``SIGNOFF``). The app enables the ``[A]`` keybind on this message.
    """

    def __init__(self, *, state: DesignState) -> None:
        super().__init__()
        self.state = state


class PipelineCompleted(Message):
    """``cmd_resume`` returned — the spine drove past the human gate to GDSII.

    F14.3: posted by the resume worker after ``cmd_resume`` returns.
    ``state`` is the final :class:`DesignState` (status COMPLETED,
    current_stage GDSII). The app exits with a rich outcome carrying
    spec / paused / final.
    """

    def __init__(self, *, state: DesignState) -> None:
        super().__init__()
        self.state = state


class RunFailed(Message):
    """A drive worker (``cmd_run`` or ``cmd_resume``) raised.

    F14.3: posted by either drive worker when the underlying CLI
    function raises. ``stage`` is ``"run"`` or ``"resume"`` so the app
    can rephrase the notification; ``message`` is ``repr(exc)`` —
    Textual's notify renders it as-is.
    """

    def __init__(self, *, stage: str, message: str) -> None:
        super().__init__()
        self.stage = stage
        self.message = message


class AuditEventBatch(Message):
    """The audit poller observed new appended events.

    F14.4: posted by the AuditPane's polling tick when one or more
    events with ``sequence > last_seen`` appear in the log. Carries
    the raw :class:`AuditEvent` objects so the pane can render rows
    + payload summaries without re-querying the DB.
    """

    def __init__(self, *, events: list[AuditEvent]) -> None:
        super().__init__()
        self.events = events


class ExportPreviewRequested(Message):
    """The exports tree posted a file-selection — preview pane should
    render the body.

    F14.5: posted when the user selects a file in the
    :class:`DirectoryTree`. The pane's own handler resolves the path
    via the lexer dispatch + updates the preview ``Static``. Exposed
    as a separate message so the F14.6 signoff dashboard can also
    request previews (e.g. "see source" → focus the matching JSON).
    """

    def __init__(self, *, path: Path) -> None:
        super().__init__()
        self.path = path


class ReasoningEventBatch(Message):
    """The reasoning tape poller observed new :class:`SpanRecord` lines.

    F22.2-D: posted by :class:`ReasoningTape`'s polling tick whenever
    new spans appear in ``runs/<design_id>/reasoning.jsonl``. The pane
    renders one row per span; mid-stream chunks aren't surfaced here
    (the gateway streams via ``ChatChunk`` for the chat-side flow).
    """

    def __init__(self, *, events: list[SpanRecord]) -> None:
        super().__init__()
        self.events = events


class AuditChainStatus(Message):
    """Audit chain validity changed (or first observation).

    F14.4: posted by the AuditPane's polling tick whenever the
    chain-validity badge text changes — either on first read, on
    new appended events (count delta), or if ``SqliteAuditLog.verify``
    flips between valid + tampered. ``tone`` is ``"valid" | "invalid" |
    None``; the pane adds ``audit-<tone>`` as a CSS class.
    """

    def __init__(self, *, text: str, tone: str | None) -> None:
        super().__init__()
        self.text = text
        self.tone = tone
