"""F6.3 — OpenSTA service. Parser + runner over a stub sandbox.

Covers the AC's timing leg: a clean log passes; a log with negative WNS
yields ``wns_ns < 0`` on the :class:`TimingReport` and closes the gate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import (
    NetlistArtifact,
    Provenance,
    Stage,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.opensta import (
    OPENSTA_BIN,
    OpenSTAService,
    STAParse,
    build_sta_script,
    default_sdc,
    parse_multi_corner_sta,
    parse_sta_output,
)


def _run(*, rc: int = 0, stdout: str = "", stderr: str = "") -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.1,
    )


_CLEAN_LOG = (
    "Reading liberty file...\n"
    "wns 0.42\n"
    "tns 0.0\n"
    "Setup violations: 0\n"
    "Hold violations: 0\n"
)


# --------------------------------------------------------------------------- #
# default_sdc / build_sta_script
# --------------------------------------------------------------------------- #
def test_default_sdc_emits_create_clock() -> None:
    sdc = default_sdc(clock_period_ns=5.0, clock_port="clk")
    assert "create_clock" in sdc
    assert "5" in sdc
    assert "[get_ports clk]" in sdc


def test_sta_script_loads_design_in_order() -> None:
    script = build_sta_script(
        netlist_file="design.nl.v",
        sdc_file="constraints.sdc",
        liberty_file="/foss/pdks/sky130A/...lib",
        top_module="counter",
    )
    assert script.index("read_liberty") < script.index("read_verilog")
    assert script.index("read_verilog") < script.index("link_design counter")
    assert "read_sdc constraints.sdc" in script
    assert "report_wns" in script
    assert "report_tns" in script


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_clean_run_passes() -> None:
    p = parse_sta_output(_run(stdout=_CLEAN_LOG))
    assert isinstance(p, STAParse)
    assert p.passed
    assert p.wns_ns == 0.42
    assert p.tns_ns == 0.0
    assert p.setup_violations == 0
    assert p.hold_violations == 0
    assert p.violations == []


def test_negative_wns_closes_gate() -> None:
    # AC: a timing-failing design yields a TimingReport with wns_ns < 0 and
    # blocks the gate. Asserted here at the parser level; the service-level
    # AC test below confirms the report shape.
    log = (
        "wns -0.85\n"
        "tns -3.2\n"
        "Setup violations: 0\n"
    )
    p = parse_sta_output(_run(stdout=log))
    assert not p.passed
    assert p.wns_ns == -0.85
    assert p.tns_ns == -3.2
    # Even when OpenSTA didn't print a Setup violations line, we promote the
    # negative slack to a typed violation so callers don't have to special-case.
    assert p.setup_violations == 1
    assert any(v.code == "STA.SETUP_VIOLATION" for v in p.violations)


def test_explicit_setup_count_propagates() -> None:
    log = (
        "wns 0.1\n"
        "tns 0.0\n"
        "Setup violations: 4\n"
        "Hold violations: 0\n"
    )
    p = parse_sta_output(_run(stdout=log))
    assert not p.passed
    assert p.setup_violations == 4
    assert any(v.code == "STA.SETUP_VIOLATION" for v in p.violations)


def test_hold_violation_closes_gate() -> None:
    log = (
        "wns 0.05\n"
        "tns 0.0\n"
        "Setup violations: 0\n"
        "Hold violations: 2\n"
    )
    p = parse_sta_output(_run(stdout=log))
    assert not p.passed
    assert p.hold_violations == 2
    assert any(v.code == "STA.HOLD_VIOLATION" for v in p.violations)


def test_violated_path_summary_counts() -> None:
    log = (
        "report_checks ...\n"
        "VIOLATED  setup  reg_q  -0.250\n"
        "wns -0.25\n"
        "tns -0.25\n"
    )
    p = parse_sta_output(_run(stdout=log))
    assert not p.passed
    assert p.setup_violations >= 1


def test_nonzero_returncode_fails_even_with_zero_slack() -> None:
    p = parse_sta_output(_run(stdout=_CLEAN_LOG, rc=1))
    assert not p.passed


def test_error_line_surfaces_as_violation() -> None:
    log = "Error: cannot find /foss/pdks/.../lib\n"
    p = parse_sta_output(_run(stderr=log, rc=1))
    assert any(v.code == "STA.ERROR" for v in p.violations)


# --------------------------------------------------------------------------- #
# Runner wiring
# --------------------------------------------------------------------------- #
@dataclass
class StubSandbox:
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


def _netlist(store: SqliteArtifactStore, *, top: str = "counter") -> NetlistArtifact:
    blob = store.put_blob(b"// gate-level\n", media_type="text/x-verilog")
    art = NetlistArtifact(
        artifact_id=f"d0.{top}.netlist",
        design_id="d0", module_id=top,
        netlist=blob, std_cell_lib="sky130_fd_sc_hd",
        cell_count=10,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_runner_command_shape_and_staging(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(tool_run=_run(stdout=_CLEAN_LOG))
    svc = OpenSTAService(sandbox=sandbox, store=store)
    netlist = _netlist(store)

    report = svc.check_timing(netlist, clock_period_ns=5.0)
    assert report.passed
    assert report.gate_ok
    assert report.wns_ns == 0.42
    assert report.tns_ns == 0.0
    assert report.provenance.inputs == [netlist.ref()]

    cmd, _mount, staged = sandbox.calls[0]
    assert cmd[0] == OPENSTA_BIN
    assert cmd[-1] == "timing.tcl"
    assert "design.nl.v" in staged
    assert "constraints.sdc" in staged
    assert "timing.tcl" in staged


def test_runner_surfaces_negative_wns(store: SqliteArtifactStore) -> None:
    # AC: timing-failing design -> TimingReport with wns_ns < 0 blocks the gate.
    log = (
        "wns -0.85\n"
        "tns -3.2\n"
    )
    sandbox = StubSandbox(tool_run=_run(stdout=log))
    svc = OpenSTAService(sandbox=sandbox, store=store)
    netlist = _netlist(store)
    report = svc.check_timing(netlist, clock_period_ns=2.0)
    assert report.wns_ns == -0.85
    assert not report.gate_ok
    assert not report.passed
    assert any(v.code == "STA.SETUP_VIOLATION" for v in report.violations)


def test_runner_accepts_custom_sdc(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(tool_run=_run(stdout=_CLEAN_LOG))
    svc = OpenSTAService(sandbox=sandbox, store=store)
    netlist = _netlist(store)
    custom = "# my SDC\ncreate_clock -name main_clk -period 8 [get_ports main_clk]\n"
    svc.check_timing(netlist, clock_period_ns=8.0, sdc_text=custom)

    _cmd, _mount, staged = sandbox.calls[0]
    assert staged["constraints.sdc"].decode() == custom


# --------------------------------------------------------------------------- #
# F12.1 — SDF support: when SIGNOFF supplies a LibreLane CTS SDF, the TCL
# pass prepends ``read_sdf`` and the report flags ``sdf_used=1.0``.
# --------------------------------------------------------------------------- #
def test_sta_script_omits_read_sdf_when_no_sdf() -> None:
    script = build_sta_script(
        netlist_file="design.nl.v", sdc_file="constraints.sdc",
        liberty_file="/foss/lib", top_module="counter",
    )
    assert "read_sdf" not in script


def test_sta_script_emits_read_sdf_when_sdf_path_supplied() -> None:
    script = build_sta_script(
        netlist_file="design.nl.v", sdc_file="constraints.sdc",
        liberty_file="/foss/lib", top_module="counter",
        sdf_file="design.sdf",
    )
    assert "read_sdf design.sdf" in script
    # SDF must come AFTER read_sdc (annotations on top of the constraints).
    assert script.index("read_sdc") < script.index("read_sdf")
    # SDF must come BEFORE report_checks so the path delays reflect it.
    assert script.index("read_sdf") < script.index("report_checks")


def test_runner_stages_sdf_when_bytes_supplied(
    store: SqliteArtifactStore,
) -> None:
    sandbox = StubSandbox(tool_run=_run(stdout=_CLEAN_LOG))
    svc = OpenSTAService(sandbox=sandbox, store=store)
    netlist = _netlist(store)
    sdf_payload = b"(DELAYFILE\n  (SDFVERSION \"3.0\")\n)\n"

    report = svc.check_timing(
        netlist, clock_period_ns=5.0, sdf_bytes=sdf_payload,
    )

    _cmd, _mount, staged = sandbox.calls[0]
    assert "design.sdf" in staged
    assert staged["design.sdf"] == sdf_payload
    assert "read_sdf design.sdf" in staged["timing.tcl"].decode()
    # F12.1 contract: the report flags whether SDF was used so callers can
    # distinguish post-CTS truth from pre-layout estimate-only.
    assert report.metrics["sdf_used"] == 1.0


def test_runner_without_sdf_records_sdf_used_zero(
    store: SqliteArtifactStore,
) -> None:
    sandbox = StubSandbox(tool_run=_run(stdout=_CLEAN_LOG))
    svc = OpenSTAService(sandbox=sandbox, store=store)
    netlist = _netlist(store)

    report = svc.check_timing(netlist, clock_period_ns=5.0)

    _cmd, _mount, staged = sandbox.calls[0]
    assert "design.sdf" not in staged
    assert "read_sdf" not in staged["timing.tcl"].decode()
    assert report.metrics["sdf_used"] == 0.0


def test_runner_with_empty_sdf_bytes_does_not_stage(
    store: SqliteArtifactStore,
) -> None:
    """Empty bytes are treated as "no SDF" — same as ``None``."""
    sandbox = StubSandbox(tool_run=_run(stdout=_CLEAN_LOG))
    svc = OpenSTAService(sandbox=sandbox, store=store)
    netlist = _netlist(store)

    report = svc.check_timing(netlist, clock_period_ns=5.0, sdf_bytes=b"")

    _cmd, _mount, staged = sandbox.calls[0]
    assert "design.sdf" not in staged
    assert report.metrics["sdf_used"] == 0.0


# --------------------------------------------------------------------------- #
# F13.1 — netlist_bytes_override: when SIGNOFF feeds the LibreLane mapped
# netlist, STA stages THOSE bytes instead of the NetlistArtifact's blob.
# --------------------------------------------------------------------------- #
def test_runner_uses_override_bytes_when_supplied(
    store: SqliteArtifactStore,
) -> None:
    sandbox = StubSandbox(tool_run=_run(stdout=_CLEAN_LOG))
    svc = OpenSTAService(sandbox=sandbox, store=store)
    netlist = _netlist(store)
    mapped = b"module counter; sky130_fd_sc_hd__dfrtp_2 dff_0 (...); endmodule\n"

    report = svc.check_timing(
        netlist, clock_period_ns=5.0, netlist_bytes_override=mapped,
    )

    _cmd, _mount, staged = sandbox.calls[0]
    assert staged["design.nl.v"] == mapped
    # The NetlistArtifact's own blob was NOT staged.
    assert staged["design.nl.v"] != store.get_blob(netlist.netlist)
    # F13.1 metric: callers can see which input the gate ran against.
    assert report.metrics["used_mapped_netlist"] == 1.0


def test_runner_without_override_records_used_mapped_zero(
    store: SqliteArtifactStore,
) -> None:
    sandbox = StubSandbox(tool_run=_run(stdout=_CLEAN_LOG))
    svc = OpenSTAService(sandbox=sandbox, store=store)
    netlist = _netlist(store)

    report = svc.check_timing(netlist, clock_period_ns=5.0)

    _cmd, _mount, staged = sandbox.calls[0]
    # NetlistArtifact's blob was staged — pre-F13.1 path.
    assert staged["design.nl.v"] == store.get_blob(netlist.netlist)
    assert report.metrics["used_mapped_netlist"] == 0.0


# --------------------------------------------------------------------------- #
# F13.3 — read_verilog errors surface as a typed STA.READ_VERILOG_ERROR
# violation with file + line in detail, NOT the generic STA.ERROR. The
# distinct code lets the operator filter on it for triage.
# --------------------------------------------------------------------------- #
def test_read_verilog_syntax_error_surfaces_as_typed_violation() -> None:
    """The F11.7 live shape: ``Error: 171 design.nl.v line 18, syntax error``."""
    log = "Error: 171 design.nl.v line 18, syntax error\n"
    parse = parse_sta_output(_run(stderr=log, rc=1))
    by_code = {v.code: v for v in parse.violations}
    assert "STA.READ_VERILOG_ERROR" in by_code, (
        f"expected STA.READ_VERILOG_ERROR, got codes: {sorted(by_code)}"
    )
    v = by_code["STA.READ_VERILOG_ERROR"]
    assert v.severity == "error"
    assert v.detail["file"] == "design.nl.v"
    assert v.detail["line"] == 18
    assert "syntax error" in v.detail["reason"].lower()
    # The location field gives the operator a one-string pointer for logs.
    assert v.location == "design.nl.v:18"
    # The generic STA.ERROR is NOT also emitted for the same line.
    assert "STA.ERROR" not in by_code


def test_read_verilog_error_closes_gate_even_when_slack_parses_zero() -> None:
    """A `read_verilog` failure means STA never reached timing analysis.
    If the noisy output happens to contain a stray ``wns 0`` line, the
    gate must STILL close because the typed error-severity violation is
    present.
    """
    log = (
        "Error: 171 design.nl.v line 18, syntax error\n"
        "wns 0\n"  # noise — would otherwise look like clean timing
        "tns 0\n"
    )
    parse = parse_sta_output(_run(stderr=log, rc=1))
    assert not parse.passed
    assert any(
        v.code == "STA.READ_VERILOG_ERROR" for v in parse.violations
    )


def test_read_verilog_error_carries_systemverilog_extension() -> None:
    """SystemVerilog .sv netlists trigger the same surfacing."""
    log = "Error: alu.sv line 42, syntax error near always_comb\n"
    parse = parse_sta_output(_run(stderr=log, rc=1))
    v = next(v for v in parse.violations if v.code == "STA.READ_VERILOG_ERROR")
    assert v.detail["file"] == "alu.sv"
    assert v.detail["line"] == 42


def test_generic_error_still_uses_sta_error_for_non_read_verilog() -> None:
    """An OpenSTA error that doesn't match the read_verilog shape (e.g.
    a missing liberty file) keeps using the generic STA.ERROR code."""
    log = "Error: cannot find /foss/pdks/sky130A/libs.ref/missing.lib\n"
    parse = parse_sta_output(_run(stderr=log, rc=1))
    codes = {v.code for v in parse.violations}
    assert "STA.ERROR" in codes
    assert "STA.READ_VERILOG_ERROR" not in codes


# --------------------------------------------------------------------------- #
# F21.2-F — parse_multi_corner_sta.
#
# Pure parser over per-corner OpenSTA stdout blobs (harvested by Phase 5
# from runs/<tag>/final/timing/<corner>/sta.rpt). No subprocess. Reuses
# the existing line-matchers; tags every violation with the corner so
# F21.3's timing-closure agent can attribute failures.
# --------------------------------------------------------------------------- #
_TT_CLEAN = b"wns 0.5\ntns 0.0\nSetup violations: 0\nHold violations: 0\n"
_SS_NEGATIVE_WNS = b"wns -0.2\ntns -0.4\nSetup violations: 2\nHold violations: 0\n"
_FF_HOLD_VIOLATION = b"wns 1.1\ntns 0.0\nSetup violations: 0\nHold violations: 1\n"


def test_multi_corner_parser_three_clean_corners_passes() -> None:
    report = parse_multi_corner_sta(
        {"tt": _TT_CLEAN, "ss": b"wns 0.1\ntns 0.0\n", "ff": b"wns 1.1\ntns 0.0\n"},
        design_id="d0",
        artifact_id="d0.top.multi_sta",
    )
    assert report.passed
    assert report.gate_ok
    assert report.worst_wns_ns == 0.1
    assert [c.corner for c in report.corners] == ["ff", "ss", "tt"]  # sorted


def test_multi_corner_parser_negative_wns_at_ss_closes_gate() -> None:
    """Per AC: worst-corner WNS feeds the gate; one bad corner is enough."""
    report = parse_multi_corner_sta(
        {"tt": _TT_CLEAN, "ss": _SS_NEGATIVE_WNS, "ff": _FF_HOLD_VIOLATION},
        design_id="d0",
        artifact_id="d0.top.multi_sta",
    )
    assert not report.passed
    assert not report.gate_ok
    assert report.worst_wns_ns == -0.2
    assert report.worst_tns_ns == -0.4
    # Setup violation tagged with the failing corner; no other corner
    # contributes a STA.SETUP_VIOLATION because tt/ff parse cleanly.
    setups = [v for v in report.violations if v.code == "STA.SETUP_VIOLATION"]
    assert all(v.location == "corner=ss" for v in setups)
    assert any(v.detail["corner"] == "ss" for v in setups)


def test_multi_corner_parser_aggregates_metrics_top_level() -> None:
    """F21.3 will read worst_wns_ns from metrics directly without walking
    the corners list — pin the surface."""
    report = parse_multi_corner_sta(
        {"tt": _TT_CLEAN, "ss": _SS_NEGATIVE_WNS},
        design_id="d0",
        artifact_id="d0.top.multi_sta",
    )
    assert report.metrics["worst_wns_ns"] == -0.2
    assert report.metrics["worst_tns_ns"] == -0.4
    assert report.metrics["corners_checked"] == 2.0


def test_multi_corner_parser_tags_read_verilog_error_with_corner() -> None:
    """F13.3-equivalent per corner — location string carries the corner
    so the operator + F21.3 can grep for it."""
    bad = b"Error: 171 design.nl.v line 18, syntax error\n"
    report = parse_multi_corner_sta(
        {"tt": _TT_CLEAN, "ss": bad},
        design_id="d0",
        artifact_id="d0.top.multi_sta",
    )
    v = next(v for v in report.violations if v.code == "STA.READ_VERILOG_ERROR")
    assert v.detail["corner"] == "ss"
    assert v.detail["file"] == "design.nl.v"
    assert v.detail["line"] == 18
    assert v.location == "corner=ss:design.nl.v:18"
    assert not report.gate_ok


def test_multi_corner_parser_picks_up_power_metrics_when_supplied() -> None:
    """When power blobs are present, the corresponding CornerTiming
    carries the parsed mW values."""
    power = b"# tt power\nleakage 0.12\nswitching 0.34\ninternal 0.21\ntotal 0.67\n"
    report = parse_multi_corner_sta(
        {"tt": _TT_CLEAN},
        {"tt": power},
        design_id="d0",
        artifact_id="d0.top.multi_sta",
    )
    pm = report.corners[0].power_metrics
    assert pm == {
        "leakage": 0.12,
        "switching": 0.34,
        "internal": 0.21,
        "total": 0.67,
    }


def test_multi_corner_parser_no_power_keeps_corners_clean() -> None:
    """power_reports=None: every corner's power_metrics is empty."""
    report = parse_multi_corner_sta(
        {"tt": _TT_CLEAN, "ss": _TT_CLEAN},
        design_id="d0", artifact_id="d0.top.multi_sta",
    )
    assert all(c.power_metrics == {} for c in report.corners)


