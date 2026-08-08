"""Pipeline progress pane (F14.2).

Renders the seven macro stages of the spine — SPEC → PLAN → RTL →
SYNTH → PHYSICAL → SIGNOFF → GDSII — as a horizontal strip of cells
whose colors reflect the current ``StageStatus`` of each.

Color coding (locked in the M14 plan):

* gray  — ``PENDING`` (stage not yet entered, or no checkpoint yet)
* yellow — ``IN_PROGRESS`` / ``ESCALATED`` (running or repair-looping)
* green — ``PASSED`` (gate closed)
* red — ``FAILED`` / ``BLOCKED`` (gate refused)
* blue — ``AWAITING_HUMAN`` (only valid at SIGNOFF — the F5.3 interrupt)

The pane polls the LangGraph checkpoint every 500 ms via
:meth:`Widget.set_interval` on the main thread (SQLite stays
single-threaded). When the checkpoint snapshot diverges from the last
seen :class:`DesignState`, the pane posts a :class:`StageAdvanced`
message; its own handler then updates the cell classes.

F14.2 mounts the pane unconditionally. The chat pane still exits the
app on ``/run`` (F14.1 contract); F14.3 will change ``/run`` to keep
the app open so the pipeline pane's live updates become useful in
the same session.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from chip_agent.design_state import (
    DesignState,
    DesignStatus,
    EscalationLevel,
    Stage,
    StageStatus,
)
from chip_agent.tui.messages import StageAdvanced
from chip_agent.tui.workers.poll_worker import (
    open_checkpoint_saver,
    read_design_state,
)

__all__ = ["PipelinePane"]


_IDLE_STATUS = (
    "Idle — describe your module in the chat above, then send /run."
)


# Display order — matches the spine's stage progression.
# F22.1: M19 stages (CONTRACT/ORACLE/ASSERTIONS/ORACLE_VERIFICATION) inserted
# between PLAN and RTL so the operator sees the test-first workflow light
# up as it runs. Each is a per-module stage (lives on
# ``design.modules[*].stages``) — :func:`_class_for_stage` aggregates the
# same worst-status-wins way RTL does so the cell colour surfaces the
# attention-worthy state across modules.
_STAGES: tuple[Stage, ...] = (
    Stage.SPEC, Stage.PLAN,
    Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS, Stage.ORACLE_VERIFICATION,
    Stage.RTL,
    Stage.SYNTH, Stage.PHYSICAL, Stage.SIGNOFF, Stage.GDSII,
)

# F22.1: the set of stages that live on ``ModuleState.stages`` (per-module)
# rather than ``DesignState.stages`` (design-level). The pipeline pane
# aggregates these worst-status-wins across modules so a single failing
# module surfaces the attention-worthy colour. Order doesn't matter; this
# is a set-membership check.
_PER_MODULE_STAGES: frozenset[Stage] = frozenset({
    Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS, Stage.ORACLE_VERIFICATION,
    Stage.RTL,
})


# F22.1.1: short, fixed-width labels so all 11 cells stay readable when the
# left column divides ``width: 1fr`` cells eleven ways. The pre-M19 strip
# rendered ``stage.value.upper()`` which overflowed for ASSERTIONS (10) and
# ORACLE_VERIFICATION (19) on standard terminal widths — green boxes with
# no visible text. These codes fit in ≤6 chars; the full stage name lives
# on each cell's ``tooltip`` and in the active-stage status line.
_STAGE_LABEL: dict[Stage, str] = {
    Stage.SPEC: "SPEC",
    Stage.PLAN: "PLAN",
    Stage.CONTRACT: "CONTRC",
    Stage.ORACLE: "ORACLE",
    Stage.ASSERTIONS: "ASSRT",
    Stage.ORACLE_VERIFICATION: "ORC_VR",
    Stage.RTL: "RTL",
    Stage.SYNTH: "SYNTH",
    Stage.PHYSICAL: "PHYS",
    Stage.SIGNOFF: "SIGN",
    Stage.GDSII: "GDSII",
}

# CSS class per status. The pane's stylesheet defines `.status-passed`
# (green), `.status-running` (yellow), `.status-failed` (red), and
# `.status-human` (blue); `.status-pending` is the default gray.
_STATUS_CLASS: dict[StageStatus, str] = {
    StageStatus.PENDING: "status-pending",
    StageStatus.IN_PROGRESS: "status-running",
    StageStatus.ESCALATED: "status-running",
    StageStatus.PASSED: "status-passed",
    StageStatus.FAILED: "status-failed",
    StageStatus.BLOCKED: "status-failed",
}


class PipelinePane(Vertical):
    """Stage strip + status line, polled from the checkpoint.

    Layout (top to bottom):

    * ``#stage-strip`` — horizontal row of seven stage cells (colour =
      :class:`StageStatus`).
    * ``#status-line`` — single-line text description of what's currently
      active: which stage, which module, attempt/budget, escalation level.
      Colour-coded for terminal states (green = COMPLETED, red = FAILED,
      yellow = AWAITING_HUMAN).
    """

    DEFAULT_CSS = """
    PipelinePane {
        height: 6;
        border: solid $primary;
        padding: 0 1;
    }

    PipelinePane > #stage-strip {
        height: 3;
        layout: horizontal;
    }

    PipelinePane > #stage-strip > Static {
        width: 1fr;
        content-align: center middle;
        border: solid $panel;
        background: $surface;
        color: $text;
    }

    PipelinePane > #stage-strip > Static.status-pending {
        background: $surface;
        color: $text-muted;
    }

    PipelinePane > #stage-strip > Static.status-running {
        background: $warning;
        color: $text;
    }

    PipelinePane > #stage-strip > Static.status-passed {
        background: $success;
        color: $text;
    }

    PipelinePane > #stage-strip > Static.status-failed {
        background: $error;
        color: $text;
    }

    PipelinePane > #stage-strip > Static.status-human {
        background: $accent;
        color: $text;
    }

    PipelinePane > #status-line {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    PipelinePane > #status-line.status-line-running {
        color: $warning;
    }

    PipelinePane > #status-line.status-line-passed {
        color: $success;
    }

    PipelinePane > #status-line.status-line-failed {
        color: $error;
    }

    PipelinePane > #status-line.status-line-human {
        color: $accent;
        text-style: bold;
    }
    """

    DEFAULT_POLL_MS: ClassVar[int] = 500

    def __init__(
        self,
        *,
        design_id: str,
        checkpoint_path: Path,
        poll_ms: int = DEFAULT_POLL_MS,
    ) -> None:
        super().__init__()
        self.design_id = design_id
        self.checkpoint_path = checkpoint_path
        self.poll_ms = poll_ms
        self._last_state: DesignState | None = None
        # Saver is opened on mount (when the checkpoint file may not yet
        # exist) and re-opened lazily inside the poll callback if the
        # file appears later. Held across ticks so we don't churn
        # SQLite connections at 2 Hz.
        self._saver_ctx: object | None = None
        self._saver: object | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="stage-strip"):
            for stage in _STAGES:
                cell = Static(_STAGE_LABEL[stage], id=f"stage-{stage.value}")
                cell.add_class("status-pending")
                # Hover tooltip carries the full stage name for users on
                # mouse-capable terminals. Keyboard-only users get the same
                # information from the active-stage status line below.
                cell.tooltip = stage.value.upper().replace("_", " ")
                yield cell
        yield Static(_IDLE_STATUS, id="status-line")

    def on_mount(self) -> None:
        # set_interval runs the callback on the main thread; we open the
        # saver lazily inside the callback because the checkpoint file
        # may not exist yet when the pane mounts (the spine hasn't been
        # driven; F14.3 will wire the [R]un keybind that creates it).
        self.set_interval(self.poll_ms / 1000, self._poll_once)

    def _poll_once(self) -> None:
        """One polling tick — read the checkpoint and diff against last seen."""
        with open_checkpoint_saver(self.checkpoint_path) as saver:
            state = read_design_state(saver, self.design_id)
        if state is None:
            return
        if self._last_state is not None and _state_equiv(self._last_state, state):
            return
        self._last_state = state
        self.post_message(StageAdvanced(design=state))

    # ----------------------------------------------------------- message handlers
    def on_stage_advanced(self, message: StageAdvanced) -> None:
        """Update each cell's color class based on the new DesignState."""
        self.apply_state(message.design)

    # ----------------------------------------------------------- public API
    def apply_state(self, design: DesignState) -> None:
        """Update the cell colors + status line from a :class:`DesignState`.

        Public so tests can drive the pane directly without going
        through the polling worker.
        """
        for stage in _STAGES:
            cell = self.query_one(f"#stage-{stage.value}", Static)
            cls = _class_for_stage(stage, design)
            # Remove any prior status class + add the new one.
            for c in (
                "status-pending", "status-running",
                "status-passed", "status-failed", "status-human",
            ):
                cell.remove_class(c)
            cell.add_class(cls)
        self._update_status_line(design)

    def _update_status_line(self, design: DesignState | None) -> None:
        """Refresh the bottom text + its tone class for the active stage."""
        line = self.query_one("#status-line", Static)
        text, tone = _format_status_line(design)
        line.update(text)
        for c in (
            "status-line-running", "status-line-passed",
            "status-line-failed", "status-line-human",
        ):
            line.remove_class(c)
        if tone:
            line.add_class(f"status-line-{tone}")


