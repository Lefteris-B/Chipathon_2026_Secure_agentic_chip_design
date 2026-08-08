"""F11.3 — ``chip-agent chat`` REPL.

Streams a clarifying-question dialog through ``router.stream`` (F11.2),
persists the transcript to ``<run_dir>/chat.transcript.md``, and on the
``/run`` slash-command materialises a typed :class:`Spec` via the
existing :class:`SpecIntakeAgent` for handoff to ``cmd_run``.

The REPL lives **outside** the LangGraph spine: chat is a stateful
conversation, the SPEC node is single-shot. The hand-off contract is
"the chat session puts a :class:`Spec` artifact in the store under the
same ``design_id``; a subsequent ``chip-agent run --design-id <id>``
picks it up without re-running intake." This keeps the spine's state
machine simple and lets the chat surface evolve independently.

System prompt for the chat persona is held in :data:`CHAT_SYSTEM_PROMPT`;
the spec-materialisation pass still uses
:attr:`SpecIntakeAgent.SYSTEM_PROMPT` so the on-disk Spec's normalised
shape matches what a ``--spec`` file run would produce.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from chip_agent.agents.spec_intake import (
    ClarifyingQuestion,
    SpecIntakeAgent,
)
from chip_agent.design_state import ModelRouter, Spec, TaskType
from chip_agent.settings import ConstraintDefaults
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = [
    "CHAT_SYSTEM_PROMPT",
    "ChatSession",
    "ChatSessionError",
    "ChatTurn",
    "format_transcript",
    "load_transcript",
]


CHAT_SYSTEM_PROMPT = """\
You are a chip-design intake assistant. The user describes a digital
hardware module in natural language; your job is to elicit a complete,
unambiguous specification by asking SHORT clarifying questions about:

* clock domain (number of clocks, target period, async crossings),
* reset polarity + timing (sync vs async, active-high vs active-low),
* every port (name, direction, width in bits, brief purpose),
* any timing / area / utilisation constraints stated by the user.