def test_multi_corner_parser_offshape_power_gracefully_returns_empty() -> None:
    """An off-shape power blob (no recognisable keys) leaves power_metrics
    empty rather than crashing the parser. Defensive — future LibreLane
    versions might change the report format."""
    bogus_power = b"garbage line 1\nanother garbage line\n"
    report = parse_multi_corner_sta(
        {"tt": _TT_CLEAN},
        {"tt": bogus_power},
        design_id="d0", artifact_id="d0.top.multi_sta",
    )
    assert report.corners[0].power_metrics == {}


def test_multi_corner_parser_deterministic_corner_order() -> None:
    """Content hash stability relies on a deterministic corner order;
    insertion-order dict-iteration drift between Python runs would break
    golden-manifest reproducibility."""
    report_a = parse_multi_corner_sta(
        {"ff": _TT_CLEAN, "tt": _TT_CLEAN, "ss": _TT_CLEAN},
        design_id="d0", artifact_id="d0.top.multi_sta",
    )
    report_b = parse_multi_corner_sta(
        {"tt": _TT_CLEAN, "ss": _TT_CLEAN, "ff": _TT_CLEAN},
        design_id="d0", artifact_id="d0.top.multi_sta",
    )
    assert [c.corner for c in report_a.corners] == ["ff", "ss", "tt"]
    assert (
        report_a.compute_content_hash() == report_b.compute_content_hash()
    )


def test_multi_corner_parser_empty_input_produces_empty_report() -> None:
    """No corners harvested at all → empty list, no violations, passed.
    The SIGNOFF dispatch never builds this artifact in that case (it
    falls back to TimingReport), but the parser stays defensive."""
    report = parse_multi_corner_sta(
        {}, design_id="d0", artifact_id="d0.top.multi_sta",
    )
    assert report.corners == []
    assert report.passed
    assert report.gate_ok
    assert report.worst_wns_ns is None
