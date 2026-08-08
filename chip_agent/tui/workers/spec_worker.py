"""Spec-materialisation worker.

Wraps :meth:`SpecIntakeAgent.intake` in a worker thread so the TUI
doesn't block while the (potentially slow) frontier-model call runs.
Posts the outcome back via :class:`SpecMaterialised` (the Spec was
minted) or :class:`ChatStreamError` (intake errored out).

Clarifying-question handling: when intake returns a
``ClarifyingQuestion`` the question is surfaced as an assistant turn
via :class:`ChatChunk` + :class:`ChatStreamDone`. The caller is
expected to keep the :class:`SpecIntakeAgent` alive and pass it back
via the ``agent`` arg on the next call (e.g. once the operator has
typed an answer), so the agent's clarifying budget decrements naturally
across rounds and the loop stays bounded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chip_agent.agents.spec_intake import ClarifyingQuestion, SpecIntakeAgent
from chip_agent.design_state import ModelRouter, Spec
from chip_agent.settings import ConstraintDefaults
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tui.messages import (
    ChatChunk,
    ChatStreamDone,
    ChatStreamError,
    SpecMaterialised,
)

if TYPE_CHECKING:
    from typing import Any

    from textual.app import App
    from textual.message_pump import MessagePump


__all__ = ["materialise_spec"]


def materialise_spec(
    *,
    app: App[Any],
    pane: MessagePump,
    router: ModelRouter,
    store: SqliteArtifactStore,
    design_id: str,
    defaults: ConstraintDefaults | None,
    raw_text: str,
    agent: SpecIntakeAgent | None = None,
) -> None:
    """Drive a single ``SpecIntakeAgent.intake`` call and post the outcome.

    Intended to be called via ``widget.run_worker(..., thread=True)``.
    Messages are posted to ``pane`` (not the app) so the pane's bubbling-up
    handlers fire — see ``chat_worker`` docstring for the why.

    ``agent`` lets the caller persist intake state (the clarifying budget)
    across rounds. When ``None``, a fresh agent is created with the default
    budget; subsequent rounds in the same /run session should pass back the
    agent the pane is holding so the budget decrements correctly.
    """
    try:
        if agent is None:
            agent = SpecIntakeAgent(
                router=router, design_id=design_id, defaults=defaults,
            )
        outcome = agent.intake(raw_text)
    except Exception as e:
        app.call_from_thread(
            pane.post_message,
            ChatStreamError(message=f"{type(e).__name__}: {e}"),
        )
        return
    if isinstance(outcome, Spec):
        # SQLite connections can't be shared across threads — persist on the
        # main thread before posting the SpecMaterialised message.
        def _persist_and_post() -> None:
            store.put(outcome)
            pane.post_message(SpecMaterialised(spec=outcome))

        app.call_from_thread(_persist_and_post)
        return
    # ClarifyingQuestion: surface as an assistant turn so the operator
    # can keep chatting to refine. F14.1 simplification — see module
    # docstring.
    assert isinstance(outcome, ClarifyingQuestion)
    question_text = f"(intake) {outcome.question}"
    app.call_from_thread(pane.post_message, ChatChunk(delta=question_text))
    app.call_from_thread(pane.post_message, ChatStreamDone(reply=question_text))
