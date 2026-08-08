"""Chat streaming worker (F14.1).

Wraps ``router.stream(TaskType.SPEC_INTAKE, ...)`` in a worker thread
so the TUI's main loop doesn't block. Each yielded ``StreamChunk`` is
forwarded back to the main thread via ``App.call_from_thread`` posting
a :class:`~chip_agent.tui.messages.ChatChunk` **to the originating pane**.

Why post to the pane rather than the App: Textual messages bubble *up*
from the widget that posts them, so a message posted to ``App`` is
seen by App-level handlers but never reaches widget handlers. Workers
must therefore post via the pane so its ``on_chat_chunk`` /
``on_chat_stream_done`` handlers fire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chip_agent.design_state import ModelRouter, TaskType
from chip_agent.tui.messages import ChatChunk, ChatStreamDone, ChatStreamError

if TYPE_CHECKING:
    from typing import Any

    from textual.app import App
    from textual.message_pump import MessagePump


__all__ = ["stream_chat_reply"]


def stream_chat_reply(
    *,
    app: App[Any],
    pane: MessagePump,
    router: ModelRouter,
    prompt: str,
    system_prompt: str,
) -> None:
    """Run ``router.stream(SPEC_INTAKE)`` synchronously; post chunks to ``pane``.

    Intended to be called via ``widget.run_worker(..., thread=True)``.
    Catches every exception so a streaming failure surfaces as a typed
    :class:`ChatStreamError` message instead of killing the worker
    silently. The accumulated reply text is posted as
    :class:`ChatStreamDone` after the terminal chunk so the pane can
    persist the assistant turn to its in-memory transcript.
    """
    accumulated: list[str] = []
    try:
        for chunk in router.stream(
            TaskType.SPEC_INTAKE,
            context={"prompt": prompt, "system": system_prompt},
        ):
            if chunk.delta:
                accumulated.append(chunk.delta)
                app.call_from_thread(
                    pane.post_message,
                    ChatChunk(delta=chunk.delta, invocation=chunk.invocation),
                )
    except Exception as e:
        app.call_from_thread(
            pane.post_message,
            ChatStreamError(message=f"{type(e).__name__}: {e}"),
        )
        return
    app.call_from_thread(
        pane.post_message,
        ChatStreamDone(reply="".join(accumulated).strip()),
    )
