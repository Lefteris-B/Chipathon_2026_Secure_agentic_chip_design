"""F14.2 acceptance: pipeline progress pane + checkpoint poller.

Drives :class:`PipelinePane` directly via fixture :class:`DesignState`
snapshots (no real LangGraph spine needed) AND end-to-end against a
real checkpoint produced by ``cmd_run``. Pins:

* Seven stage cells in the canonical order SPEC → … → GDSII.
* Color class per ``StageStatus`` (gray / yellow / green / red / blue).
* AWAITING_HUMAN only colors the SIGNOFF cell, not arbitrary stages.
* Per-module RTL aggregates worst-status-wins across modules.
* ``read_design_state`` reconstructs a ``DesignState`` from a real
  ``cmd_run``-produced checkpoint.
* The poll callback posts ``StageAdvanced`` only when the snapshot
  differs from the last seen state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from chip_agent.cli import RunArgs, cmd_run
from chip_agent.design_state import (
    ArtifactRef,
    DesignConstraints,
    DesignState,
    DesignStatus,
    ModuleState,
    Stage,
    StageState,
    StageStatus,
)
from chip_agent.tui.panes.pipeline import (
    PipelinePane,
    _class_for_stage,
    _state_equiv,
)
from chip_agent.tui.workers.poll_worker import (
    open_checkpoint_saver,
    read_design_state,
)
from tests._routing_stub import StubBackend, make_routing_config, make_test_router

HMAC_KEY = b"f14.2-pipeline-test-hmac-key"


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _spec_ref(design_id: str = "d") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"{design_id}.spec", version=1,
        kind=__import__("chip_agent.design_state", fromlist=["ArtifactKind"]).ArtifactKind.SPEC,
        content_hash="sha256:00",
    )


def _plan_ref(design_id: str = "d") -> ArtifactRef:
    from chip_agent.design_state import ArtifactKind
    return ArtifactRef(
        artifact_id=f"{design_id}.plan", version=1,
        kind=ArtifactKind.PLAN, content_hash="sha256:00",
    )


def _state(
    *,
    status: DesignStatus = DesignStatus.PLANNING,
    current_stage: Stage = Stage.SPEC,
    has_spec: bool = False,
    has_plan: bool = False,
    rtl_status: StageStatus | None = None,
    synth_status: StageStatus | None = None,
    physical_status: StageStatus | None = None,
    signoff_status: StageStatus | None = None,
    gdsii_status: StageStatus | None = None,
) -> DesignState:
    """Build a synthetic :class:`DesignState` for pane rendering tests."""
    stages: dict[Stage, StageState] = {}
    for stage, st in [
        (Stage.SYNTH, synth_status),
        (Stage.PHYSICAL, physical_status),
        (Stage.SIGNOFF, signoff_status),
        (Stage.GDSII, gdsii_status),
    ]:
        if st is not None:
            stages[stage] = StageState(stage=stage, status=st)

    modules: dict[str, ModuleState] = {}
    if rtl_status is not None:
        modules["counter"] = ModuleState(
            module_id="counter", name="counter",
            stages={Stage.RTL: StageState(stage=Stage.RTL, status=rtl_status)},
        )

    return DesignState(
        design_id="d", name="design",
        constraints=DesignConstraints(),
        spec=_spec_ref() if has_spec else None,
        plan=_plan_ref() if has_plan else None,
        status=status,
        current_stage=current_stage,
        modules=modules,
        stages=stages,
    )


# --------------------------------------------------------------------------- #
# Pure-function tests: _class_for_stage maps (stage, state) -> CSS class.
# --------------------------------------------------------------------------- #
def test_class_for_spec_pending_when_no_spec_ref() -> None:
    assert _class_for_stage(Stage.SPEC, _state()) == "status-pending"


def test_class_for_spec_passed_when_spec_ref_set() -> None:
    assert _class_for_stage(
        Stage.SPEC, _state(has_spec=True),
    ) == "status-passed"


def test_class_for_plan_pending_when_no_plan_ref() -> None:
    assert _class_for_stage(Stage.PLAN, _state()) == "status-pending"


def test_class_for_plan_passed_when_plan_ref_set() -> None:
    assert _class_for_stage(
        Stage.PLAN, _state(has_plan=True),
    ) == "status-passed"


@pytest.mark.parametrize(
    "status,cls",
    [
        (StageStatus.PENDING, "status-pending"),
        (StageStatus.IN_PROGRESS, "status-running"),
        (StageStatus.ESCALATED, "status-running"),
        (StageStatus.PASSED, "status-passed"),
        (StageStatus.FAILED, "status-failed"),
        (StageStatus.BLOCKED, "status-failed"),
    ],
)
def test_class_for_design_level_stages_per_status(
    status: StageStatus, cls: str,
) -> None:
    assert _class_for_stage(
        Stage.SYNTH, _state(synth_status=status),
    ) == cls


def test_signoff_human_takes_precedence_over_status_at_signoff_pause() -> None:
    """The normal end-of-flow gate: paused at SIGNOFF with
    status=AWAITING_HUMAN, SIGNOFF cell turns blue."""
    state = _state(
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.SIGNOFF,
        signoff_status=StageStatus.PASSED,
    )
    assert _class_for_stage(Stage.SIGNOFF, state) == "status-human"


def test_awaiting_human_at_rtl_colors_the_rtl_cell_blue() -> None:
    """When RTL repair budget exhausts + F12.5 frontier-fallback also
    fails, the stage escalates to HUMAN at RTL (current_stage=RTL).
    The blue cell should follow current_stage so the operator sees
    where the spine actually got stuck."""
    state = _state(
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.RTL,
        has_spec=True, has_plan=True,
        rtl_status=StageStatus.BLOCKED,
    )
    assert _class_for_stage(Stage.RTL, state) == "status-human"
    # SIGNOFF stays pending (no gate fired there).
    assert _class_for_stage(Stage.SIGNOFF, state) == "status-pending"


def test_awaiting_human_does_not_color_already_passed_cells() -> None:
    """Stages that already passed stay green when AWAITING_HUMAN is
    elsewhere — only the current_stage cell turns blue."""
    state = _state(
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.SIGNOFF,
        synth_status=StageStatus.PASSED,
        signoff_status=StageStatus.PASSED,
    )
    assert _class_for_stage(Stage.SYNTH, state) == "status-passed"
    assert _class_for_stage(Stage.SIGNOFF, state) == "status-human"


def test_rtl_pending_when_no_modules_have_rtl_state() -> None:
    assert _class_for_stage(Stage.RTL, _state()) == "status-pending"


def test_rtl_aggregates_worst_status_wins_across_modules() -> None:
    """If one module's RTL is FAILED and another is PASSED, the cell is
    red — operator-attention-first."""
    state = DesignState(
        design_id="d", name="d", constraints=DesignConstraints(),
        modules={
            "a": ModuleState(
                module_id="a", name="a",
                stages={Stage.RTL: StageState(
                    stage=Stage.RTL, status=StageStatus.PASSED,
                )},
            ),
            "b": ModuleState(
                module_id="b", name="b",
                stages={Stage.RTL: StageState(
                    stage=Stage.RTL, status=StageStatus.FAILED,
                )},
            ),
        },
    )
    assert _class_for_stage(Stage.RTL, state) == "status-failed"


# --------------------------------------------------------------------------- #
# F22.1 — M19 stage cells (CONTRACT / ORACLE / ASSERTIONS / ORACLE_VERIFICATION)
# joined the pipeline strip between PLAN and RTL. Each is a per-module stage
# (lives on ``ModuleState.stages``) and aggregates worst-status-wins the same
# way RTL does.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stage",
    [Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS, Stage.ORACLE_VERIFICATION],
)
def test_m19_stage_pending_when_no_modules_have_state(stage: Stage) -> None:
    """No module carries the stage → cell stays gray. Matches the F19.13
    fast-path-everywhere shape (every module skipped M19)."""
    assert _class_for_stage(stage, _state()) == "status-pending"


@pytest.mark.parametrize(
    "stage,status,cls",
    [
        (Stage.CONTRACT, StageStatus.IN_PROGRESS, "status-running"),
        (Stage.CONTRACT, StageStatus.PASSED, "status-passed"),
        (Stage.CONTRACT, StageStatus.FAILED, "status-failed"),
        (Stage.ORACLE, StageStatus.PASSED, "status-passed"),
        (Stage.ASSERTIONS, StageStatus.PASSED, "status-passed"),
        (Stage.ORACLE_VERIFICATION, StageStatus.PASSED, "status-passed"),
        (Stage.ORACLE_VERIFICATION, StageStatus.FAILED, "status-failed"),
    ],
)
def test_m19_stage_reads_per_module_status(
    stage: Stage, status: StageStatus, cls: str,
) -> None:
    """A single module carrying the stage drives the cell colour."""
    state = DesignState(
        design_id="d", name="d", constraints=DesignConstraints(),
        modules={
            "counter": ModuleState(
                module_id="counter", name="counter",
                stages={stage: StageState(stage=stage, status=status)},
            ),
        },
    )
    assert _class_for_stage(stage, state) == cls


def test_m19_oracle_aggregates_worst_status_wins_across_modules() -> None:
    """Two modules with ORACLE; one PASSED, one FAILED → cell renders red.
    Same idiom RTL has used since F14.2."""
    state = DesignState(
        design_id="d", name="d", constraints=DesignConstraints(),
        modules={
            "a": ModuleState(
                module_id="a", name="a",
                stages={Stage.ORACLE: StageState(
                    stage=Stage.ORACLE, status=StageStatus.PASSED,
                )},
            ),
            "b": ModuleState(
                module_id="b", name="b",
                stages={Stage.ORACLE: StageState(
                    stage=Stage.ORACLE, status=StageStatus.FAILED,
                )},
            ),
        },
    )
    assert _class_for_stage(Stage.ORACLE, state) == "status-failed"


def test_m19_stage_pending_when_only_fast_path_modules_present() -> None:
    """F19.13 fast path: a trivial module skips CONTRACT entirely so its
    ``ModuleState.stages`` map doesn't carry it. When EVERY module took
    the fast path the cell stays PENDING — the operator sees that M19
    wasn't exercised, which matches reality."""
    state = DesignState(
        design_id="d", name="d", constraints=DesignConstraints(),
        modules={
            "trivial": ModuleState(
                module_id="trivial", name="trivial",
                # Module exists; no CONTRACT stage on it (fast-pathed).
                stages={Stage.RTL: StageState(
                    stage=Stage.RTL, status=StageStatus.PASSED,
                )},
            ),
        },
    )
    assert _class_for_stage(Stage.CONTRACT, state) == "status-pending"
    assert _class_for_stage(Stage.ORACLE, state) == "status-pending"
    assert _class_for_stage(Stage.ASSERTIONS, state) == "status-pending"
    assert _class_for_stage(Stage.ORACLE_VERIFICATION, state) == "status-pending"


