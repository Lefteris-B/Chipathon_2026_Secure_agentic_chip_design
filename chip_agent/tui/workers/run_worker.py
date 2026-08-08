"""Run + Resume drive workers (F14.3).

Wrap the existing :func:`cmd_run` / :func:`cmd_resume` CLI entry points
in worker threads so the TUI's main loop doesn't block while the spine
drives. Each wrapper:

* Redirects ``stdout`` / ``stderr`` to ``/dev/null`` for the duration
  of the call — Textual owns the terminal and a stray print would
  corrupt the screen. The CLI's ``_print_run`` / ``_print_resume``
  output is discarded (the TUI surfaces equivalent status via the
  pipeline pane + notifications).
* Catches every exception and posts a typed
  :class:`~chip_agent.tui.messages.RunFailed` message instead of
  crashing the worker silently.
* On success, posts :class:`PipelinePaused` (run worker) or
  :class:`PipelineCompleted` (resume worker) with the produced
  :class:`DesignState` so the app can transition to the next state.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import traceback
from typing import TYPE_CHECKING, Any

from chip_agent.cli import RunArgs, _RunPaths, cmd_resume, cmd_run
from chip_agent.design_state import DesignStatus
from chip_agent.obs.jsonl_tracer import JsonlTracer
from chip_agent.tui.messages import (
    PipelineCompleted,
    PipelinePaused,
    RunFailed,
)

if TYPE_CHECKING:
    from textual.app import App
    from textual.message_pump import MessagePump


__all__ = ["resume_pipeline", "run_pipeline"]


def _attach_jsonl_tracer(args: RunArgs) -> RunArgs:
    """F22.2: wire a JsonlTracer into ``args`` so every LLM call and
    every typed tool service invocation appends a SpanRecord line to
    ``runs/<design_id>/reasoning.jsonl`` — the source-of-truth file the
    ReasoningTape pane tails at 500 ms.

    ``RunArgs`` is a frozen dataclass, so we return a *new* ``RunArgs``
    with ``tracer`` populated rather than mutating in place. A
    pre-existing ``args.tracer`` is preserved untouched (tests inject
    InMemoryTracer; we never overwrite a caller-supplied tracer).
    """
    if args.tracer is not None:
        return args
    paths = _RunPaths(args.run_dir)
    design_id = args.design_id or args.name
    return dataclasses.replace(
        args, tracer=JsonlTracer(paths.reasoning_jsonl(design_id))
    )


def run_pipeline(
    *,
    app: App[Any],
    pane: MessagePump,
    args: RunArgs,
) -> None:
    """Run ``cmd_run(args)`` and post the paused state back to the pane.

    Intended to be called via ``widget.run_worker(..., thread=True)``.
    """
    args = _attach_jsonl_tracer(args)
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            outcome = cmd_run(args)
    except Exception as e:
        app.call_from_thread(
            pane.post_message,
            RunFailed(
                stage="run",
                message=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
            ),
        )
        return
    app.call_from_thread(
        pane.post_message,
        PipelinePaused(state=outcome.paused_state),
    )


def resume_pipeline(
    *,
    app: App[Any],
    pane: MessagePump,
    args: RunArgs,
) -> None:
    """Run ``cmd_resume(args)`` and post the final state back to the pane.

    Intended to be called via ``widget.run_worker(..., thread=True)``.
    """
    args = _attach_jsonl_tracer(args)
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            outcome = cmd_resume(args)
    except Exception as e:
        app.call_from_thread(
            pane.post_message,
            RunFailed(
                stage="resume",
                message=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
            ),
        )
        return
    # F23.5: an interactive-repair resume can re-pause (needs more guidance)
    # or end blocked instead of completing to GDS. Only a COMPLETED run is a
    # PipelineCompleted; anything else routes back as a pause so the app can
    # re-open the repair modal (pending request) or surface the block.
    final = outcome.final_state
    message: PipelineCompleted | PipelinePaused = (
        PipelineCompleted(state=final)
        if final.status is DesignStatus.COMPLETED
        else PipelinePaused(state=final)
    )
    app.call_from_thread(pane.post_message, message)