# --------------------------------------------------------------------------- #
# Helpers — pure functions, easy to unit-test.
# --------------------------------------------------------------------------- #
def _class_for_stage(stage: Stage, design: DesignState) -> str:
    """Map a (stage, DesignState) pair to the CSS status class."""
    # SPEC + PLAN are "implicit" stages — their status is derived from
    # whether the design carries the head ref.
    if stage is Stage.SPEC:
        return "status-passed" if design.spec is not None else "status-pending"
    if stage is Stage.PLAN:
        return "status-passed" if design.plan is not None else "status-pending"

    # AWAITING_HUMAN can fire at any stage when an escalation walks up
    # to EscalationLevel.HUMAN (e.g. RTL repair budget exhausted +
    # F12.5 frontier-fallback also failed). The blue cell follows
    # ``design.current_stage`` so the operator sees WHERE the spine
    # got stuck, not where the normal end-of-flow gate would have been.
    if (
        design.status is DesignStatus.AWAITING_HUMAN
        and stage is design.current_stage
    ):
        return "status-human"

    if stage in _PER_MODULE_STAGES:
        # F22.1: per-module stage — aggregate across modules, worst status
        # wins. Same pattern RTL used pre-F22.1; extended to cover the four
        # M19 stages now that they're in the strip. A module that took the
        # F19.13 fast path won't carry these stages on its ``ModuleState``
        # so it contributes nothing to the aggregate, leaving the cell
        # PENDING when EVERY module fast-pathed — matches the operator's
        # expectation that a "skipped" M19 leg shouldn't go green.
        statuses = [
            ms.stages[stage].status
            for ms in design.modules.values()
            if stage in ms.stages
        ]
        if not statuses:
            return "status-pending"
        return _STATUS_CLASS[_worst_status(statuses)]

    # Design-level stage (SYNTH / PHYSICAL / SIGNOFF / GDSII).
    ss = design.stages.get(stage)
    if ss is None:
        return "status-pending"
    return _STATUS_CLASS[ss.status]