# --------------------------------------------------------------------------- #
# _state_equiv: diff-aware polling — only post StageAdvanced on real change.
# --------------------------------------------------------------------------- #
def test_state_equiv_true_for_identical_color_relevant_fields() -> None:
    a = _state(has_spec=True, synth_status=StageStatus.PASSED)
    b = _state(has_spec=True, synth_status=StageStatus.PASSED)
    assert _state_equiv(a, b)


def test_state_equiv_false_when_design_status_changes() -> None:
    a = _state(status=DesignStatus.PLANNING)
    b = _state(status=DesignStatus.AWAITING_HUMAN)
    assert not _state_equiv(a, b)


def test_state_equiv_false_when_a_stage_status_changes() -> None:
    a = _state(synth_status=StageStatus.IN_PROGRESS)
    b = _state(synth_status=StageStatus.PASSED)
    assert not _state_equiv(a, b)


# --------------------------------------------------------------------------- #
# Pane rendering: apply_state updates the cell classes correctly.
# --------------------------------------------------------------------------- #
def _classes_of(cell: object) -> set[str]:
    """Set of status-* classes on the cell."""
    return {
        c for c in cell.classes  # type: ignore[attr-defined]
        if c.startswith("status-")
    }


def _drive(
    drive: Callable[[PipelinePane, object], Awaitable[dict[str, Any]]],
    *,
    checkpoint_path: Path,
    design_id: str = "d",
    size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Mount a PipelinePane in a minimal App for testing.

    ``size`` defaults to Textual's pilot default (80x24). Pass an
    explicit (width, height) for layout-sensitive tests where cell
    labels would wrap to 2 lines at the default width and inflate
    cell heights, e.g. region/overlap assertions.
    """
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def __init__(self) -> None:
            super().__init__()

        def compose(self) -> ComposeResult:
            yield PipelinePane(
                design_id=design_id, checkpoint_path=checkpoint_path,
            )

    async def _go() -> dict[str, Any]:
        app = _Host()
        kwargs: dict[str, Any] = {}
        if size is not None:
            kwargs["size"] = size
        async with app.run_test(**kwargs) as pilot:
            await pilot.pause()
            pane = app.query_one(PipelinePane)
            return await drive(pane, pilot)

    return _arun(_go())


def test_pane_starts_with_all_cells_gray(tmp_path: Path) -> None:
    """F22.1: no checkpoint yet → 11 cells, all gray. The four M19 stages
    (CONTRACT/ORACLE/ASSERTIONS/ORACLE_VERIFICATION) joined the classic
    seven (SPEC, PLAN, RTL, SYNTH, PHYSICAL, SIGNOFF, GDSII) between PLAN
    and RTL."""

    async def drive(pane: PipelinePane, pilot: object) -> dict[str, Any]:
        cells = list(pane.query("#stage-strip > Static"))
        return {"classes": [_classes_of(c) for c in cells]}

    captured = _drive(drive, checkpoint_path=tmp_path / "no-such-cp.sqlite")
    assert len(captured["classes"]) == 11
    for cls_set in captured["classes"]:
        assert "status-pending" in cls_set


# --------------------------------------------------------------------------- #
# F22.1.1 — short, fixed-width cell labels + hover tooltip with the full
# stage name. Pre-F22.1.1 the cells rendered ``stage.value.upper()`` which
# overflowed the ``width: 1fr`` cell at 11 cells per left column —
# ASSERTIONS (10 chars) and ORACLE_VERIFICATION (19) clipped to nothing.
# These tests pin the 1:1 abbreviation table so a future stage rename or
# strip-width change can't silently regress the readability win.
# --------------------------------------------------------------------------- #
_EXPECTED_LABELS: dict[Stage, str] = {
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


@pytest.mark.parametrize("stage,expected_label", list(_EXPECTED_LABELS.items()))
def test_cell_label_is_short_and_fits(
    tmp_path: Path, stage: Stage, expected_label: str,
) -> None:
    """Each cell renders the short label (≤6 chars) so 11 cells fit on a
    standard-width terminal without clipping."""
    from textual.widgets import Static

    async def drive(pane: PipelinePane, pilot: object) -> dict[str, Any]:
        cell = pane.query_one(f"#stage-{stage.value}", Static)
        return {"renderable": str(cell.renderable)}

    captured = _drive(drive, checkpoint_path=tmp_path / "no.sqlite")
    assert captured["renderable"] == expected_label
    # 6-char hard cap — width: 1fr divides ~80-char left column 11 ways =
    # ~7 chars per cell. 6 leaves a safe margin for the cell border.
    assert len(expected_label) <= 6


@pytest.mark.parametrize("stage", list(_EXPECTED_LABELS))
def test_cell_tooltip_carries_full_stage_name(
    tmp_path: Path, stage: Stage,
) -> None:
    """Hover tooltip exposes the full stage name (uppercased, underscores
    → spaces) so power users on mouse-capable terminals see the
    expansion of the short cell label."""
    from textual.widgets import Static
    expected = stage.value.upper().replace("_", " ")

    async def drive(pane: PipelinePane, pilot: object) -> dict[str, Any]:
        cell = pane.query_one(f"#stage-{stage.value}", Static)
        return {"tooltip": cell.tooltip}

    captured = _drive(drive, checkpoint_path=tmp_path / "no.sqlite")
    assert captured["tooltip"] == expected


def test_oracle_verification_tooltip_is_human_readable(tmp_path: Path) -> None:
    """Smoke-test the specific case that motivated F22.1.1 — the longest
    stage name. Cell shows ``ORC_VR``; tooltip expands to ``ORACLE
    VERIFICATION`` (no underscore)."""
    from textual.widgets import Static

    async def drive(pane: PipelinePane, pilot: object) -> dict[str, Any]:
        cell = pane.query_one("#stage-oracle_verification", Static)
        return {
            "renderable": str(cell.renderable),
            "tooltip": cell.tooltip,
        }

    captured = _drive(drive, checkpoint_path=tmp_path / "no.sqlite")
    assert captured["renderable"] == "ORC_VR"
    assert captured["tooltip"] == "ORACLE VERIFICATION"


def test_status_line_does_not_overdraw_cell_labels(tmp_path: Path) -> None:
    """F22.1.2 regression: pre-fix the strip CSS was ``height: 1`` but
    each cell's ``border: solid`` forced its true region height to 3.
    Textual still placed the status-line at ``y=2`` (right after the
    strip's *declared* 1-row height), so the status-line rendered on top
    of the cells' centred content row — the very row where the label
    text would have appeared.

    At terminals ≥140 cols every cell fits its label on one line so cell
    height stays at exactly 3 (border + content + border). This test
    pins the strict no-overlap property at that representative size so
    a future strip-height tweak can't silently re-introduce the overdraw."""
    from textual.widgets import Static

    async def drive(pane: PipelinePane, pilot: object) -> dict[str, Any]:
        await pilot.pause()  # type: ignore[attr-defined]
        cells = [
            pane.query_one(f"#stage-{s.value}", Static)
            for s in (
                Stage.SPEC, Stage.PLAN, Stage.CONTRACT, Stage.ORACLE,
                Stage.ASSERTIONS, Stage.ORACLE_VERIFICATION, Stage.RTL,
                Stage.SYNTH, Stage.PHYSICAL, Stage.SIGNOFF, Stage.GDSII,
            )
        ]
        status_line = pane.query_one("#status-line", Static)
        return {
            "cell_bottoms": [c.region.bottom for c in cells],
            "cell_heights": [c.region.height for c in cells],
            "status_top": status_line.region.y,
        }

    captured = _drive(
        drive, checkpoint_path=tmp_path / "no.sqlite", size=(140, 24),
    )
    # Every cell must be exactly 3 rows (border + content + border) at
    # this width — if Textual wrapped a label across two lines the cell
    # would balloon to 4 and the assertion below would catch it.
    assert all(h == 3 for h in captured["cell_heights"]), (
        f"cell heights at 140 cols: {captured['cell_heights']} — a label "
        f"wrapped, meaning the _STAGE_LABEL table grew too wide"
    )
    max_cell_bottom = max(captured["cell_bottoms"])
    assert max_cell_bottom <= captured["status_top"], (
        f"status-line at y={captured['status_top']} overlaps a cell that "
        f"extends to y={max_cell_bottom}; labels would be overdrawn"
    )


def test_apply_state_colors_each_stage_cell(tmp_path: Path) -> None:
    """Feed a synthetic state with each stage in a distinct status; assert
    every cell ends up in the expected color class."""
    from textual.widgets import Static
    state = _state(
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.SIGNOFF,
        has_spec=True, has_plan=True,
        rtl_status=StageStatus.PASSED,
        synth_status=StageStatus.PASSED,
        physical_status=StageStatus.PASSED,
        signoff_status=StageStatus.PASSED,
    )

    async def drive(pane: PipelinePane, pilot: object) -> dict[str, Any]:
        pane.apply_state(state)
        await pilot.pause()  # type: ignore[attr-defined]
        # Each cell has id stage-<stage.value>.
        return {
            stage.value: list(
                pane.query_one(f"#stage-{stage.value}", Static).classes,
            )
            for stage in (
                Stage.SPEC, Stage.PLAN, Stage.RTL,
                Stage.SYNTH, Stage.PHYSICAL, Stage.SIGNOFF, Stage.GDSII,
            )
        }

    captured = _drive(drive, checkpoint_path=tmp_path / "no.sqlite")
    assert "status-passed" in captured["spec"]
    assert "status-passed" in captured["plan"]
    assert "status-passed" in captured["rtl"]
    assert "status-passed" in captured["synth"]
    assert "status-passed" in captured["physical"]
    assert "status-human" in captured["signoff"]
    assert "status-pending" in captured["gdsii"]


# --------------------------------------------------------------------------- #
# Checkpoint reader: round-trip against a real cmd_run-produced checkpoint.
# --------------------------------------------------------------------------- #
@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


@pytest.fixture
def patched_router(
    monkeypatch: pytest.MonkeyPatch, routing_config: Path,
) -> StubBackend:
    backend = StubBackend()
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router", lambda _args, *, settings: router,
    )
    return backend


def test_read_design_state_returns_none_when_no_checkpoint(
    tmp_path: Path,
) -> None:
    with open_checkpoint_saver(tmp_path / "absent.sqlite") as saver:
        assert saver is None
        assert read_design_state(saver, "x") is None


def test_read_design_state_round_trips_a_real_cmd_run_checkpoint(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
) -> None:
    """Drive cmd_run against the stub backend, then read the checkpoint
    back via read_design_state and assert the rebuilt DesignState
    matches what the spine produced."""
    spec_path = Path(__file__).resolve().parent.parent / "specs" / "counter.md"
    run_dir = tmp_path / "run"
    args = RunArgs(
        cmd="run", spec_path=spec_path, name="counter",
        run_dir=run_dir, design_id="cp-rt",
        hmac_key=HMAC_KEY, config_path=routing_config,
    )
    cmd_run(args)

    cp = run_dir / "checkpoint.sqlite"
    assert cp.exists()
    with open_checkpoint_saver(cp) as saver:
        state = read_design_state(saver, "cp-rt")
    assert state is not None
    assert state.design_id == "cp-rt"
    # cmd_run drives the spine to AWAITING_HUMAN at SIGNOFF.
    assert state.status is DesignStatus.AWAITING_HUMAN
    assert state.spec is not None
    assert state.plan is not None
    assert Stage.SYNTH in state.stages
    assert state.stages[Stage.SYNTH].status is StageStatus.PASSED


def test_read_design_state_returns_none_for_unknown_design_id(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
) -> None:
    """A checkpoint exists but the design_id doesn't match — return ``None``,
    not raise."""
    spec_path = Path(__file__).resolve().parent.parent / "specs" / "counter.md"
    run_dir = tmp_path / "run"
    args = RunArgs(
        cmd="run", spec_path=spec_path, name="counter",
        run_dir=run_dir, design_id="cp-real",
        hmac_key=HMAC_KEY, config_path=routing_config,
    )
    cmd_run(args)

    with open_checkpoint_saver(run_dir / "checkpoint.sqlite") as saver:
        assert read_design_state(saver, "no-such-design") is None


def test_pipeline_pane_reflects_real_checkpoint_state(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
) -> None:
    """End-to-end: a real cmd_run-produced checkpoint, polled by a real
    PipelinePane, results in SIGNOFF cell turning blue (AWAITING_HUMAN)."""
    from textual.widgets import Static
    spec_path = Path(__file__).resolve().parent.parent / "specs" / "counter.md"
    run_dir = tmp_path / "run"
    args = RunArgs(
        cmd="run", spec_path=spec_path, name="counter",
        run_dir=run_dir, design_id="cp-live",
        hmac_key=HMAC_KEY, config_path=routing_config,
    )
    cmd_run(args)
    cp = run_dir / "checkpoint.sqlite"

    async def drive(pane: PipelinePane, pilot: object) -> dict[str, Any]:
        # Trigger one poll tick manually so the test isn't sensitive to
        # the polling interval.
        pane._poll_once()
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "signoff_classes": list(
                pane.query_one("#stage-signoff", Static).classes,
            ),
            "spec_classes": list(
                pane.query_one("#stage-spec", Static).classes,
            ),
        }

    captured = _drive(
        drive, checkpoint_path=cp, design_id="cp-live",
    )
    assert "status-human" in captured["signoff_classes"]
    assert "status-passed" in captured["spec_classes"]


