"""F6.2 acceptance: LibreLane physical service.

* Parser turns metrics + DEF bytes into a typed :class:`PhysicalRun` —
  congestion above the hard threshold and missing-DEF flow runs become
  ``Violation``s with the right ``code`` / ``severity``.
* Service stages the netlist + JSON config into the sandbox, invokes
  LibreLane, harvests ``runs/<tag>/final/{def,metrics}``, and returns
  ``(LayoutArtifact, PhysicalRun)`` with content-addressed DEF.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import (
    DesignConstraints,
    NetlistArtifact,
    PhysicalRepairRoute,
    PhysicalRepairRouteKind,
    Provenance,
    Stage,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.librelane import (
    DEFAULT_FLOW_RUN_TAG,
    LIBRELANE_BIN,
    LibreLaneFlowError,
    LibreLanePhysicalService,
    PhysicalConfig,
    _detect_multi_corner_segfault,
    apply_repair_delta,
    build_librelane_config,
    from_constraints,
    parse_librelane_outputs,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run(*, rc: int = 0, stdout: str = "", stderr: str = "") -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.1,
    )


def _clean_metrics() -> dict[str, float]:
    """A passing PhysicalRun shape.

    F13.2: includes the canonical chipathon26 keys (double underscore between
    domain + field) plus cell_count so no warning fires.
    """
    return {
        "route__congestion__overflow": 0.0,
        "design__core__util": 0.55,            # F13.2 canonical
        "design__die__area": 12345.6,          # F13.2 canonical
        "design__instance__count": 1.0,        # F12.4 + F13.2
        "magic__drc__errors": 0,
    }


def _congested_metrics() -> dict[str, float]:
    return {
        "route__congestion__overflow": 0.18,  # well above 5% threshold
        "design__core_util": 0.80,
        "design__die_area": 9876.0,
        "magic__drc__errors": 0,
    }


# --------------------------------------------------------------------------- #
# PhysicalConfig retune semantics
# --------------------------------------------------------------------------- #
def test_retune_lowers_utilization_by_delta() -> None:
    c = PhysicalConfig(design_name="d", top_module="t", target_utilization=0.55)
    nxt = c.retuned(utilization_delta=0.05, floor_utilization=0.30)
    assert nxt.target_utilization == 0.50
    # original is untouched (frozen + pure)
    assert c.target_utilization == 0.55


def test_retune_clamps_at_floor() -> None:
    c = PhysicalConfig(design_name="d", top_module="t", target_utilization=0.32)
    nxt = c.retuned(utilization_delta=0.05, floor_utilization=0.30)
    assert nxt.target_utilization == 0.30
    # Re-applying after we hit the floor is a no-op — driver uses this as
    # the exhaustion signal.
    nxt2 = nxt.retuned(utilization_delta=0.05, floor_utilization=0.30)
    assert nxt2.target_utilization == 0.30


# --------------------------------------------------------------------------- #
# from_constraints maps DesignConstraints onto a PhysicalConfig
# --------------------------------------------------------------------------- #
def test_from_constraints_pulls_pdk_and_lib() -> None:
    constraints = DesignConstraints(
        pdk="sky130A", std_cell_lib="sky130_fd_sc_ms",
        target_clock_ns=4.0, max_die_area_um2=10000.0,
        target_utilization=0.45,
    )
    c = from_constraints(constraints, design_name="d0", top_module="counter")
    assert c.pdk == "sky130A"
    assert c.std_cell_lib == "sky130_fd_sc_ms"
    assert c.clock_period_ns == 4.0
    assert c.target_die_area_um2 == 10000.0
    assert c.target_utilization == 0.45


def test_from_constraints_uses_default_when_utilization_absent() -> None:
    c = from_constraints(
        DesignConstraints(), design_name="d", top_module="t",
        target_utilization=0.7,
    )
    assert c.target_utilization == 0.7


# --------------------------------------------------------------------------- #
# F21.2-D — PhysicalConfig threads SignoffSettings.sta_corners / sta_report_power
#
# Single-corner default keeps today's path byte-identical; multi-corner opt-in
# survives the retune copy + the from_constraints seam.
# --------------------------------------------------------------------------- #
def test_physical_config_sta_corners_default_is_single_corner() -> None:
    c = PhysicalConfig(design_name="d", top_module="t")
    assert c.sta_corners is None
    assert c.sta_report_power is False


def test_physical_config_accepts_sta_corners_tuple() -> None:
    c = PhysicalConfig(
        design_name="d", top_module="t",
        sta_corners=("tt", "ss", "ff"), sta_report_power=True,
    )
    assert c.sta_corners == ("tt", "ss", "ff")
    assert c.sta_report_power is True


def test_physical_config_retuned_preserves_sta_corners() -> None:
    """Inner-loop retune (target_utilization decrement) must not drop the
    per-corner opt-in; the same multi-corner reports are needed on the
    re-run."""
    c = PhysicalConfig(
        design_name="d", top_module="t",
        sta_corners=("tt", "ss", "ff"), sta_report_power=True,
    )
    again = c.retuned()
    assert again.sta_corners == ("tt", "ss", "ff")
    assert again.sta_report_power is True


def test_from_constraints_threads_sta_corners() -> None:
    c = from_constraints(
        DesignConstraints(), design_name="d", top_module="t",
        sta_corners=("tt", "ss"), sta_report_power=True,
    )
    assert c.sta_corners == ("tt", "ss")
    assert c.sta_report_power is True


def test_from_constraints_sta_corners_default_unset() -> None:
    """Existing callers (no F21.2 kwargs) get None / False — single-corner
    path unchanged."""
    c = from_constraints(
        DesignConstraints(), design_name="d", top_module="t",
    )
    assert c.sta_corners is None
    assert c.sta_report_power is False


# --------------------------------------------------------------------------- #
# build_librelane_config — only the keys LibreLane recognises
# --------------------------------------------------------------------------- #
def test_config_dict_has_required_keys() -> None:
    c = PhysicalConfig(
        design_name="counter", top_module="counter",
        clock_period_ns=5.0, target_utilization=0.5,
    )
    cfg = build_librelane_config(c, verilog_files=["design.nl.v"])
    assert cfg["DESIGN_NAME"] == "counter"
    assert cfg["VERILOG_FILES"] == ["design.nl.v"]
    assert cfg["FP_CORE_UTIL"] == 50.0  # 0.5 -> 50.0 (LibreLane uses percent)
    assert cfg["CLOCK_PERIOD"] == 5.0
    assert cfg["CLOCK_PORT"] == "clk"


def test_config_dict_omits_optional_keys_when_unset() -> None:
    c = PhysicalConfig(design_name="d", top_module="t")
    cfg = build_librelane_config(c, verilog_files=["d.nl.v"])
    assert "CLOCK_PERIOD" not in cfg
    assert "DIE_AREA" not in cfg


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_clean_routed_design_passes() -> None:
    parse = parse_librelane_outputs(
        _run(), metrics=_clean_metrics(),
        def_bytes=b"# DEF\nEND DESIGN\n",
        stage_reached_override="routed",
    )
    assert parse.passed
    assert parse.stage_reached == "routed"
    assert parse.congested is False
    assert parse.violations == []
    assert parse.die_area_um2 == 12345.6


def test_congested_design_fails_with_typed_violation() -> None:
    # AC: a forced-overcongested config -> the run is marked failed and
    # congestion is surfaced as a typed PHYSICAL.CONGESTION violation. The
    # driver uses this signal to retune; the test for that is in
    # tests/test_physical_stage.py.
    parse = parse_librelane_outputs(
        _run(), metrics=_congested_metrics(),
        def_bytes=b"# DEF\nEND DESIGN\n",
        stage_reached_override="routed",
    )
    assert not parse.passed
    assert parse.congested
    assert parse.congestion_pct == 18.0
    assert any(v.code == "PHYSICAL.CONGESTION" for v in parse.violations)


def test_drc_errors_surface_as_violation() -> None:
    metrics = _clean_metrics() | {"magic__drc__errors": 3}
    parse = parse_librelane_outputs(
        _run(), metrics=metrics,
        def_bytes=b"# DEF\n", stage_reached_override="routed",
    )
    assert not parse.passed
    drc = [v for v in parse.violations if v.code == "PHYSICAL.DRC"]
    assert len(drc) == 1
    assert "3" in drc[0].message


def test_flow_returned_zero_without_def_fails() -> None:
    parse = parse_librelane_outputs(
        _run(rc=0), metrics=_clean_metrics(),
        def_bytes=b"", stage_reached_override="placed",
    )
    assert not parse.passed
    assert any(v.code == "PHYSICAL.FLOW" for v in parse.violations)


def test_nonzero_returncode_fails_even_with_def() -> None:
    parse = parse_librelane_outputs(
        _run(rc=1), metrics=_clean_metrics(),
        def_bytes=b"# DEF\n", stage_reached_override="routed",
    )
    assert not parse.passed


# --------------------------------------------------------------------------- #
# Runner wiring with a stub sandbox
# --------------------------------------------------------------------------- #
@dataclass
class StubSandbox:
    """Mimics what real LibreLane does: writes runs/<tag>/final/{def,metrics}
    into the mount dir, then returns a canned :class:`ToolRun`."""

    tool_run: ToolRun
    side_effect: Callable[[Path], None] | None = None
    calls: list[tuple[list[str], Path, dict[str, bytes]]] = field(default_factory=list)

    def run(
        self, cmd: list[str], mount: Path | str, *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun:
        mp = Path(mount)
        staged = {p.name: p.read_bytes() for p in mp.iterdir() if p.is_file()}
        self.calls.append((list(cmd), mp, staged))
        if self.side_effect is not None:
            self.side_effect(mp)
        return self.tool_run


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


def _stage_netlist(
    store: SqliteArtifactStore, *,
    design_id: str = "d0", top: str = "counter",
) -> NetlistArtifact:
    blob = store.put_blob(b"// gate-level\n", media_type="text/x-verilog")
    art = NetlistArtifact(
        artifact_id=f"{design_id}.{top}.netlist",
        design_id=design_id, module_id=top,
        netlist=blob,
        std_cell_lib="sky130_fd_sc_hd",
        cell_count=42,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _drop_artifacts(
    *, design_name: str, def_text: str, metrics: dict[str, float],
    run_tag: str = DEFAULT_FLOW_RUN_TAG,
) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        final = mount / "runs" / run_tag / "final"
        (final / "def").mkdir(parents=True, exist_ok=True)
        (final / "def" / f"{design_name}.def").write_text(def_text)
        (final / "metrics.json").write_text(json.dumps(metrics))
    return _impl


def test_runner_command_shape(store: SqliteArtifactStore) -> None:
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")

    layout, run = svc.run_flow(netlist, config=cfg)

    assert run.passed
    assert layout.stage_reached == "routed"
    assert store.get_blob(layout.def_file) == b"# DEF\nEND DESIGN\n"

    cmd, _mount, staged = sandbox.calls[0]
    assert cmd[0] == LIBRELANE_BIN
    assert "--run-tag" in cmd
    assert cmd[-1] == "config.json"
    assert "design.nl.v" in staged
    assert "config.json" in staged
    cfg_in_sandbox = json.loads(staged["config.json"])
    assert cfg_in_sandbox["DESIGN_NAME"] == "counter"


def test_runner_provenance_links_netlist(store: SqliteArtifactStore) -> None:
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter",
                         clock_period_ns=5.0)
    layout, _ = svc.run_flow(netlist, config=cfg)
    assert layout.provenance.inputs == [netlist.ref()]
    assert layout.provenance.config["target_utilization"] == cfg.target_utilization
    assert layout.provenance.config["clock_period_ns"] == 5.0


def test_runner_surfaces_congestion(store: SqliteArtifactStore) -> None:
    # AC: an over-congested config produces a failing PhysicalRun the driver
    # can react to (the retune assertion lives in test_physical_stage.py).
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_congested_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, run = svc.run_flow(netlist, config=cfg)
    assert not run.passed
    assert run.congested
    assert any(v.code == "PHYSICAL.CONGESTION" for v in run.violations)
    # Layout still recorded — provenance is preserved even on a failing run.
    assert layout.metadata["congestion_pct"] > 5.0


def test_runner_records_layout_even_on_failure(store: SqliteArtifactStore) -> None:
    # A soft failure (no DEF written) still produces a typed PhysicalRun and
    # a LayoutArtifact — provenance is preserved so the next attempt can
    # link back to what was tried. The DEF blob ends up empty.
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(tool_run=_run())  # no side_effect: no DEF
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, run = svc.run_flow(netlist, config=cfg)
    assert not run.passed
    assert any(v.code == "PHYSICAL.FLOW" for v in run.violations)
    assert store.get_blob(layout.def_file) == b""


# --------------------------------------------------------------------------- #
# F18: rc!=0 + no DEF = "the flow died at startup". Pre-fix this silently
# produced an empty layout and the spine escalated to HUMAN with zero
# signal. Post-fix it raises LibreLaneFlowError with the captured stderr
# tail so the TUI can surface the real reason, and the layout blob carries
# the full stdout+stderr for post-mortem inspection.
# --------------------------------------------------------------------------- #
def test_run_flow_raises_when_nonzero_rc_and_missing_def(
    store: SqliteArtifactStore,
) -> None:
    """The pre-fix silent-fail trap: librelane exits non-zero in seconds
    without producing a DEF. The user's live "counter" run hit exactly
    this path — synth promoted at 20:55:08, await_human at 20:55:16,
    layout DEF was 0 bytes (sha256 of the empty string), and the audit
    trail offered nothing actionable. After the fix the failure raises
    loudly with the tail of stderr in the message."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(tool_run=_run(
        rc=1,
        stderr=(
            "ERROR: 'CLOCK_PORT' references a port 'clk' that does not "
            "exist in any of the design's Verilog files.\n"
            "FATAL: Flow stopped before any step ran.\n"
        ),
    ))
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    with pytest.raises(LibreLaneFlowError) as exc_info:
        svc.run_flow(netlist, config=cfg)
    msg = str(exc_info.value)
    assert "rc=1" in msg
    assert "counter" in msg
    # The stderr tail must be embedded so the user can read the real cause.
    assert "CLOCK_PORT" in msg
    assert "FATAL" in msg


