"""Model picker modal (F15.1 view + F15.2 tasks + F15.4 loops + F15.5 knobs).

Pushed by Ctrl+L. Renders every ``routing.tasks.*`` + ``routing.loops.*``
binding for the active session as a row with five parts: task/loop
label, a :class:`Select[str]` of registry keys, an :class:`Input` for
temperature (validated to ``[0.0, 2.0]``), an Input for ``n``
(validated to ``>= 1``), and a Static suffix carrying the resolved
``provider:model``. The ``[Apply]`` button computes the per-binding
diff against the values the modal was constructed with, validates the
parsed knob fields, and dismisses with::

    {
        "tasks": {task_name: {"model"?: str, "temperature"?: float,
                              "n"?: int}, ...},
        "loops": {loop_name: {...}, ...},
    }

Each inner dict carries only the binding fields that changed. The app
uses this to mutate the live :class:`RoutingSettings` in place. An
invalid Input (out-of-range, NaN, blank) blocks Apply with an error
notification — the modal stays open so the operator can fix the value.
``[Close]`` / Esc dismisses with ``None`` (cancellation).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.validation import Integer, Number
from textual.widgets import Button, Input, Select, Static

from chip_agent.settings import LoopBinding, RoutingSettings, TaskBinding

__all__ = [
    "ModelPickerScreen",
    "PartialBinding",
    "RoutingDiff",
    "format_binding_row",
    "format_binding_suffix",
]


# Loop slots are validated at parse time to be a subset of {"inner", "outer"}.
# Render in that fixed order so the modal layout is stable across configs.
_LOOP_DISPLAY_ORDER: tuple[str, ...] = ("inner", "outer")


PartialBinding = dict[str, str | float | int]
"""Per-binding diff: a subset of {"model": str, "temperature": float,
"n": int}. Only changed fields appear."""

RoutingDiff = dict[str, dict[str, PartialBinding]]
"""Dismiss payload: ``{"tasks": {name: PartialBinding}, "loops":
{name: PartialBinding}}``.