# --------------------------------------------------------------------------- #
# Status-line formatter — pure-function tests for every workflow state.
# --------------------------------------------------------------------------- #
from chip_agent.design_state import EscalationLevel  # noqa: E402
from chip_agent.tui.panes.pipeline import (  # noqa: E402
    _IDLE_STATUS,
    _format_status_line,
)


def test_status_line_idle_when_no_design_state() -> None:
    text, tone = _format_status_line(None)
    assert text == _IDLE_STATUS
    assert tone is None


def test_status_line_idle_when_design_has_no_spec_yet() -> None:
    text, tone = _format_status_line(_state())
    assert text == _IDLE_STATUS
    assert tone is None


def test_status_line_completed_announces_gdsii() -> None:
    state = _state(
        status=DesignStatus.COMPLETED,
        current_stage=Stage.GDSII,
        has_spec=True, has_plan=True,
    )
    text, tone = _format_status_line(state)
    assert "COMPLETED" in text
    assert tone == "passed"


def test_status_line_awaiting_human_at_signoff_invites_ctrl_a() -> None:
    state = _state(
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.SIGNOFF,
        has_spec=True, has_plan=True,
        signoff_status=StageStatus.PASSED,
    )
    text, tone = _format_status_line(state)
    assert "Ctrl+A" in text
    assert "SIGNOFF" in text
    assert tone == "human"