Keep each reply under 4 sentences. Ask one or two questions at a time;
do not dump a final specification. When the user types ``/run`` the
session will materialise a typed Spec from the transcript — until then,
your job is to elicit constraints, not to declare the spec complete.
""".strip()


_BANNER = (
    "chip-agent chat — describe your module; ask /help for commands."
)
_HELP_TEXT = (
    "/run    materialise a Spec from the transcript and exit.\n"
    "/show   print the current transcript.\n"
    "/exit   exit without materialising a Spec.\n"
    "/help   show this help.\n"
)
_USER_PREFIX = "You: "
_ASSISTANT_PREFIX = "Assistant: "


class ChatSessionError(ValueError):
    """The chat session was driven into an invalid state."""


@dataclass(frozen=True)
class ChatTurn:
    """One turn of the dialog. ``role`` is ``"user"`` or ``"assistant"``."""

    role: str
    text: str


@dataclass
class ChatSession:
    """Stateful chat REPL that streams clarifying questions and mints a Spec.

    Construction:

    * ``router`` — the :class:`ModelRouter` (its ``stream`` path is used for
      per-turn clarifying questions; its ``generate`` path is used by the
      embedded :class:`SpecIntakeAgent` for spec materialisation on ``/run``).
    * ``store`` — the artifact store the materialised Spec is written into.
    * ``design_id``, ``name`` — both flow into the Spec's identity + provenance.
    * ``transcript_path`` — markdown file the transcript is mirrored to; loaded
      on construction if it already exists (resume).
    * ``defaults`` — passed straight through to :class:`SpecIntakeAgent` so the
      materialised Spec's :class:`DesignConstraints` fall back to project
      defaults when the user never named e.g. a target utilisation.
    * ``stdin`` / ``stdout`` — injected for tests; default to the process
      handles.
    """

    router: ModelRouter
    store: SqliteArtifactStore
    design_id: str
    name: str
    transcript_path: Path
    defaults: ConstraintDefaults | None = None
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    turns: list[ChatTurn] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.design_id:
            raise ValueError("design_id must be non-empty")
        if self.transcript_path.exists():
            self.turns = load_transcript(self.transcript_path)

    # ----------------------------------------------------------------- run
    def run(self) -> Spec | None:
        """Drive the REPL until ``/run`` mints a Spec or ``/exit``/EOF ends it."""
        self._println(_BANNER)
        self._println(f"design_id: {self.design_id}")
        if self.turns:
            self._println("(resuming prior transcript)")
            for t in self.turns:
                self._println(_render_turn_inline(t))

        while True:
            self._write(_USER_PREFIX)
            self._flush()
            line = self._readline()
            if line is None:
                self._println("")  # newline after EOF for clean stdout
                return None
            text = line.rstrip("\n")
            if not text.strip():
                continue

            if text.startswith("/"):
                outcome = self._handle_slash(text)
                if isinstance(outcome, Spec):
                    return outcome
                if outcome is False:  # /exit
                    return None
                continue

            self.turns.append(ChatTurn(role="user", text=text))
            reply = self._stream_assistant_reply()
            self.turns.append(ChatTurn(role="assistant", text=reply))
            self._persist_transcript()

    # --------------------------------------------------------- slash-cmds
    def _handle_slash(self, line: str) -> Spec | bool:
        cmd = line.strip().split()[0].lower()
        if cmd == "/exit":
            return False
        if cmd == "/help":
            self._write(_HELP_TEXT)
            self._flush()
            return True
        if cmd == "/show":
            self._show_transcript()
            return True
        if cmd == "/run":
            spec = self._materialise_spec()
            # User EOFed mid-clarification — end the REPL cleanly
            # without a Spec.
            return spec if spec is not None else False
        self._println(f"unknown command: {cmd} (try /help)")
        return True

    # --------------------------------------------------------- streaming
    def _stream_assistant_reply(self) -> str:
        prompt = self._assemble_chat_prompt()
        self._write(_ASSISTANT_PREFIX)
        self._flush()
        accumulated: list[str] = []
        for chunk in self.router.stream(
            TaskType.SPEC_INTAKE,
            context={"prompt": prompt, "system": CHAT_SYSTEM_PROMPT},
        ):
            if chunk.delta:
                self._write(chunk.delta)
                self._flush()
                accumulated.append(chunk.delta)
        self._println("")
        return "".join(accumulated).strip()

    def _assemble_chat_prompt(self) -> str:
        """Flatten the full transcript into a single user-prompt string."""
        lines: list[str] = []
        for t in self.turns:
            label = "User" if t.role == "user" else "Assistant"
            lines.append(f"{label}: {t.text.strip()}")
        return "\n".join(lines)

    # --------------------------------------------------------- /run -> Spec
    def _materialise_spec(self) -> Spec | None:
        """Drive the F11.4 clarifying-question loop until a Spec is minted.

        Each pass: assemble the transcript, ask the agent. If the agent
        returns a :class:`ClarifyingQuestion`, surface the question to the
        user as an assistant turn, collect the next stdin line as the
        user's answer, append both to the transcript, and retry. The
        agent's own clarifying budget caps the loop; if the user EOFs
        mid-clarification the function returns ``None`` so the REPL ends
        cleanly without a Spec.
        """
        user_turns = [t for t in self.turns if t.role == "user"]
        if not user_turns:
            self._println(
                "nothing to materialise yet — describe the module first.",
            )
            raise ChatSessionError("/run called before any user turn")
        agent = SpecIntakeAgent(
            router=self.router,
            design_id=self.design_id,
            defaults=self.defaults,
        )
        while True:
            raw_text = _assemble_raw_text(self.turns)
            outcome = agent.intake(raw_text)
            if isinstance(outcome, Spec):
                self.store.put(outcome)
                self._persist_transcript()
                self._println(
                    f"spec materialised: {outcome.artifact_id} "
                    f"(content hash {outcome.content_hash[:12]}...)",
                )
                return outcome
            # ClarifyingQuestion: surface, append, prompt for the user's answer.
            assert isinstance(outcome, ClarifyingQuestion)
            self.turns.append(
                ChatTurn(role="assistant", text=outcome.question),
            )
            self._persist_transcript()
            self._println(f"{_ASSISTANT_PREFIX}{outcome.question}")
            self._write(_USER_PREFIX)
            self._flush()
            line = self._readline()
            if line is None:
                self._println("")
                return None
            answer = line.rstrip("\n")
            if not answer.strip():
                continue
            self.turns.append(ChatTurn(role="user", text=answer))
            self._persist_transcript()

    # --------------------------------------------------------- transcript
    def _persist_transcript(self) -> None:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript_path.write_text(
            format_transcript(self.design_id, self.name, self.turns),
            encoding="utf-8",
        )

    def _show_transcript(self) -> None:
        if not self.turns:
            self._println("(transcript is empty)")
            return
        self._println(
            format_transcript(self.design_id, self.name, self.turns).rstrip("\n"),
        )

    # --------------------------------------------------------- I/O helpers
    def _readline(self) -> str | None:
        line = self.stdin.readline()
        if line == "":
            return None
        return line

    def _write(self, s: str) -> None:
        self.stdout.write(s)

    def _println(self, s: str) -> None:
        self.stdout.write(s)
        self.stdout.write("\n")
        self.stdout.flush()

    def _flush(self) -> None:
        self.stdout.flush()


# --------------------------------------------------------------------------- #
# Module-level transcript serialisation helpers.
# --------------------------------------------------------------------------- #
_TRANSCRIPT_HEADER_TEMPLATE = (
    "# chip-agent chat transcript\n"
    "design_id: {design_id}\n"
    "name: {name}\n"
    "\n"
)


def format_transcript(
    design_id: str, name: str, turns: list[ChatTurn],
) -> str:
    """Serialise ``turns`` to the on-disk markdown shape ``ChatSession`` uses."""
    out = _TRANSCRIPT_HEADER_TEMPLATE.format(design_id=design_id, name=name)
    for t in turns:
        out += _render_turn_block(t)
    return out


def load_transcript(path: Path) -> list[ChatTurn]:
    """Parse a transcript markdown file back into a list of :class:`ChatTurn`s.

    Tolerant: skips header / blank lines, recognises ``**You:**`` and
    ``**Assistant:**`` prefixes (possibly spanning multiple lines).
    """
    text = path.read_text(encoding="utf-8")
    turns: list[ChatTurn] = []
    current_role: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        if current_role is not None and current_lines:
            turns.append(
                ChatTurn(role=current_role, text="\n".join(current_lines).strip()),
            )

    for raw in text.splitlines():
        if raw.startswith("**You:**"):
            flush()
            current_role = "user"
            current_lines = [raw[len("**You:**"):].strip()]
        elif raw.startswith("**Assistant:**"):
            flush()
            current_role = "assistant"
            current_lines = [raw[len("**Assistant:**"):].strip()]
        elif current_role is not None:
            current_lines.append(raw)
    flush()
    # Strip trailing blank lines that came from inter-turn blank rows.
    return [
        ChatTurn(role=t.role, text=t.text.strip())
        for t in turns
        if t.text.strip()
    ]


# --------------------------------------------------------------------------- #
# Private rendering helpers.
# --------------------------------------------------------------------------- #
def _render_turn_block(turn: ChatTurn) -> str:
    label = "**You:**" if turn.role == "user" else "**Assistant:**"
    return f"{label} {turn.text.strip()}\n\n"


def _render_turn_inline(turn: ChatTurn) -> str:
    label = "You" if turn.role == "user" else "Assistant"
    return f"{label}: {turn.text.strip()}"


def _assemble_raw_text(turns: list[ChatTurn]) -> str:
    """Build the ``raw_text`` payload SpecIntakeAgent.intake will see.

    Includes both user descriptions and the assistant's clarifying questions
    + their answers (the user turns), so the materialised Spec reflects
    every constraint the dialog elicited.
    """
    return "\n\n".join(
        f"{'User' if t.role == 'user' else 'Assistant'}: {t.text.strip()}"
        for t in turns
    )