# Ordering "worst-first" so aggregate per-module RTL status surfaces the
# attention-worthy state in the cell color.
_STATUS_ORDER: tuple[StageStatus, ...] = (
    StageStatus.FAILED,
    StageStatus.BLOCKED,
    StageStatus.ESCALATED,
    StageStatus.IN_PROGRESS,
    StageStatus.PENDING,
    StageStatus.PASSED,
)


def _worst_status(statuses: list[StageStatus]) -> StageStatus:
    """Return the highest-attention status in the list."""
    seen = set(statuses)
    for s in _STATUS_ORDER:
        if s in seen:
            return s
    return StageStatus.PENDING  # unreachable for non-empty input


def _state_equiv(a: DesignState, b: DesignState) -> bool:
    """Cheap equality check for diff-aware polling.

    The PipelinePane only cares about the bits that affect cell color
    — design.status, current_stage, and each stage's StageStatus. A
    full Pydantic equality check would catch unrelated changes
    (timestamps, etc.) and over-post.
    """
    if a.status is not b.status:
        return False
    if a.current_stage is not b.current_stage:
        return False
    if (a.spec is None) != (b.spec is None):
        return False
    if (a.plan is None) != (b.plan is None):
        return False
    if {k: v.status for k, v in a.stages.items()} != {
        k: v.status for k, v in b.stages.items()
    }:
        return False
    a_rtl = sorted(
        (ms.stages[Stage.RTL].status, ms.stages[Stage.RTL].attempts,
         ms.stages[Stage.RTL].escalation)
        for ms in a.modules.values()
        if Stage.RTL in ms.stages
    )
    b_rtl = sorted(
        (ms.stages[Stage.RTL].status, ms.stages[Stage.RTL].attempts,
         ms.stages[Stage.RTL].escalation)
        for ms in b.modules.values()
        if Stage.RTL in ms.stages
    )
    if a_rtl != b_rtl:
        return False
    # Design-level attempts/escalation also drive the status line text.
    a_dl = {k: (v.attempts, v.escalation) for k, v in a.stages.items()}
    b_dl = {k: (v.attempts, v.escalation) for k, v in b.stages.items()}
    return a_dl == b_dl


