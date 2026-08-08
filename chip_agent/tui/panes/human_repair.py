"""Interactive human-repair modal (F23.5 Option B, TUI surface).

Pushed by :class:`ChipAgentApp` when a run pauses with a
``pending_human_repair`` request (an RTL escalation, not the pre-GDSII
success gate). Shows the :class:`FailureDiagnosis` and captures the
operator's guidance in a ``TextArea``; dismisses with the entered text
(→ the app resumes with ``cmd_resume --hint``) or ``None`` (skip → the
run stays blocked).

The gate stays binding: this modal only collects a hint that seeds one
more bounded, gated repair attempt — it can never pass a stage.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from chip_agent.design_state import FailureDiagnosis

__all__ = ["HumanRepairScreen", "format_diagnosis"]

_HINT_INPUT_ID = "repair-hint-input"


def format_diagnosis(diagnosis: FailureDiagnosis | None) -> str:
    """Render the diagnosis for the operator (module-agnostic body text)."""
    if diagnosis is None:
        return "(no diagnosis captured — describe what you think is wrong.)"
    lines = [
        f"Summary:        {diagnosis.nl_summary.strip() or '<none>'}",
        f"Failing signal: {diagnosis.failing_signal or '<unknown>'}",
        f"Cycle:          {diagnosis.cycle if diagnosis.cycle is not None else '<unknown>'}",
        f"Expected:       {diagnosis.expected or '<unknown>'}",
        f"Actual:         {diagnosis.actual or '<unknown>'}",
    ]
    if diagnosis.suspected_cause:
        lines.append(f"Suspected:      {diagnosis.suspected_cause}")
    return "\n".join(lines)


class HumanRepairScreen(ModalScreen[str | None]):
    """Capture operator guidance for a stuck RTL repair.

    Dismisses with the entered text (stripped, non-empty) or ``None`` when
    the operator skips / cancels / submits blank.
    """

    BINDINGS = [  # noqa: RUF012  (Screen base expects a list)
        Binding("escape", "skip", "Skip", show=True, priority=True),
        Binding("ctrl+d", "send", "Send guidance", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    HumanRepairScreen {
        align: center middle;
        background: $background 70%;
    }
    HumanRepairScreen > Vertical {
        width: 70%;
        height: auto;
        max-height: 90%;
        background: $panel;
        border: thick $warning;
        padding: 1 2;
    }
    HumanRepairScreen Static#repair-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
    }
    HumanRepairScreen Static#repair-body {
        height: auto;
        padding: 1 1;
    }
    HumanRepairScreen TextArea {
        height: 8;
    }
    HumanRepairScreen Horizontal {
        height: 3;
        align: center middle;
    }
    HumanRepairScreen Button {
        margin: 0 1;
    }
    """

    def __init__(self, *, module_id: str, body: str) -> None:
        super().__init__()
        self._module_id = module_id
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"RTL repair stuck on {self._module_id!r} — your guidance",
                id="repair-title",
            )
            yield Static(self._body, id="repair-body")
            yield Static(
                "Describe the fix or what to try. It seeds one more bounded, "
                "gated attempt (Ctrl+D to send, Esc to skip):",
            )
            yield TextArea(id=_HINT_INPUT_ID)
            with Horizontal():
                yield Button("Send guidance", id="send-button", variant="primary")
                yield Button("Skip", id="skip-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-button":
            self.action_send()
        elif event.button.id == "skip-button":
            self.action_skip()

    def action_send(self) -> None:
        text = self.query_one(f"#{_HINT_INPUT_ID}", TextArea).text.strip()
        self.dismiss(text or None)

    def action_skip(self) -> None:
        self.dismiss(None)
