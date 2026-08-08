"""F6.1 acceptance: Yosys synth service produces ``(NetlistArtifact, SynthesisReport)``;
inferred-latch and blackbox warnings surface as :class:`Violation`s on the report.

Parser tests feed canned Yosys logs (no Docker); the service test injects a
stub sandbox that writes the netlist file on call so the assertions exercise
the staging / artifact wiring end-to-end.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import (
    ArtifactKind,
    Provenance,
    RTLArtifact,
    Stage,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.yosys import (
    YOSYS_BIN,
    YosysParse,
    YosysSynthError,
    YosysSynthService,
    build_synth_script,
    parse_yosys_output,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run(stdout: str = "", stderr: str = "", *, rc: int = 0) -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.1,
    )


_CLEAN_STAT = (
    "2.21. Printing statistics.\n"
    "\n"
    "=== counter ===\n"
    "\n"
    "Number of wires:                  12\n"
    "Number of wire bits:              40\n"
    "Number of public wires:           12\n"
    "Number of cells:                  17\n"
    "  sky130_fd_sc_hd__dfxtp_2          8\n"
    "  sky130_fd_sc_hd__nand2_1          9\n"
)


# --------------------------------------------------------------------------- #
# build_synth_script
# --------------------------------------------------------------------------- #
def test_synth_script_uses_sv_reader_for_verilog_too() -> None:
    """Verilog-2001 source still reads in ``-sv`` mode.

    SV mode is a strict superset of V-2001 — V-2001 code parses unchanged —
    and forcing always-``-sv`` avoids the LLM-generated-RTL trap where
    ``always_ff`` etc. in a module labelled ``language="verilog"`` is
    silently dropped, producing a zero-cell netlist that's painful to triage.
    """
    s = build_synth_script(
        source_file="counter.v", top_module="counter",
        netlist_file="netlist.v", language="verilog",
    )
    assert "read_verilog -sv counter.v" in s
    assert "hierarchy -check -top counter" in s
    assert "synth -top counter" in s
    assert "stat" in s
    assert "write_verilog -noattr netlist.v" in s


def test_synth_script_systemverilog_uses_sv_flag() -> None:
    s = build_synth_script(
        source_file="alu.sv", top_module="alu",
        netlist_file="netlist.v", language="systemverilog",
    )
    assert "read_verilog -sv alu.sv" in s


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_clean_synth_passes() -> None:
    p = parse_yosys_output(_run(stdout=_CLEAN_STAT))
    assert isinstance(p, YosysParse)
    assert p.passed
    assert p.violations == []
    assert p.cell_count == 17
    assert p.inferred_latches == 0
    assert p.blackbox_modules == []
    assert p.metrics["cells"] == 17.0
    assert p.metrics["wires"] == 12.0


def test_inferred_latch_surfaces_as_violation() -> None:
    # AC: inferred-latch warning -> Violation with code LATCH_INFERRED, severity=error.
    log = (
        "2.6. Executing PROC_DLATCH pass (convert process syncs to latches).\n"
        "Warning: Latch inferred for signal `\\q' from process `proc_2'.\n"
    ) + _CLEAN_STAT
    p = parse_yosys_output(_run(stdout=log))
    latch_v = [v for v in p.violations if v.code == "LATCH_INFERRED"]
    assert len(latch_v) == 1
    assert latch_v[0].severity == "error"
    assert latch_v[0].location == "q"  # leading "\" stripped
    assert "q" in latch_v[0].detail["signal"]
    assert p.inferred_latches == 1
    assert not p.passed  # error-severity violation closes the gate
    assert not _gate_ok(p)


def test_multiple_inferred_latches_counted() -> None:
    log = (
        "Warning: Latch inferred for signal `\\foo' from process `p1'.\n"
        "Warning: Latch inferred for signal `\\bar' from process `p2'.\n"
    ) + _CLEAN_STAT
    p = parse_yosys_output(_run(stdout=log))
    assert p.inferred_latches == 2
    assert sum(1 for v in p.violations if v.code == "LATCH_INFERRED") == 2


def test_blackbox_surfaces_as_violation() -> None:
    # AC: blackbox warning -> Violation with code BLACKBOX_MODULE, severity=error.
    log = (
        "2.3. Executing HIERARCHY pass (managing design hierarchy).\n"
        "Warning: Module `\\my_ram' is blackbox; instantiations will be opaque.\n"
    ) + _CLEAN_STAT
    p = parse_yosys_output(_run(stdout=log))
    bb_v = [v for v in p.violations if v.code == "BLACKBOX_MODULE"]
    assert len(bb_v) == 1
    assert bb_v[0].severity == "error"
    assert "my_ram" in bb_v[0].message
    assert p.blackbox_modules == ["my_ram"]
    assert not p.passed


def test_blackbox_dedupes_module_names() -> None:
    log = (
        "Warning: Module `\\ram' is blackbox.\n"
        "Warning: Module `\\ram' is blackbox.\n"
    ) + _CLEAN_STAT
    p = parse_yosys_output(_run(stdout=log))
    assert p.blackbox_modules == ["ram"]  # deduped
    # Each occurrence still emits a Violation (so location/raw_line provenance survives).
    assert sum(1 for v in p.violations if v.code == "BLACKBOX_MODULE") == 2


def test_error_line_surfaces_as_violation() -> None:
    log = "ERROR: Module `\\unknown' referenced in `top' but not defined.\n"
    p = parse_yosys_output(_run(stderr=log, rc=1))
    assert not p.passed
    assert any(v.code == "SYNTH.ERROR" and v.severity == "error"
               for v in p.violations)


def test_generic_warning_lands_as_warning_violation() -> None:
    # Non-classified warnings still surface — at advisory severity.
    log = "Warning: Couldn't infer a multiplier for X.\n" + _CLEAN_STAT
    p = parse_yosys_output(_run(stdout=log))
    advisories = [v for v in p.violations if v.code == "SYNTH.WARNING"]
    assert len(advisories) == 1
    assert advisories[0].severity == "warning"
    # Advisory-only warnings do NOT close the gate.
    assert p.passed


def test_nonzero_returncode_fails_even_without_error_lines() -> None:
    # Yosys can die without writing a typed error (e.g. crash on bad input).
    p = parse_yosys_output(_run(stdout=_CLEAN_STAT, rc=1))
    assert not p.passed


def test_zero_cell_synth_fails_gate_even_with_clean_rc() -> None:
    """A run that returns rc=0 with no error lines but reports zero cells
    must NOT pass the synth gate. Real designs always synthesise to at
    least their register cells; a zero-cell result is the synth output
    being silently swallowed (e.g. ``yosys -q`` suppressing ``stat``).
    Without this guard the spine promotes an unmapped netlist that the
    physical stage can't place — observed live as a 2-second LibreLane
    run that produced an empty DEF and escalated to HUMAN."""
    # No "Number of cells: N" line at all; just the standard ``stat``
    # preamble with no counts (the exact shape ``yosys -q stat`` emits).
    log = "2.21. Printing statistics.\n\n=== counter ===\n\n"
    p = parse_yosys_output(_run(stdout=log, rc=0))
    assert p.violations == []          # no error/warning lines either
    assert p.cell_count == 0
    assert p.metrics["cells"] == 0.0
    assert not p.passed                # the new guard
    assert not _gate_ok(p)


def test_simulated_yosys_q_suppressed_log_fails_gate() -> None:
    """Pin the verbatim shape produced when Yosys was invoked with ``-q``
    and the script still ran ``stat`` + ``write_verilog``: stat output is
    suppressed entirely, write_verilog emits no info line, and the parser
    sees a near-empty stdout. The pre-fix code would have set passed=True;
    the post-fix code must fail the gate so the inner loop reacts instead
    of forwarding a 0-cell netlist to PHYSICAL."""
    # ``yosys -q`` silences INFO/log output; only the script's terminating
    # blank line plus an empty stdout buffer reach the parser.
    p = parse_yosys_output(_run(stdout="\n", rc=0))
    assert p.cell_count == 0
    assert not p.passed


def test_modern_yosys_stat_format_is_parsed() -> None:
    """Yosys ≥0.4x rewrote ``stat`` to a count-first, label-after layout
    with no ``Number of …:`` prefix. Verbatim output from the pinned
    IIC-OSIC-TOOLS image (Yosys 0.64) on a tiny 8-bit counter:

        +----------Local Count, excluding submodules.
              22 cells
              11 wires
              ...

    Without this regex the parser saw zero cells, the new ``cell_count > 0``
    guard correctly failed the synth gate, and the live run on
    ``runs/.../counter-20260615T203544-072b0b.synth_report`` escalated to
    HUMAN with metrics={cells:0, wires:0, errors:0, violations:0}."""
    log = (
        "3.25. Printing statistics.\n"
        "\n"
        "=== c ===\n"
        "\n"
        "        +----------Local Count, excluding submodules.\n"
        "        | \n"
        "       11 wires\n"
        "       32 wire bits\n"
        "        3 public wires\n"
        "       10 public wire bits\n"
        "        3 ports\n"
        "       10 port bits\n"
        "       22 cells\n"
        "        5   $_AND_\n"
        "        1   $_NAND_\n"
        "        1   $_NOT_\n"
        "        8   $_SDFF_PP0_\n"
        "        1   $_XNOR_\n"
        "        6   $_XOR_\n"
    )
    p = parse_yosys_output(_run(stdout=log, rc=0))
    assert p.cell_count == 22
    assert p.metrics["cells"] == 22.0
    assert p.metrics["wires"] == 11.0
    # The "32 wire bits" / "3 public wires" auxiliary lines must NOT clobber
    # the wire count — the regex is anchored to ``^<N> wires$`` exactly.
    assert p.metrics["wires"] != 32.0
    assert p.metrics["wires"] != 3.0
    # And the per-cell-type breakdown lines (e.g. ``5   $_AND_``) must NOT
    # be misread as cell counts.
    assert p.cell_count != 5
    assert p.passed
    assert _gate_ok(p)


def test_modern_stat_does_not_match_cell_type_breakdown() -> None:
    """Defensive: a stray ``5   cells`` line — say from a future Yosys
    version emitting per-bucket counts — must still be picked up if it
    matches the strict shape. Conversely, the per-type breakdown rows
    like ``5   $_AND_`` use a non-word second token and must not match."""
    # Direct probe of the regex via a synthetic single-line log.
    assert parse_yosys_output(
        _run(stdout="5   $_AND_\n", rc=0)
    ).cell_count == 0
    # And the count-first total IS picked up.
    assert parse_yosys_output(
        _run(stdout="22 cells\n", rc=0)
    ).cell_count == 22


def test_clean_synth_still_passes_with_real_stat_output() -> None:
    """Sanity check: dropping ``-q`` means the parser now sees the
    full stat block. With cells=17, the synth gate must still pass."""
    p = parse_yosys_output(_run(stdout=_CLEAN_STAT, rc=0))
    assert p.cell_count == 17
    assert p.passed
    assert _gate_ok(p)


def test_metrics_include_violation_counts() -> None:
    log = (
        "Warning: Latch inferred for signal `\\q' from process `p'.\n"
        "Warning: Module `\\ram' is blackbox.\n"
        "ERROR: bad something\n"
    ) + _CLEAN_STAT
    p = parse_yosys_output(_run(stdout=log, rc=1))
    assert p.metrics["errors"] >= 3.0
    assert p.metrics["inferred_latches"] == 1.0
    assert p.metrics["blackbox_modules"] == 1.0


def _gate_ok(p: YosysParse) -> bool:
    """Mirror the VerificationArtifact.gate_ok formula for parser-level checks."""
    return p.passed and not any(v.severity == "error" for v in p.violations)


# --------------------------------------------------------------------------- #
# Runner wiring with a stub sandbox
# --------------------------------------------------------------------------- #
@dataclass
class StubSandbox:
    """Records the call and, optionally, drops files into the mount to
    simulate Yosys writing its netlist."""

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


def _stage_rtl(store: SqliteArtifactStore, *, source_text: str = "module counter; endmodule\n",
               language: str = "verilog", top: str = "counter") -> RTLArtifact:
    blob = store.put_blob(source_text.encode(), media_type="text/x-verilog")
    art = RTLArtifact(
        artifact_id=f"d0.{top}.rtl",
        design_id="d0",
        module_id=top,
        top_module=top,
        language=language,
        source=blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _write_netlist(text: str) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        (mount / "netlist.v").write_text(text)
    return _impl


# --------------------------------------------------------------------------- #
# Service tests
# --------------------------------------------------------------------------- #
def test_synth_runner_command_shape(store: SqliteArtifactStore) -> None:
    rtl = _stage_rtl(store)
    sandbox = StubSandbox(
        tool_run=_run(stdout=_CLEAN_STAT),
        side_effect=_write_netlist("// gate-level counter\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)

    netlist, report = svc.synthesize(rtl)

    assert report.passed
    assert report.gate_ok
    assert netlist.cell_count == 17
    assert netlist.std_cell_lib == "gf180mcu_fd_sc_mcu7t5v0"
    # The netlist BlobRef carries the bytes Yosys wrote in the sandbox.
    assert store.get_blob(netlist.netlist) == b"// gate-level counter\n"

    cmd, _mount, staged = sandbox.calls[0]
    assert cmd[0] == YOSYS_BIN
    assert "-s" in cmd
    assert cmd[-1] == "synth.ys"
    # The ``-q`` flag MUST NOT be present: it silences the ``stat``
    # command's "Number of cells: N" output, which the parser keys off
    # to surface a non-zero cell_count and gate the synth result.
    assert "-q" not in cmd
    # Source AND script both staged to the sandbox mount.
    assert staged["counter.v"] == b"module counter; endmodule\n"
    assert "synth.ys" in staged
    assert b"synth -top counter" in staged["synth.ys"]


def test_synth_returns_typed_pair(store: SqliteArtifactStore) -> None:
    # AC: netlist produced (NetlistArtifact); report is a SynthesisReport.
    rtl = _stage_rtl(store)
    sandbox = StubSandbox(
        tool_run=_run(stdout=_CLEAN_STAT),
        side_effect=_write_netlist("// netlist\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    netlist, report = svc.synthesize(rtl)

    assert netlist.kind is ArtifactKind.NETLIST
    assert report.kind is ArtifactKind.SYNTH_REPORT
    assert netlist.provenance.inputs == [rtl.ref()]
    assert report.provenance.inputs == [rtl.ref()]
    assert netlist.artifact_id == "d0.counter.netlist"
    assert report.artifact_id == "d0.counter.synth_report"


def test_synth_surfaces_inferred_latch_in_report(store: SqliteArtifactStore) -> None:
    # AC: inferred-latch warning -> Violation on the report.
    rtl = _stage_rtl(store)
    log = (
        "Warning: Latch inferred for signal `\\q' from process `p'.\n"
        + _CLEAN_STAT
    )
    sandbox = StubSandbox(
        tool_run=_run(stdout=log),
        side_effect=_write_netlist("// netlist\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    _, report = svc.synthesize(rtl)

    assert report.inferred_latches == 1
    assert any(v.code == "LATCH_INFERRED" for v in report.violations)
    assert not report.gate_ok


def test_synth_surfaces_blackbox_in_report(store: SqliteArtifactStore) -> None:
    # AC: blackbox warning -> Violation on the report.
    rtl = _stage_rtl(store)
    log = (
        "Warning: Module `\\ram' is blackbox.\n"
        + _CLEAN_STAT
    )
    sandbox = StubSandbox(
        tool_run=_run(stdout=log),
        side_effect=_write_netlist("// netlist\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    _, report = svc.synthesize(rtl)

    assert any(v.code == "BLACKBOX_MODULE" and "ram" in v.message
               for v in report.violations)
    assert not report.gate_ok


def test_synth_missing_netlist_on_pass_raises(store: SqliteArtifactStore) -> None:
    # Defensive: Yosys reported success but produced no netlist file.
    rtl = _stage_rtl(store)
    sandbox = StubSandbox(tool_run=_run(stdout=_CLEAN_STAT))  # no side_effect
    svc = YosysSynthService(sandbox=sandbox, store=store)
    with pytest.raises(YosysSynthError):
        svc.synthesize(rtl)


def test_synth_failure_still_returns_pair(store: SqliteArtifactStore) -> None:
    # On failure Yosys may or may not have written a netlist; the service still
    # returns the typed pair so the gate can read the violations.
    rtl = _stage_rtl(store)
    sandbox = StubSandbox(
        tool_run=_run(stderr="ERROR: bad input\n", rc=1),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    netlist, report = svc.synthesize(rtl)
    assert not report.passed
    assert any(v.code == "SYNTH.ERROR" for v in report.violations)
    # Netlist is recorded but empty — provenance is preserved.
    assert netlist.netlist.size_bytes == 0


def test_systemverilog_source_extension(store: SqliteArtifactStore) -> None:
    rtl = _stage_rtl(store, language="systemverilog", top="alu",
                     source_text="module alu; endmodule\n")
    sandbox = StubSandbox(
        tool_run=_run(stdout=_CLEAN_STAT),
        side_effect=_write_netlist("// alu\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    svc.synthesize(rtl)

    _, _mount, staged = sandbox.calls[0]
    assert "alu.sv" in staged
    assert "read_verilog -sv alu.sv" in staged["synth.ys"].decode()