# --------------------------------------------------------------------------- #
# Status-line formatter — pure, easy to unit-test.
# --------------------------------------------------------------------------- #
# Tone used by the CSS class. None = default muted text.
_StatusTone = str | None


def _format_status_line(design: DesignState | None) -> tuple[str, _StatusTone]:
    """Render the bottom status line + tone class for the active stage.

    Returns ``(text, tone)`` where ``tone`` is one of
    ``"running" | "passed" | "failed" | "human" | None``.
    """
    if design is None or design.spec is None:
        return _IDLE_STATUS, None

    if design.status is DesignStatus.COMPLETED:
        return "Pipeline COMPLETED — GDSII emitted.", "passed"

    if design.status is DesignStatus.FAILED:
        stage_name = design.current_stage.value.upper()
        return f"Pipeline FAILED at {stage_name}.", "failed"

    if design.status is DesignStatus.AWAITING_HUMAN:
        stage_name = design.current_stage.value.upper()
        if design.current_stage is Stage.SIGNOFF:
            return (
                f"Awaiting human approval at {stage_name} — "
                "press Ctrl+A to approve.",
                "human",
            )
        # Earlier-stage escalation = repair budget burned through.
        # cmd_resume can't recover; the user has to restart with a
        # revised spec. Phrase it bluntly so the operator doesn't
        # press Ctrl+A and trip the F14.3 refusal notification.
        return (
            f"Stuck at {stage_name} — repair budget exhausted. "
            "Restart with a revised spec.",
            "failed",
        )

    # RUNNING / PLANNING — describe the active stage in detail.
    stage = design.current_stage
    stage_name = stage.value.upper()
    if stage is Stage.SPEC:
        return f"Active: {stage_name} — intaking.", "running"
    if stage is Stage.PLAN:
        return f"Active: {stage_name} — planning module decomposition.", "running"

    # Per-module RTL: find the first non-PASSED module to surface.
    if stage is Stage.RTL:
        for module in design.modules.values():
            ss = module.stages.get(Stage.RTL)
            if ss is None or ss.status is StageStatus.PASSED:
                continue
            return (
                f"Active: RTL {module.name} — attempt {ss.attempts}/"
                f"{ss.max_attempts}, {_describe_escalation(ss.escalation)}.",
                "running",
            )
        return f"Active: {stage_name}.", "running"

    # Design-level stage (SYNTH / PHYSICAL / SIGNOFF / GDSII).
    ss = design.stages.get(stage)
    if ss is None:
        return f"Active: {stage_name}.", "running"
    return (
        f"Active: {stage_name} — attempt {ss.attempts}/{ss.max_attempts}, "
        f"{_describe_escalation(ss.escalation)}.",
        "running",
    )


def _describe_escalation(level: EscalationLevel) -> str:
    """Human-readable phrase for the EscalationLevel rung."""
    if level is EscalationLevel.INNER:
        return "inner repair"
    if level is EscalationLevel.OUTER:
        return "outer (semantic) repair"
    if level is EscalationLevel.EXHAUSTED:
        return "budget exhausted — frontier fallback"
    if level is EscalationLevel.HUMAN:
        return "awaiting human"
    return level.value  # forward-compat for new rungs