Each inner dict carries only the bindings whose model / temperature /
n changed; each binding's PartialBinding carries only the fields that
changed. Both subdicts empty (or whole dict missing) means "Apply with
no changes".
"""


def _format_temperature(value: float) -> str:
    """Render a temperature value the same way every time so the diff
    snapshot + the live Input value compare cleanly."""
    return f"{value:.2f}"


def _format_n(value: int) -> str:
    return str(value)


class ModelPickerScreen(ModalScreen[RoutingDiff | None]):
    """Editable picker for ``routing.tasks.*`` + ``routing.loops.*`` bindings.

    Dismiss value semantics:

    * ``None`` — user cancelled (Esc / Close button). The app does not
      mutate routing.
    * ``{"tasks": {}, "loops": {}}`` (or omitted blocks) — Apply was
      clicked with no changes. The app dismisses without mutating.
    * Non-empty ``{tasks, loops}`` dict — see :data:`RoutingDiff` for
      the per-binding partial-dict shape. The app calls
      ``apply_routing_change(tasks=..., loops=...)`` with the dicts;
      each entry sets the named binding fields on the live routing.
    """

    BINDINGS = [  # noqa: RUF012  (mypy expects list per Screen base class)
        Binding("escape", "close", "Close", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    ModelPickerScreen {
        align: center middle;
        background: $background 70%;
    }

    ModelPickerScreen > Vertical {
        width: 90%;
        height: 80%;
        background: $panel;
        border: thick $primary;
        padding: 1 2;
    }

    ModelPickerScreen Static#picker-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
    }

    ModelPickerScreen VerticalScroll#picker-body {
        height: 1fr;
        padding: 1 1;
    }

    ModelPickerScreen Static.picker-section-header {
        height: 1;
        text-style: bold;
        color: $accent;
        padding-top: 1;
    }

    ModelPickerScreen Horizontal.picker-row {
        height: 3;
        margin-bottom: 1;
    }

    ModelPickerScreen Static.picker-label {
        width: 14;
        content-align: left middle;
        height: 3;
    }

    ModelPickerScreen Select {
        width: 22;
    }

    ModelPickerScreen Static.picker-knob-label {
        width: 3;
        content-align: left middle;
        height: 3;
        padding-left: 1;
    }

    ModelPickerScreen Input.picker-knob {
        height: 3;
    }

    ModelPickerScreen Input.picker-knob-temp {
        width: 8;
    }

    ModelPickerScreen Input.picker-knob-n {
        width: 6;
    }

    ModelPickerScreen Static.picker-suffix {
        width: 1fr;
        content-align: left middle;
        padding-left: 1;
        color: $text-muted;
        height: 3;
    }

    ModelPickerScreen Horizontal#picker-buttons {
        height: 3;
        align: center middle;
    }

    ModelPickerScreen Button {
        margin: 0 1;
    }

    ModelPickerScreen Static#picker-footer {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(self, *, routing: RoutingSettings) -> None:
        super().__init__()
        self._routing = routing
        # Snapshot each binding's starting state so Apply can compute a
        # clean diff. We snapshot the **string** representations because
        # the Inputs' .value is always a string; comparing
        # "0.20" == "0.20" is cheaper than re-parsing the floats.
        self._initial_tasks: dict[str, tuple[str, str, str]] = {
            name: (
                b.model,
                _format_temperature(b.temperature),
                _format_n(b.n),
            )
            for name, b in routing.tasks.items()
        }
        self._initial_loops: dict[str, tuple[str, str, str]] = {
            name: (
                b.model,
                _format_temperature(b.temperature),
                _format_n(b.n),
            )
            for name, b in routing.loops.items()
        }

    # ---------------------------------------------------------------- compose
    def compose(self) -> ComposeResult:
        registry_options = [
            (key, key) for key in sorted(self._routing.registry)
        ]
        with Vertical():
            yield Static("Model picker", id="picker-title")
            with VerticalScroll(id="picker-body"):
                if self._routing.tasks:
                    yield Static("Tasks:", classes="picker-section-header")
                    for task_name, task_binding in sorted(
                        self._routing.tasks.items(),
                    ):
                        yield from self._row(
                            kind="task",
                            name=task_name,
                            binding=task_binding,
                            options=registry_options,
                        )
                if self._routing.loops:
                    yield Static("Loops:", classes="picker-section-header")
                    for loop_name in _LOOP_DISPLAY_ORDER:
                        loop_binding = self._routing.loops.get(loop_name)
                        if loop_binding is None:
                            continue
                        yield from self._row(
                            kind="loop",
                            name=loop_name,
                            binding=loop_binding,
                            options=registry_options,
                        )
                if not self._routing.tasks and not self._routing.loops:
                    yield Static(
                        "(no routing bindings — config has no tasks/loops)",
                        id="picker-empty",
                    )
            with Horizontal(id="picker-buttons"):
                yield Button(
                    "Apply", id="apply-button", variant="primary",
                )
                yield Button("Close", id="close-button")
            yield Static("Press Esc to close.", id="picker-footer")

    def _row(
        self,
        *,
        kind: str,  # "task" | "loop"
        name: str,
        binding: TaskBinding | LoopBinding,
        options: list[tuple[str, str]],
    ) -> ComposeResult:
        """Yield one editable binding row: label, model Select, knob
        Inputs, resolved-info suffix."""
        with Horizontal(classes="picker-row", id=f"{kind}-{name}"):
            yield Static(name, classes="picker-label")
            yield Select[str](
                options=options,
                value=binding.model,
                allow_blank=False,
                id=f"{kind}-select-{name}",
            )
            yield Static("T=", classes="picker-knob-label")
            yield Input(
                value=_format_temperature(binding.temperature),
                validators=[Number(minimum=0.0, maximum=2.0)],
                classes="picker-knob picker-knob-temp",
                id=f"{kind}-temp-{name}",
            )
            yield Static("n=", classes="picker-knob-label")
            yield Input(
                value=_format_n(binding.n),
                validators=[Integer(minimum=1)],
                classes="picker-knob picker-knob-n",
                id=f"{kind}-n-{name}",
            )
            yield Static(
                format_binding_suffix(binding, self._routing),
                classes="picker-suffix",
                id=f"{kind}-suffix-{name}",
            )

    # --------------------------------------------------------------- actions
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-button":
            self.action_close()
        elif event.button.id == "apply-button":
            self.action_apply()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        """Compute the per-binding diff + dismiss.

        If any edited Input is invalid (out of range, unparseable), the
        modal stays open with an error notification so the operator can
        fix the value.
        """
        try:
            task_diff = self._diff_block("task", self._initial_tasks)
            loop_diff = self._diff_block("loop", self._initial_loops)
        except ValueError as e:
            self.app.notify(f"Cannot apply: {e}", severity="error")
            return
        self.dismiss({"tasks": task_diff, "loops": loop_diff})

    def _diff_block(
        self,
        kind: str,  # "task" | "loop"
        initial: dict[str, tuple[str, str, str]],
    ) -> dict[str, PartialBinding]:
        """Walk each binding's widgets; collect changed fields per
        binding. Raises :class:`ValueError` on any invalid Input."""
        diff: dict[str, PartialBinding] = {}
        for name, (init_model, init_temp, init_n) in initial.items():
            partial: PartialBinding = {}
            # Model swap.
            select = self.query_one(f"#{kind}-select-{name}", Select)
            sel_value = select.value
            if isinstance(sel_value, str) and sel_value != init_model:
                partial["model"] = sel_value
            # Temperature edit.
            temp_input = self.query_one(f"#{kind}-temp-{name}", Input)
            if temp_input.value != init_temp:
                if not temp_input.is_valid:
                    raise ValueError(
                        f"{name}: temperature {temp_input.value!r} "
                        "outside [0.0, 2.0]",
                    )
                partial["temperature"] = float(temp_input.value)
            # n edit.
            n_input = self.query_one(f"#{kind}-n-{name}", Input)
            if n_input.value != init_n:
                if not n_input.is_valid:
                    raise ValueError(
                        f"{name}: n {n_input.value!r} must be >= 1",
                    )
                partial["n"] = int(n_input.value)
            if partial:
                diff[name] = partial
        return diff


# --------------------------------------------------------------------------- #
# Pure formatters — easy to unit-test.
# --------------------------------------------------------------------------- #
def format_binding_row(
    name: str,
    binding: TaskBinding | LoopBinding,
    routing: RoutingSettings,
) -> str:
    """One-line summary of a task/loop binding (no widget required).

    Shape::

        spec_intake    -> frontier        (anthropic:claude-sonnet-4-6)  T=0.20  n=1

    When the registry entry is missing (shouldn't happen post-validation
    but the formatter doesn't trust transient state), the row flags it
    explicitly so the operator sees the gap.
    """
    suffix = format_binding_suffix(binding, routing)
    return f"{name:<14s} -> {binding.model:<14s}  {suffix}"


def format_binding_suffix(
    binding: TaskBinding | LoopBinding,
    routing: RoutingSettings,
) -> str:
    """Right-hand-side of a binding row: resolved provider:model + knobs."""
    entry = routing.registry.get(binding.model)
    resolved = (
        "(missing from registry)"
        if entry is None
        else f"({entry.provider}:{entry.model})"
    )
    return f"{resolved:<48s}  T={binding.temperature:.2f}  n={binding.n}"