def test_run_flow_raises_with_helpful_message_when_output_is_empty(
    store: SqliteArtifactStore,
) -> None:
    """Defensive: if the sandbox runner died so early it captured no
    stdout AND no stderr (rare — usually the entrypoint banner alone
    produces some bytes), the error message still names the design and
    the rc so the user knows what to triage."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(tool_run=_run(rc=125, stdout="", stderr=""))
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    with pytest.raises(LibreLaneFlowError) as exc_info:
        svc.run_flow(netlist, config=cfg)
    msg = str(exc_info.value)
    assert "rc=125" in msg
    assert "counter" in msg
    assert "emitted no output" in msg


def test_run_flow_does_not_raise_when_def_present_despite_nonzero_rc(
    store: SqliteArtifactStore,
) -> None:
    """LibreLane occasionally exits non-zero with a real DEF on disk
    (post-route checker failed but place+route succeeded). The driver
    must still get a typed (LayoutArtifact, PhysicalRun) pair so it can
    decide retune-or-give-up — the raise is reserved for the "flow died
    before producing anything" pathology."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(rc=2),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    # No raise — the soft-failure path returns the pair with passed=False.
    layout, run = svc.run_flow(netlist, config=cfg)
    assert not run.passed                          # rc != 0 still fails the gate
    assert store.get_blob(layout.def_file) == b"# DEF\nEND DESIGN\n"