def test_status_line_awaiting_human_at_earlier_stage_says_stuck() -> None:
    """When the spine pauses at an earlier stage (escalation walked up to
    HUMAN at RTL), the status line must NOT promise Ctrl+A — that key
    is blocked by F14.3's refusal. It should phrase the situation as
    stuck + restart required."""
    state = _state(
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.RTL,
        has_spec=True, has_plan=True,
        rtl_status=StageStatus.BLOCKED,
    )
    text, tone = _format_status_line(state)
    assert "Stuck" in text
    assert "RTL" in text
    assert "Ctrl+A" not in text
    assert tone == "failed"


def test_status_line_failed_announces_stage() -> None:
    state = _state(
        status=DesignStatus.FAILED,
        current_stage=Stage.SYNTH,
        has_spec=True, has_plan=True,
        synth_status=StageStatus.FAILED,
    )
    text, tone = _format_status_line(state)
    assert "FAILED" in text
    assert "SYNTH" in text
    assert tone == "failed"


def test_status_line_running_rtl_shows_attempt_and_escalation() -> None:
    """Per-module RTL surfaces module name, attempt budget, escalation."""
    state = DesignState(
        design_id="d", name="d", constraints=DesignConstraints(),
        spec=_spec_ref(), plan=_plan_ref(),
        status=DesignStatus.RUNNING,
        current_stage=Stage.RTL,
        modules={
            "counter_8bit": ModuleState(
                module_id="counter_8bit", name="counter_8bit",
                stages={Stage.RTL: StageState(
                    stage=Stage.RTL, status=StageStatus.IN_PROGRESS,
                    attempts=2, max_attempts=5,
                    escalation=EscalationLevel.OUTER,
                )},
            ),
        },
    )
    text, tone = _format_status_line(state)
    assert "counter_8bit" in text
    assert "2/5" in text
    assert "outer" in text.lower()
    assert tone == "running"


