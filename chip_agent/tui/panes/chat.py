"""Chat REPL pane (F14.1).

Mirror of ``cli_chat.ChatSession``'s slash-command + transcript-
persistence semantics in a Textual widget. The pane owns:

* An :class:`Input` field at the bottom for the operator to type.
* A scrollable :class:`RichLog` filling the rest, where streamed
  assistant tokens land.
* The transcript state (``list[ChatTurn]``) — persisted to
  ``<run-dir>/chat.transcript.md`` after every turn, identically to
  ``cli_chat.ChatSession._persist_transcript``.

Streaming + spec materialisation run in worker threads
(``chip_agent.tui.workers.chat_worker.stream_chat_reply`` and
``materialise_spec``); the pane listens for :class:`ChatChunk` /
:class:`ChatStreamDone` / :class:`SpecMaterialised` /
:class:`ChatStreamError` messages and updates the UI on the main thread.

The pane reuses the existing transcript serialiser
(``cli_chat.format_transcript``) so the on-disk shape stays identical
to what ``chip-agent chat`` produces — a TUI-minted transcript is
indistinguishable from a CLI-minted one.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RichLog, Static

from chip_agent.agents.spec_intake import SpecIntakeAgent
from chip_agent.cli_chat import (
    CHAT_SYSTEM_PROMPT,
    ChatTurn,
    format_transcript,
    load_transcript,
)
from chip_agent.design_state import ModelRouter
from chip_agent.settings import ConstraintDefaults
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tui.messages import (
    ChatChunk,
    ChatStreamDone,
    ChatStreamError,
    SpecMaterialised,
)
from chip_agent.tui.workers.chat_worker import stream_chat_reply
from chip_agent.tui.workers.spec_worker import materialise_spec

__all__ = ["ChatPane"]


_HELP_TEXT = (
    "/run     materialise a Spec from the transcript.\n"
    "/cancel  exit an in-progress /run intake loop and return to chat.\n"
    "/show    print the current transcript.\n"
    "/exit    quit the app.\n"
    "/help    show this help.\n"
)


class ChatPane(Vertical):
    """Container widget — :class:`RichLog` on top, :class:`Input` below."""

    DEFAULT_CSS = """
    ChatPane {
        height: 1fr;
    }

    ChatPane > #chat-log {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
    }

    ChatPane > #chat-prompt-label {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    ChatPane > #chat-input {
        height: 3;
        border: solid $accent;
    }
    """

    def __init__(
        self,
        *,
        router: ModelRouter,
        store: SqliteArtifactStore,
        design_id: str,
        name: str,
        transcript_path: Path,
        defaults: ConstraintDefaults | None = None,
    ) -> None:
        super().__init__()
        self.router = router
        self.store = store
        self.design_id = design_id
        self.design_name = name
        self.transcript_path = transcript_path
        self.defaults = defaults
        self.turns: list[ChatTurn] = []
        # Rolling buffer of the in-progress assistant turn — appended via
        # :class:`ChatChunk` messages, finalised on :class:`ChatStreamDone`.
        self._pending_assistant: list[str] = []
        self._streaming = False
        # When /run surfaces a ClarifyingQuestion, the agent is held here
        # so the next user message re-enters intake (with the same budget
        # counter). Cleared on SpecMaterialised / ChatStreamError / /cancel.
        self._intake_agent: SpecIntakeAgent | None = None
        # Load any pre-existing transcript so re-launching against an
        # existing run-dir picks up where the operator left off.
        if self.transcript_path.exists():
            self.turns = load_transcript(self.transcript_path)

    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", wrap=True, markup=False, highlight=False)
        yield Static(_USER_PREFIX, id="chat-prompt-label")
        yield Input(placeholder="Describe a module, or /help …", id="chat-input")

    # ------------------------------------------------------------- lifecycle
    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write(
            "chip-agent TUI — describe a module, then /run to mint a Spec.",
        )
        log.write(f"design_id: {self.design_id}")
        if self.turns:
            log.write("(resuming prior transcript)")
            for t in self.turns:
                log.write(_render_turn_inline(t))
        self.query_one(Input).focus()

    # ----------------------------------------------------------- input flow
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.rstrip("\n")
        event.input.value = ""
        if not text.strip():
            return
        if self._streaming:
            # Ignore submissions while a worker is mid-flight; the chunk
            # stream is appending to the same log and we don't want
            # interleaved turns.
            return

        if text.startswith("/"):
            self._handle_slash(text)
            return

        # Append user turn + persist regardless of which worker we dispatch.
        self.turns.append(ChatTurn(role="user", text=text))
        self._persist_transcript()
        log = self.query_one(RichLog)
        log.write(f"You: {text}")

        # If a /run intake is awaiting an answer, re-enter intake with the
        # accumulated transcript so the SpecIntakeAgent's budget decrements
        # naturally. Otherwise fall back to the chat streamer.
        if self._intake_agent is not None:
            log.write("(/run) re-evaluating with your answer …")
            self._streaming = True
            self.run_worker(
                self._continue_intake, thread=True, exclusive=False,
                group="spec-intake",
            )
            return

        log.write("Assistant: …")
        self._streaming = True
        # Spawn a thread worker — `run_worker` on the widget itself returns
        # a Worker that the test harness can observe via ``widget.workers``.
        self.run_worker(
            self._stream_user_turn, thread=True, exclusive=False,
            group="chat-stream",
        )

    def _handle_slash(self, line: str) -> None:
        cmd = line.strip().split()[0].lower()
        log = self.query_one(RichLog)
        if cmd == "/exit":
            self.app.exit()
            return
        if cmd == "/help":
            log.write(_HELP_TEXT.rstrip("\n"))
            return
        if cmd == "/show":
            if not self.turns:
                log.write("(transcript is empty)")
                return
            log.write(format_transcript(
                self.design_id, self.design_name, self.turns,
            ).rstrip("\n"))
            return
        if cmd == "/run":
            self._dispatch_run()
            return
        if cmd == "/cancel":
            if self._intake_agent is None:
                log.write("(no /run intake in progress)")
                return
            self._intake_agent = None
            log.write("(/run) intake cancelled — back to chat.")
            return
        log.write(f"unknown command: {cmd} (try /help)")

    # ---------------------------------------------------------- workers
    def _stream_user_turn(self) -> None:
        prompt = _assemble_chat_prompt(self.turns)
        stream_chat_reply(
            app=self.app,
            pane=self,
            router=self.router,
            prompt=prompt,
            system_prompt=CHAT_SYSTEM_PROMPT,
        )

    def _dispatch_run(self) -> None:
        user_turns = [t for t in self.turns if t.role == "user"]
        if not user_turns:
            self.query_one(RichLog).write(
                "nothing to materialise yet — describe the module first.",
            )
            return
        self.query_one(RichLog).write("(/run) materialising spec …")
        # Fresh /run always starts a fresh intake session — clarifying
        # budget resets. The mid-loop continuation path reuses the agent.
        self._intake_agent = SpecIntakeAgent(
            router=self.router,
            design_id=self.design_id,
            defaults=self.defaults,
        )
        self._streaming = True
        self.run_worker(
            self._continue_intake, thread=True, exclusive=False,
            group="spec-intake",
        )

    def _continue_intake(self) -> None:
        """Worker body — re-evaluate intake with the current transcript.

        Shared by /run (fresh agent) and the answer-to-clarifying-question
        path (re-using the pane's agent). The pane owns the agent so the
        budget decrements across rounds.
        """
        raw_text = _assemble_raw_text(self.turns)
        materialise_spec(
            app=self.app,
            pane=self,
            router=self.router,
            store=self.store,
            design_id=self.design_id,
            defaults=self.defaults,
            raw_text=raw_text,
            agent=self._intake_agent,
        )

    # ---------------------------------------------------------- message handlers
    def on_chat_chunk(self, message: ChatChunk) -> None:
        """One streaming token — append in-place to the rolling assistant turn."""
        self._pending_assistant.append(message.delta)

    def on_chat_stream_done(self, message: ChatStreamDone) -> None:
        """Stream complete — render the full assistant turn + persist."""
        reply = message.reply
        if reply:
            self.turns.append(ChatTurn(role="assistant", text=reply))
            self._persist_transcript()
        log = self.query_one(RichLog)
        log.write(f"Assistant: {reply}" if reply else "Assistant: (empty)")
        self._pending_assistant.clear()
        self._streaming = False

    def on_chat_stream_error(self, message: ChatStreamError) -> None:
        log = self.query_one(RichLog)
        log.write(f"[!] streaming error: {message.message}")
        self.app.notify(
            f"streaming error: {message.message}", severity="error",
        )
        self._pending_assistant.clear()
        self._streaming = False
        # Drop intake state on error — a fresh /run starts a fresh agent.
        self._intake_agent = None

    def on_spec_materialised(self, message: SpecMaterialised) -> None:
        spec = message.spec
        log = self.query_one(RichLog)
        log.write(
            f"spec materialised: {spec.artifact_id} "
            f"(content hash {spec.content_hash[:12]}…)",
        )
        log.write("[Ctrl+R] run the pipeline   [Ctrl+Q] quit")
        self._streaming = False
        self._intake_agent = None
        # The app keeps running so the operator can press [R] to drive
        # the spine in-place. The SpecMaterialised message bubbles up to
        # the app, which records the spec for the [R] action.

    # ---------------------------------------------------------- persistence
    def _persist_transcript(self) -> None:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript_path.write_text(
            format_transcript(self.design_id, self.design_name, self.turns),
            encoding="utf-8",
        )


# --------------------------------------------------------------------------- #
# Module-level helpers — copies of the cli_chat.ChatSession internals so the
# pane can reuse them without depending on the private API surface.
# --------------------------------------------------------------------------- #
_USER_PREFIX = "You> "


def _render_turn_inline(turn: ChatTurn) -> str:
    label = "You" if turn.role == "user" else "Assistant"
    return f"{label}: {turn.text.strip()}"


def _assemble_chat_prompt(turns: list[ChatTurn]) -> str:
    """Flatten the transcript into a single user-prompt string.

    Mirrors :meth:`cli_chat.ChatSession._assemble_chat_prompt`.
    """
    lines: list[str] = []
    for t in turns:
        label = "User" if t.role == "user" else "Assistant"
        lines.append(f"{label}: {t.text.strip()}")
    return "\n".join(lines)


def _assemble_raw_text(turns: list[ChatTurn]) -> str:
    """Build the ``raw_text`` payload :meth:`SpecIntakeAgent.intake` sees.

    Mirrors :func:`cli_chat._assemble_raw_text`.
    """
    return "\n\n".join(
        f"{'User' if t.role == 'user' else 'Assistant'}: {t.text.strip()}"
        for t in turns
    )