def test_librelane_log_blob_captures_stdout_and_stderr(
    store: SqliteArtifactStore,
) -> None:
    """A healthy run still persists the LibreLane log as a sidecar blob
    so a future post-mortem can compare a sick run against a healthy
    one without re-executing the flow. Both streams are included with
    section headers so the consumer can split them."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(
            stdout="Classic - Stage 80 - Report Manufacturability\nFlow complete.\n",
            stderr="[WARN] 2 Lint warnings found.\n",
        ),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, run = svc.run_flow(netlist, config=cfg)
    assert run.passed
    assert layout.librelane_log is not None
    body = store.get_blob(layout.librelane_log).decode()
    assert "=== stdout ===" in body
    assert "=== stderr ===" in body
    assert "Flow complete." in body
    assert "2 Lint warnings" in body


def test_librelane_log_is_none_when_both_streams_empty(
    store: SqliteArtifactStore,
) -> None:
    """No streams to capture -> field stays None, not an empty blob.
    Keeps the audit's "no output captured" signal distinguishable from
    "a blob exists but it's empty"."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),  # rc=0, stdout="", stderr=""
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _phys_run = svc.run_flow(netlist, config=cfg)
    assert layout.librelane_log is None


def test_librelane_log_captured_on_soft_failure(
    store: SqliteArtifactStore,
) -> None:
    """Even when the flow is a soft failure (rc=0, no DEF — handled by
    the existing PHYSICAL.FLOW violation path, NOT the new raise), the
    log blob still gets persisted on the layout so the audit trail
    explains what LibreLane was doing when it gave up."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(tool_run=_run(
        stdout="[OpenROAD.CTS] No CLOCK_NET specified. Returning state unaltered.\n",
        stderr="",
    ))
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, run = svc.run_flow(netlist, config=cfg)
    assert not run.passed
    assert any(v.code == "PHYSICAL.FLOW" for v in run.violations)
    assert layout.librelane_log is not None
    body = store.get_blob(layout.librelane_log).decode()
    assert "No CLOCK_NET specified" in body


# --------------------------------------------------------------------------- #
# F12.1: harvest pass picks up the CTS SDF and exposes it via
# ``LayoutArtifact.librelane_sdf``. Missing SDFs leave the field ``None``.
# --------------------------------------------------------------------------- #
def _drop_artifacts_with_sdf(
    *,
    design_name: str,
    def_text: str,
    metrics: dict[str, float],
    sdf_text: str,
    sdf_path: str = "final/sdf",
    run_tag: str = DEFAULT_FLOW_RUN_TAG,
) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        final = mount / "runs" / run_tag / "final"
        (final / "def").mkdir(parents=True, exist_ok=True)
        (final / "def" / f"{design_name}.def").write_text(def_text)
        (final / "metrics.json").write_text(json.dumps(metrics))
        sdf_dir = mount / "runs" / run_tag / sdf_path
        sdf_dir.mkdir(parents=True, exist_ok=True)
        (sdf_dir / f"{design_name}.sdf").write_text(sdf_text)
    return _impl


def test_harvest_captures_sdf_when_emitted(store: SqliteArtifactStore) -> None:
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_sdf(
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
            sdf_text="(DELAYFILE (SDFVERSION \"3.0\"))\n",
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_sdf is not None
    body = store.get_blob(layout.librelane_sdf)
    assert body == b"(DELAYFILE (SDFVERSION \"3.0\"))\n"


def test_harvest_captures_sdf_from_cts_dir(store: SqliteArtifactStore) -> None:
    """LibreLane sometimes dumps the SDF under ``runs/<tag>/cts/`` instead.

    The harvest pass tolerates both layouts.
    """
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_sdf(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
            sdf_text="(DELAYFILE (SDFVERSION \"3.0\"))\n",
            sdf_path="cts",
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_sdf is not None
    assert store.get_blob(layout.librelane_sdf).startswith(b"(DELAYFILE")


def test_harvest_omits_sdf_field_when_not_emitted(
    store: SqliteArtifactStore,
) -> None:
    """No SDF dropped — ``LayoutArtifact.librelane_sdf`` stays ``None``."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_sdf is None