def test_status_line_running_design_level_stage_shows_attempt() -> None:
    state = _state(
        status=DesignStatus.RUNNING,
        current_stage=Stage.SYNTH,
        has_spec=True, has_plan=True,
        synth_status=StageStatus.IN_PROGRESS,
    )
    text, tone = _format_status_line(state)
    assert "SYNTH" in text
    assert "attempt" in text
    assert tone == "running"


def test_status_line_widget_updates_on_apply_state(tmp_path: Path) -> None:
    """End-to-end: mount the pane, call apply_state, status Static reflects it."""
    from textual.widgets import Static

    target = _state(
        status=DesignStatus.AWAITING_HUMAN,
        current_stage=Stage.SIGNOFF,
        has_spec=True, has_plan=True,
        signoff_status=StageStatus.PASSED,
    )

    async def drive(pane: PipelinePane, pilot: object) -> dict[str, Any]:
        pane.apply_state(target)
        await pilot.pause()  # type: ignore[attr-defined]
        line = pane.query_one("#status-line", Static)
        return {
            "text": str(line.renderable),
            "classes": _classes_of_line(line),
        }

    captured = _drive(drive, checkpoint_path=tmp_path / "no-such-cp.sqlite")
    assert "Ctrl+A" in captured["text"]
    assert "status-line-human" in captured["classes"]


def _classes_of_line(line: object) -> set[str]:
    return {
        c for c in line.classes  # type: ignore[attr-defined]
        if c.startswith("status-line-")
    }
