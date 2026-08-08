"""Help screen listing all chip-agent keybindings (F14.6).

Pushed by ``[Ctrl+H]``. Static modal — the binding table is hard-coded
here rather than introspected from :attr:`ChipAgentApp.BINDINGS` to
keep the description text accurate (Textual's ``Binding.description``
is shown in the footer; the help screen has more room for context).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

__all__ = ["HelpScreen"]


_HELP_BODY = """\
chip-agent keybindings

  Ctrl+R   Run pipeline (cmd_run from the chat-minted Spec)
  Ctrl+A   Approve + resume past the human gate (cmd_resume); when RTL
           repair is stuck, (re)open the guidance modal instead
  Ctrl+S   Open the signoff dashboard
  Ctrl+H   Show this help screen
  Ctrl+N   Start a new run (mints fresh design_id; confirms first)
  Ctrl+L   Open the model picker (per-task LLM bindings)
  Ctrl+Q   Quit (also Ctrl+C)
  Enter    Send the chat input as a turn
  /run     (in chat) materialise the Spec from the conversation
  /exit    (in chat) end the session without minting a Spec
  /help    (in chat) show available chat slash commands
  Esc      Close the current modal

Pipeline pane (left, bottom): live stage strip + status line.
Audit pane (right, bottom): chain-hashed event timeline + validity badge.
Exports pane (right, top): directory tree + syntax-highlighted preview.
"""


class HelpScreen(ModalScreen[None]):
    """Static keybinding reference shown over the main app."""

    BINDINGS = [  # noqa: RUF012  (mypy expects list per Screen base class)
        Binding("escape", "close", "Close", show=True, priority=True),
        Binding("ctrl+h", "close", "Close", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
        background: $background 70%;
    }

    HelpScreen > Vertical {
        width: 70%;
        height: 80%;
        background: $panel;
        border: thick $primary;
        padding: 1 2;
    }

    HelpScreen Static#help-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
    }

    HelpScreen Static#help-body {
        height: 1fr;
        padding: 1 1;
    }

    HelpScreen Static#help-footer {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Help", id="help-title")
            yield Static(_HELP_BODY, id="help-body")
            yield Static("Press Esc to close.", id="help-footer")

    def action_close(self) -> None:
        self.dismiss()