# --------------------------------------------------------------------------- #
# F12.2: harvest pass captures LibreLane's Magic DRC report so SIGNOFF
# can cross-check it against our re-run.
# --------------------------------------------------------------------------- #
def _drop_artifacts_with_drc_report(
    *,
    design_name: str,
    def_text: str,
    metrics: dict[str, float],
    drc_report_text: str,
    drc_subpath: str = "route/reports",
    run_tag: str = DEFAULT_FLOW_RUN_TAG,
) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        final = mount / "runs" / run_tag / "final"
        (final / "def").mkdir(parents=True, exist_ok=True)
        (final / "def" / f"{design_name}.def").write_text(def_text)
        (final / "metrics.json").write_text(json.dumps(metrics))
        drc_dir = mount / "runs" / run_tag / drc_subpath
        drc_dir.mkdir(parents=True, exist_ok=True)
        (drc_dir / "magic_drc.rpt").write_text(drc_report_text)
    return _impl


def test_harvest_captures_magic_drc_report(store: SqliteArtifactStore) -> None:
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_drc_report(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
            drc_report_text="Total DRC errors found: 0\n",
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_drc_report is not None
    body = store.get_blob(layout.librelane_drc_report)
    assert b"Total DRC errors found: 0" in body


def test_harvest_omits_drc_report_when_not_emitted(
    store: SqliteArtifactStore,
) -> None:
    """No DRC report dropped — ``LayoutArtifact.librelane_drc_report`` stays None."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_drc_report is None


# --------------------------------------------------------------------------- #
# F12.3: harvest pass captures LibreLane's extracted SPICE.
# --------------------------------------------------------------------------- #
def _drop_artifacts_with_spice(
    *,
    design_name: str,
    def_text: str,
    metrics: dict[str, float],
    spice_text: str,
    spice_subpath: str = "final/spice",
    run_tag: str = DEFAULT_FLOW_RUN_TAG,
) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        final = mount / "runs" / run_tag / "final"
        (final / "def").mkdir(parents=True, exist_ok=True)
        (final / "def" / f"{design_name}.def").write_text(def_text)
        (final / "metrics.json").write_text(json.dumps(metrics))
        spice_dir = mount / "runs" / run_tag / spice_subpath
        spice_dir.mkdir(parents=True, exist_ok=True)
        (spice_dir / f"{design_name}.spice").write_text(spice_text)
    return _impl


def test_harvest_captures_layout_spice(store: SqliteArtifactStore) -> None:
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_spice(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
            spice_text=".SUBCKT counter clk q\n.ENDS\n",
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_layout_spice is not None
    body = store.get_blob(layout.librelane_layout_spice)
    assert b".SUBCKT counter" in body


def test_harvest_captures_layout_spice_from_extraction_dir(
    store: SqliteArtifactStore,
) -> None:
    """Older flows write the SPICE under ``runs/<tag>/extraction/``."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_spice(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
            spice_text=".SUBCKT counter\n.ENDS\n",
            spice_subpath="extraction",
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_layout_spice is not None


def test_harvest_omits_layout_spice_when_not_emitted(
    store: SqliteArtifactStore,
) -> None:
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_layout_spice is None


# --------------------------------------------------------------------------- #
# F12.4: metric population — cell_count + congestion_pct land on the layout
# from metrics.json, and the GDSII inherits cell_count via the layout.
# --------------------------------------------------------------------------- #
def test_layout_populates_cell_count_from_instance_count_metric(
    store: SqliteArtifactStore,
) -> None:
    netlist = _stage_netlist(store)
    metrics = dict(_clean_metrics())
    metrics["design__instance__count"] = 245.0
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=metrics,
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, run = svc.run_flow(netlist, config=cfg)

    assert layout.cell_count == 245
    assert run.cell_count == 245


def test_layout_populates_congestion_pct(
    store: SqliteArtifactStore,
) -> None:
    netlist = _stage_netlist(store)
    metrics = dict(_clean_metrics())
    metrics["route__congestion__overflow"] = 0.012
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=metrics,
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.congestion_pct is not None
    assert layout.congestion_pct == pytest.approx(1.2)


def test_layout_cell_count_defaults_to_zero_when_metric_absent(
    store: SqliteArtifactStore,
) -> None:
    netlist = _stage_netlist(store)
    metrics = {"route__congestion__overflow": 0.0}  # no instance count key
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=metrics,
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.cell_count == 0


# --------------------------------------------------------------------------- #
# F13.1: harvest pass captures LibreLane's mapped Verilog netlist.
# --------------------------------------------------------------------------- #
_MAPPED_NETLIST_BODY = (
    "module counter (clk, rst_n, q);\n"
    "  input clk;\n"
    "  input rst_n;\n"
    "  output [7:0] q;\n"
    "  sky130_fd_sc_hd__dfrtp_2 _0_ (.CLK(clk), .D(_n0_), .RESET_B(rst_n), .Q(q[0]));\n"
    "  sky130_fd_sc_hd__inv_2 _1_ (.A(q[0]), .Y(_n0_));\n"
    "endmodule\n"
)


def _drop_artifacts_with_mapped_netlist(
    *,
    design_name: str,
    def_text: str,
    metrics: dict[str, float],
    netlist_text: str,
    netlist_subpath: str = "final/nl",
    netlist_filename: str | None = None,
    run_tag: str = DEFAULT_FLOW_RUN_TAG,
) -> Callable[[Path], None]:
    name = netlist_filename or f"{design_name}.nl.v"

    def _impl(mount: Path) -> None:
        final = mount / "runs" / run_tag / "final"
        (final / "def").mkdir(parents=True, exist_ok=True)
        (final / "def" / f"{design_name}.def").write_text(def_text)
        (final / "metrics.json").write_text(json.dumps(metrics))
        nl_dir = mount / "runs" / run_tag / netlist_subpath
        nl_dir.mkdir(parents=True, exist_ok=True)
        (nl_dir / name).write_text(netlist_text)
    return _impl


def test_harvest_captures_mapped_netlist_from_final_nl_path(
    store: SqliteArtifactStore,
) -> None:
    """LibreLane's canonical mapped netlist path is
    ``runs/<tag>/final/nl/<design>.nl.v``."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_mapped_netlist(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
            netlist_text=_MAPPED_NETLIST_BODY,
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_mapped_netlist is not None
    body = store.get_blob(layout.librelane_mapped_netlist).decode("utf-8")
    assert "sky130_fd_sc_hd__dfrtp_2" in body
    assert "module counter" in body


def test_harvest_captures_mapped_netlist_from_yosys_synthesis_path(
    store: SqliteArtifactStore,
) -> None:
    """Older flows write the netlist under ``runs/<tag>/yosys.synthesis/``."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_mapped_netlist(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
            netlist_text=_MAPPED_NETLIST_BODY,
            netlist_subpath="yosys.synthesis",
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_mapped_netlist is not None


def test_harvest_omits_mapped_netlist_when_not_emitted(
    store: SqliteArtifactStore,
) -> None:
    """No netlist dropped — ``LayoutArtifact.librelane_mapped_netlist`` stays None.

    Important: the stub flow doesn't emit a mapped netlist, so SIGNOFF
    falls back to ``NetlistArtifact.netlist`` (the pre-F13.1 behaviour).
    """
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_mapped_netlist is None


# --------------------------------------------------------------------------- #
# F13.4-B: harvest pass captures LibreLane's powered netlist (PNL).
# --------------------------------------------------------------------------- #
_POWERED_NETLIST_BODY = (
    "module counter (VPWR, VGND, clk, q\\[0\\] , q\\[1\\] , q\\[2\\] , q\\[3\\] ,\n"
    "                q\\[4\\] , q\\[5\\] , q\\[6\\] , q\\[7\\] , rst_n);\n"
    "  input VPWR;\n"
    "  input VGND;\n"
    "  input clk;\n"
    "  output q\\[0\\] ;\n"
    "  output q\\[1\\] ;\n"
    "  output q\\[7\\] ;\n"
    "  input rst_n;\n"
    "  sky130_fd_sc_hd__dfrtp_2 _0_\n"
    "    (.CLK(clk), .D(_n0_), .RESET_B(rst_n), .Q(q\\[0\\] ),\n"
    "     .VPWR(VPWR), .VGND(VGND));\n"
    "endmodule\n"
)


def _drop_artifacts_with_pnl(
    *,
    design_name: str,
    def_text: str,
    metrics: dict[str, float],
    pnl_text: str,
    pnl_subpath: str = "final/pnl",
    run_tag: str = DEFAULT_FLOW_RUN_TAG,
) -> Callable[[Path], None]:
    """Like ``_drop_artifacts_with_mapped_netlist`` but drops a powered netlist."""

    def _impl(mount: Path) -> None:
        final = mount / "runs" / run_tag / "final"
        (final / "def").mkdir(parents=True, exist_ok=True)
        (final / "def" / f"{design_name}.def").write_text(def_text)
        (final / "metrics.json").write_text(json.dumps(metrics))
        pnl_dir = mount / "runs" / run_tag / pnl_subpath
        pnl_dir.mkdir(parents=True, exist_ok=True)
        (pnl_dir / f"{design_name}.pnl.v").write_text(pnl_text)
    return _impl


def test_harvest_captures_powered_netlist_from_final_pnl_path(
    store: SqliteArtifactStore,
) -> None:
    """LibreLane's canonical PNL path is ``runs/<tag>/final/pnl/<design>.pnl.v``."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_pnl(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
            pnl_text=_POWERED_NETLIST_BODY,
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_powered_netlist is not None
    body = store.get_blob(layout.librelane_powered_netlist).decode("utf-8")
    assert "VPWR" in body
    assert "VGND" in body
    # Powered netlist uses bit-blasted ports to match the extracted SPICE.
    assert "q\\[0\\]" in body
    assert "q\\[7\\]" in body


def test_harvest_omits_powered_netlist_when_not_emitted(
    store: SqliteArtifactStore,
) -> None:
    """No PNL dropped — ``LayoutArtifact.librelane_powered_netlist`` stays None.

    LVS then falls back to ``librelane_mapped_netlist`` (signoff_stage.py).
    """
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(
            design_name="counter",
            def_text="# DEF\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_powered_netlist is None


# --------------------------------------------------------------------------- #
# F13.2: metric-key drift handling. The chipathon26 image emits the
# double-underscore form; the legacy single-underscore form must keep working
# for older flows. A missing key emits a warning so future drifts surface.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "die_area_key",
    [
        "design__die__area",          # F13.2 chipathon26 canonical
        "design__die__area__um2",     # explicit units variant
        "floorplan__die_area",        # alternate domain
        "design__die_area",           # legacy
        "die__area",                  # legacy short form
    ],
)
def test_die_area_extracted_from_each_pinned_key(
    die_area_key: str,
) -> None:
    metrics = {
        die_area_key: 42_000.0,
        # supply enough to keep the other warn-on-miss helpers quiet:
        "design__core__util": 0.5,
        "design__instance__count": 100.0,
    }
    parse = parse_librelane_outputs(
        _run(rc=0), metrics=metrics, def_bytes=b"# DEF\n",
    )
    assert parse.die_area_um2 == 42_000.0


@pytest.mark.parametrize(
    "util_key",
    [
        "design__core__util",         # F13.2 chipathon26 canonical
        "design__instance__utilization",  # variant
        "floorplan__core_util",       # legacy
        "design__core_util",          # legacy
        "design__utilization",        # legacy
        "design__util",               # short
    ],
)
def test_utilization_extracted_from_each_pinned_key(util_key: str) -> None:
    metrics = {
        util_key: 0.45,
        "design__die__area": 100.0,
        "design__instance__count": 10.0,
    }
    parse = parse_librelane_outputs(
        _run(rc=0), metrics=metrics, def_bytes=b"# DEF\n",
    )
    assert parse.utilization_pct == pytest.approx(45.0)


@pytest.mark.parametrize(
    "cell_count_key",
    [
        "design__instance__count",        # F12.4/F13.2 canonical
        "design__instance__count__total",  # variant
        "design__instance_count",         # legacy
        "design__inst_count_total",       # legacy short
    ],
)
def test_cell_count_extracted_from_each_pinned_key(cell_count_key: str) -> None:
    metrics = {
        cell_count_key: 245.0,
        "design__die__area": 100.0,
        "design__core__util": 0.3,
    }
    parse = parse_librelane_outputs(
        _run(rc=0), metrics=metrics, def_bytes=b"# DEF\n",
    )
    assert parse.cell_count == 245


def test_warns_when_die_area_missing_from_metrics() -> None:
    """F13.2: when no pinned die_area key matches, emit a one-line warning."""
    metrics = {
        # die_area key intentionally omitted
        "design__core__util": 0.5,
        "design__instance__count": 10.0,
        "route__congestion__overflow": 0.0,
    }
    with pytest.warns(UserWarning, match="die_area"):
        parse = parse_librelane_outputs(
            _run(rc=0), metrics=metrics, def_bytes=b"# DEF\n",
        )
    assert parse.die_area_um2 is None


def test_warns_when_utilization_missing_from_metrics() -> None:
    metrics = {
        "design__die__area": 100.0,
        "design__instance__count": 10.0,
        "route__congestion__overflow": 0.0,
        # utilization key omitted
    }
    with pytest.warns(UserWarning, match="utilization"):
        parse = parse_librelane_outputs(
            _run(rc=0), metrics=metrics, def_bytes=b"# DEF\n",
        )
    assert parse.utilization_pct == 0.0


def test_warns_when_cell_count_missing_from_metrics() -> None:
    metrics = {
        "design__die__area": 100.0,
        "design__core__util": 0.5,
        "route__congestion__overflow": 0.0,
        # instance count omitted
    }
    with pytest.warns(UserWarning, match="cell_count"):
        parse = parse_librelane_outputs(
            _run(rc=0), metrics=metrics, def_bytes=b"# DEF\n",
        )
    assert parse.cell_count == 0


def test_no_warning_when_all_pinned_keys_match(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """When the canonical chipathon26 keys all match, no warnings fire."""
    metrics = {
        "design__die__area": 100.0,
        "design__core__util": 0.5,
        "design__instance__count": 10.0,
        "route__congestion__overflow": 0.0,
    }
    parse = parse_librelane_outputs(
        _run(rc=0), metrics=metrics, def_bytes=b"# DEF\n",
    )
    relevant_warnings = [
        w for w in recwarn if "no pinned key matched" in str(w.message)
    ]
    assert relevant_warnings == [], (
        f"unexpected drift warnings: {relevant_warnings}"
    )
    assert parse.die_area_um2 == 100.0
    assert parse.utilization_pct == 50.0
    assert parse.cell_count == 10


# --------------------------------------------------------------------------- #
# F21.2-E — STA_CORNERS / STA_REPORT_POWER plumbing + per-corner harvest +
# OpenROAD #6227 segfault fallback.
# --------------------------------------------------------------------------- #
def test_config_dict_omits_sta_corners_when_unset() -> None:
    """Single-corner default: no STA_CORNERS / STA_REPORT_POWER in the JSON
    LibreLane sees, so today's path is byte-identical."""
    c = PhysicalConfig(design_name="d", top_module="t")
    cfg = build_librelane_config(c, verilog_files=["d.nl.v"])
    assert "STA_CORNERS" not in cfg
    assert "STA_REPORT_POWER" not in cfg


def test_config_dict_emits_sta_corners_when_set() -> None:
    c = PhysicalConfig(
        design_name="d", top_module="t",
        sta_corners=("tt", "ss", "ff"), sta_report_power=True,
    )
    cfg = build_librelane_config(c, verilog_files=["d.nl.v"])
    assert cfg["STA_CORNERS"] == ["tt", "ss", "ff"]  # list at the JSON seam
    assert cfg["STA_REPORT_POWER"] == 1


def test_config_dict_emits_sta_corners_without_power() -> None:
    """Corner opt-in without power: STA_CORNERS only, STA_REPORT_POWER omitted."""
    c = PhysicalConfig(
        design_name="d", top_module="t",
        sta_corners=("tt", "ss"),
    )
    cfg = build_librelane_config(c, verilog_files=["d.nl.v"])
    assert cfg["STA_CORNERS"] == ["tt", "ss"]
    assert "STA_REPORT_POWER" not in cfg


def test_detect_multi_corner_segfault_matches_openroad_6227_signatures() -> None:
    """The detector must catch the canonical OpenROAD #6227 shapes the
    issue tracker shows; conservative on purpose — false-positive is
    cheaper than false-negative."""
    assert _detect_multi_corner_segfault(
        "OpenROAD got Segmentation fault at corner ss while reading liberty"
    )
    assert _detect_multi_corner_segfault(
        "thread terminated by signal 11 during multi-corner STA"
    )
    assert _detect_multi_corner_segfault(
        "multi_corner sta crashed with SIGSEGV"
    )


def test_detect_multi_corner_segfault_ignores_unrelated_errors() -> None:
    """A generic OpenROAD error or a normal log line must NOT fire the fallback."""
    assert not _detect_multi_corner_segfault("")
    assert not _detect_multi_corner_segfault("OpenROAD: timing closed at tt corner")
    assert not _detect_multi_corner_segfault(
        "Error: route__congestion__overflow exceeded threshold"
    )


def _drop_artifacts_with_per_corner_sta(
    *,
    design_name: str,
    def_text: str,
    metrics: dict[str, float],
    corners: tuple[str, ...],
    run_tag: str = DEFAULT_FLOW_RUN_TAG,
    include_power: bool = False,
) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        final = mount / "runs" / run_tag / "final"
        (final / "def").mkdir(parents=True, exist_ok=True)
        (final / "def" / f"{design_name}.def").write_text(def_text)
        (final / "metrics.json").write_text(json.dumps(metrics))
        for corner in corners:
            corner_dir = final / "timing" / corner
            corner_dir.mkdir(parents=True, exist_ok=True)
            (corner_dir / "sta.rpt").write_text(
                f"wns {-0.1 if corner == 'ss' else 0.5}\n"
                f"tns {-0.4 if corner == 'ss' else 0.0}\n"
            )
            if include_power:
                (corner_dir / "power.rpt").write_text(
                    f"# {corner} power\nTotal {1.0 if corner == 'tt' else 1.4}\n"
                )
    return _impl


def test_harvest_populates_per_corner_timing_when_opted_in(
    store: SqliteArtifactStore,
) -> None:
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_per_corner_sta(
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
            corners=("tt", "ss", "ff"),
            include_power=True,
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(
        design_name="counter", top_module="counter",
        sta_corners=("tt", "ss", "ff"), sta_report_power=True,
    )
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_per_corner_timing is not None
    assert set(layout.librelane_per_corner_timing.keys()) == {"tt", "ss", "ff"}
    assert store.get_blob(layout.librelane_per_corner_timing["ss"]).startswith(
        b"wns -0.1"
    )
    assert layout.librelane_per_corner_power is not None
    assert set(layout.librelane_per_corner_power.keys()) == {"tt", "ss", "ff"}
    assert layout.metadata["multi_corner_fallback"] == "false"


def test_harvest_omits_per_corner_fields_when_not_opted_in(
    store: SqliteArtifactStore,
) -> None:
    """Default single-corner path: per-corner fields stay None even if
    reports exist on disk (the harvest never probes)."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts_with_per_corner_sta(
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
            corners=("tt", "ss", "ff"),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(design_name="counter", top_module="counter")  # no sta_corners
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_per_corner_timing is None
    assert layout.librelane_per_corner_power is None
    assert layout.metadata["multi_corner_fallback"] == "false"


def test_harvest_short_circuits_on_sky130a_segfault(
    store: SqliteArtifactStore,
) -> None:
    """OpenROAD #6227: when the multi-corner STA crashed, the harvest must
    skip per-corner collection AND stamp multi_corner_fallback so SIGNOFF
    dispatch can emit the audit event in Phase 7."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(
            stdout="",
            stderr="OpenROAD got Segmentation fault at corner ss while reading liberty",
        ),
        side_effect=_drop_artifacts_with_per_corner_sta(
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
            corners=("tt", "ss", "ff"),  # reports exist but must be ignored
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(
        design_name="counter", top_module="counter",
        sta_corners=("tt", "ss", "ff"),
    )
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_per_corner_timing is None
    assert layout.librelane_per_corner_power is None
    assert layout.metadata["multi_corner_fallback"] == "true"


def test_harvest_warns_when_opted_in_but_no_reports_found(
    store: SqliteArtifactStore,
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Operator asked for tt/ss/ff but the flow emitted nothing — visible
    one-line warning + clean fallback."""
    netlist = _stage_netlist(store)
    sandbox = StubSandbox(
        tool_run=_run(),
        side_effect=_drop_artifacts(  # no per-corner reports
            design_name="counter",
            def_text="# DEF\nEND DESIGN\n",
            metrics=_clean_metrics(),
        ),
    )
    svc = LibreLanePhysicalService(sandbox=sandbox, store=store)
    cfg = PhysicalConfig(
        design_name="counter", top_module="counter",
        sta_corners=("tt", "ss", "ff"),
    )
    layout, _physical = svc.run_flow(netlist, config=cfg)

    assert layout.librelane_per_corner_timing is None
    relevant = [w for w in recwarn if "no per-corner reports found" in str(w.message)]
    assert len(relevant) == 1
    msg = str(relevant[0].message)
    assert "tt" in msg and "ss" in msg and "ff" in msg


def test_multi_corner_fallback_event_type_enum_value() -> None:
    """F21.2: pin the audit-event surface so a future renaming PR can't
    silently drift the SQLite event_type column."""
    from chip_agent.obs.audit_log import EventType
    assert EventType.MULTI_CORNER_FALLBACK == "multi_corner_fallback"


# --------------------------------------------------------------------------- #
# F21.3-B — PhysicalConfig knob fields + apply_repair_delta + new config keys
# emitted only when set.
# --------------------------------------------------------------------------- #
def test_physical_config_pl_target_density_default_is_none() -> None:
    """Default path: pl_target_density / synth_strategy stay None →
    PL_TARGET_DENSITY / SYNTH_STRATEGY absent from the JSON LibreLane
    sees, so today's path is byte-identical."""
    c = PhysicalConfig(design_name="d", top_module="t")
    assert c.pl_target_density is None
    assert c.synth_strategy is None


def test_config_dict_omits_pl_target_density_and_synth_strategy_when_unset() -> None:
    c = PhysicalConfig(design_name="d", top_module="t")
    cfg = build_librelane_config(c, verilog_files=["d.nl.v"])
    assert "PL_TARGET_DENSITY" not in cfg
    assert "SYNTH_STRATEGY" not in cfg


def test_config_dict_emits_pl_target_density_when_set() -> None:
    c = PhysicalConfig(
        design_name="d", top_module="t", pl_target_density=0.42,
    )
    cfg = build_librelane_config(c, verilog_files=["d.nl.v"])
    assert cfg["PL_TARGET_DENSITY"] == 0.42


def test_config_dict_emits_synth_strategy_when_set() -> None:
    c = PhysicalConfig(
        design_name="d", top_module="t", synth_strategy="DELAY 3",
    )
    cfg = build_librelane_config(c, verilog_files=["d.nl.v"])
    assert cfg["SYNTH_STRATEGY"] == "DELAY 3"


def test_physical_config_retuned_preserves_repair_knobs() -> None:
    """Inner-loop retune (congestion path) must NOT drop the F21.3
    knobs — a multi-attempt run where the agent first picks LOWER_DENSITY
    and the driver then retunes for congestion needs both effects to
    stack on the next attempt."""
    c = PhysicalConfig(
        design_name="d", top_module="t",
        pl_target_density=0.42, synth_strategy="DELAY 3",
    )
    again = c.retuned()
    assert again.pl_target_density == 0.42
    assert again.synth_strategy == "DELAY 3"


def test_from_constraints_threads_repair_knobs() -> None:
    c = from_constraints(
        DesignConstraints(), design_name="d", top_module="t",
        pl_target_density=0.42, synth_strategy="AREA 2",
    )
    assert c.pl_target_density == 0.42
    assert c.synth_strategy == "AREA 2"


# --------------------------------------------------------------------------- #
# apply_repair_delta — pure function over (config, route) → new config.
# --------------------------------------------------------------------------- #
def test_apply_repair_delta_lower_density_from_default() -> None:
    """First LOWER_DENSITY: starts from target_utilization (0.5 default)
    since pl_target_density is None. Subtracts 0.05 → 0.45."""
    c = PhysicalConfig(
        design_name="d", top_module="t", target_utilization=0.5,
    )
    r = PhysicalRepairRoute(kind=PhysicalRepairRouteKind.LOWER_DENSITY)
    out = apply_repair_delta(c, r)
    assert out.pl_target_density == 0.45
    # Original config unchanged — pure function.
    assert c.pl_target_density is None


def test_apply_repair_delta_lower_density_clamps_at_floor() -> None:
    """Repeated LOWER_DENSITY applications clamp at the 0.30 floor so a
    runaway dispatcher can't push the design unplaceable."""
    c = PhysicalConfig(
        design_name="d", top_module="t", target_utilization=0.5,
    )
    r = PhysicalRepairRoute(kind=PhysicalRepairRouteKind.LOWER_DENSITY)
    # 0.5 → 0.45 → 0.40 → 0.35 → 0.30 → 0.30 (clamped)
    for _ in range(10):
        c = apply_repair_delta(c, r)
    assert c.pl_target_density == 0.30


def test_apply_repair_delta_lower_density_continues_from_current() -> None:
    """When pl_target_density is already set, LOWER_DENSITY operates on
    THAT value, not on target_utilization. Matches the dispatcher's
    expectation that history is reapplied in order."""
    c = PhysicalConfig(
        design_name="d", top_module="t",
        target_utilization=0.5, pl_target_density=0.42,
    )
    r = PhysicalRepairRoute(kind=PhysicalRepairRouteKind.LOWER_DENSITY)
    out = apply_repair_delta(c, r)
    assert out.pl_target_density == 0.37  # 0.42 - 0.05


def test_apply_repair_delta_increase_delay_optimization() -> None:
    c = PhysicalConfig(design_name="d", top_module="t")
    r = PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.INCREASE_DELAY_OPTIMIZATION,
    )
    out = apply_repair_delta(c, r)
    assert out.synth_strategy == "DELAY 3"


def test_apply_repair_delta_relax_clock_period_scales_by_factor() -> None:
    """1.10× the current clock_period_ns — the standard ORFS first-relaxation."""
    c = PhysicalConfig(
        design_name="d", top_module="t", clock_period_ns=10.0,
    )
    r = PhysicalRepairRoute(kind=PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD)
    out = apply_repair_delta(c, r)
    assert out.clock_period_ns == 11.0


def test_apply_repair_delta_relax_clock_period_noop_when_no_clock() -> None:
    """Combinational designs (no clock_period_ns) → identity. Defensive
    against the agent picking this route for an inappropriate design."""
    c = PhysicalConfig(design_name="d", top_module="t")  # no clock
    r = PhysicalRepairRoute(kind=PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD)
    out = apply_repair_delta(c, r)
    assert out is c  # identity-when-noop preserves caller equality checks


def test_apply_repair_delta_escalate_human_is_identity() -> None:
    """ESCALATE_HUMAN as a delta is a no-op — the dispatcher routes to
    the human gate without ever calling this function in practice; the
    identity transform here keeps the PHYSICAL node's reapply loop
    branch-free."""
    c = PhysicalConfig(
        design_name="d", top_module="t", clock_period_ns=10.0,
        pl_target_density=0.42, synth_strategy="AREA 2",
    )
    r = PhysicalRepairRoute(kind=PhysicalRepairRouteKind.ESCALATE_HUMAN)
    out = apply_repair_delta(c, r)
    assert out is c


def test_apply_repair_delta_chains_routes_in_history_order() -> None:
    """Two routes applied in order produce the expected stacked effect —
    the invariant the PHYSICAL node relies on when reapplying the route
    history at entry."""
    c = PhysicalConfig(
        design_name="d", top_module="t",
        clock_period_ns=10.0, target_utilization=0.5,
    )
    history = [
        PhysicalRepairRoute(kind=PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD),
        PhysicalRepairRoute(kind=PhysicalRepairRouteKind.LOWER_DENSITY),
    ]
    for r in history:
        c = apply_repair_delta(c, r)
    assert c.clock_period_ns == 11.0
    assert c.pl_target_density == 0.45


def test_apply_repair_delta_preserves_other_fields() -> None:
    """Single-knob deltas must leave the other fields untouched."""
    c = PhysicalConfig(
        design_name="d", top_module="t",
        clock_period_ns=10.0, target_utilization=0.5,
        sta_corners=("tt", "ss", "ff"), sta_report_power=True,
        pdk="sky130A", std_cell_lib="sky130_fd_sc_hd",
    )
    r = PhysicalRepairRoute(kind=PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD)
    out = apply_repair_delta(c, r)
    assert out.sta_corners == ("tt", "ss", "ff")
    assert out.sta_report_power is True
    assert out.target_utilization == 0.5
    assert out.pdk == "sky130A"
    assert out.std_cell_lib == "sky130_fd_sc_hd"
