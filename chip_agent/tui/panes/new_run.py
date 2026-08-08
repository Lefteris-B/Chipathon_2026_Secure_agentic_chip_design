"""New-run confirmation modal (Ctrl+N).

Pushed when the operator hits Ctrl+N to start a fresh design without
quitting + relaunching the app. Returns ``True`` on Confirm + ``False``
on Cancel; ``ChipAgentApp`` resets its in-memory state, mints a new
design_id, and remounts the four panes when ``True`` lands.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

__all__ = ["NewRunConfirmScreen"]


_BODY = """\
Start a new run?

This keeps the current run-dir + audit log + store, but mints a fresh
design_id so a new spec → run → resume chain starts from scratch.
Previous run state stays on disk for inspection.

(The chat conversation will reset.)
"""


class NewRunConfirmScreen(ModalScreen[bool]):
    """Confirm/cancel modal — dismisses with ``True`` to start fresh."""

    BINDINGS = [  # noqa: RUF012  (mypy expects list per Screen base class)
        Binding("escape", "cancel", "Cancel", show=True, priority=True),
        Binding("enter", "confirm", "Confirm", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    NewRunConfirmScreen {
        align: center middle;
        background: $background 70%;
    }

    NewRunConfirmScreen > Vertical {
        width: 60%;
        height: auto;
        max-height: 80%;
        background: $panel;
        border: thick $primary;
        padding: 1 2;
    }

    NewRunConfirmScreen Static#new-run-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
    }

    NewRunConfirmScreen Static#new-run-body {
        height: auto;
        padding: 1 1;
    }

    NewRunConfirmScreen Horizontal {
        height: 3;
        align: center middle;
    }

    NewRunConfirmScreen Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("New run", id="new-run-title")
            yield Static(_BODY, id="new-run-body")
            with Horizontal():
                yield Button(
                    "Start new run", id="confirm-button", variant="primary",
                )
                yield Button("Cancel", id="cancel-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-button":
            self.action_confirm()
        elif event.button.id == "cancel-button":
            self.action_cancel()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
