"""F6.3 — Netgen LVS service. Parser + runner over a stub sandbox."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import (
    LayoutArtifact,
    NetlistArtifact,
    Provenance,
    Stage,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.netgen_lvs import (
    NETGEN_BIN,
    LVSParse,
    NetgenLVSService,
    build_lvs_script,
    parse_netgen_lvs_output,
)


def _run(*, rc: int = 0, stdout: str = "", stderr: str = "") -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.1,
    )


def test_lvs_script_compares_both_netlists() -> None:
    s = build_lvs_script(
        netlist_file="design.nl.v",
        layout_netlist_file="extracted.spice",
        top_module="counter",
    )
    assert "lvs" in s
    assert "extracted.spice counter" in s
    assert "design.nl.v counter" in s


def test_lvs_script_does_not_source_setup_file_explicitly() -> None:
    """``lvs`` consumes the setup file as its third positional arg and
    sources it internally with a circuit already declared. An explicit
    ``source <setup_file>`` BEFORE ``lvs`` makes the setup script's
    ``cells list -all -circuit1`` call fail with ``No circuit has been
    declared for comparison`` and Netgen exits before the comparison
    runs — exactly the live ``counter`` ``LVS.UNKNOWN`` trap."""
    s = build_lvs_script(
        netlist_file="design.nl.v",
        layout_netlist_file="extracted.spice",
        top_module="counter",
        setup_file="/foss/pdks/gf180mcuD/libs.tech/netgen/gf180mcuD_setup.tcl",
    )
    # The setup file MUST appear inside the ``lvs`` call (positional arg
    # 3) — that's how Netgen actually sources it.
    assert "/foss/pdks/gf180mcuD/libs.tech/netgen/gf180mcuD_setup.tcl" in s
    # But the script must NOT start with an explicit ``source <setup>``
    # line: that's what causes the silent-fail trap.
    first_line = s.splitlines()[0]
    assert not first_line.startswith("source "), (
        f"build_lvs_script must not source the setup file before lvs; "
        f"got first line {first_line!r}"
    )


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_circuits_match_passes() -> None:
    log = "Circuits match uniquely.\n"
    p = parse_netgen_lvs_output(_run(stdout=log))
    assert isinstance(p, LVSParse)
    assert p.passed
    assert p.matched
    assert p.mismatch_count == 0
    assert p.violations == []


def test_netlists_match_in_report_passes() -> None:
    """Netgen writes ``Netlists match uniquely.`` to the report file
    while stdout carries ``Circuits match uniquely.`` — the parser must
    treat both as a pass verdict. Without this widening a fix that
    reads only the report file (or a future image bump that swaps the
    stdout wording) would silently lose the match. Verbatim from the
    LVS run we shipped: tail of ``runs/.../lvs.rpt``."""
    report = (
        "Subcircuit pins:\n"
        "Circuit 1: counter_8bit_sync               |"
        "Circuit 2: counter_8bit_sync               \n"
        "Cell pin lists are equivalent.\n"
        "Device classes counter_8bit_sync and counter_8bit_sync "
        "are equivalent.\n"
        "\n"
        "Final result: Netlists match uniquely.\n"
    )
    p = parse_netgen_lvs_output(_run(rc=0), report_text=report)
    assert p.passed
    assert p.matched
    assert p.mismatch_count == 0
    assert p.violations == []


def test_match_with_symmetries_still_passes() -> None:
    """The verdict line can read ``Netlists match with N symmetries.``
    too — confirm the widened regex still accepts the symmetry form on
    BOTH ``Circuits`` and ``Netlists`` wording."""
    for verb in ("Circuits", "Netlists"):
        log = f"{verb} match with 2 symmetries.\n"
        p = parse_netgen_lvs_output(_run(stdout=log))
        assert p.passed, f"failed to match {verb!r} symmetry-form verdict"
        assert p.matched


def test_no_match_with_counts_fails() -> None:
    log = (
        "Netlists do not match.\n"
        "3 device mismatches.\n"
        "1 net mismatch.\n"
    )
    p = parse_netgen_lvs_output(_run(stdout=log, rc=1))
    assert not p.passed
    assert not p.matched
    assert p.mismatch_count == 4
    assert any(v.code == "LVS.MISMATCH" for v in p.violations)


def test_no_match_without_counts_still_fails() -> None:
    log = "Netlists do not match.\n"
    p = parse_netgen_lvs_output(_run(stdout=log, rc=1))
    assert not p.passed
    assert not p.matched
    # No explicit count -> we surface at least 1 so downstream metrics aren't zero.
    assert p.mismatch_count >= 1


def test_empty_output_falls_back_to_unknown() -> None:
    p = parse_netgen_lvs_output(_run(rc=0))
    assert not p.passed
    assert not p.matched
    assert any(v.code == "LVS.UNKNOWN" for v in p.violations)


def test_nonzero_returncode_fails_even_on_match() -> None:
    log = "Circuits match uniquely.\n"
    p = parse_netgen_lvs_output(_run(stdout=log, rc=1))
    assert not p.passed


def test_error_line_surfaces_as_violation() -> None:
    log = "Error: cannot read /foss/pdks/.../setup.tcl\n"
    p = parse_netgen_lvs_output(_run(stderr=log, rc=1))
    assert any(v.code == "LVS.ERROR" for v in p.violations)


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


def _netlist(store: SqliteArtifactStore) -> NetlistArtifact:
    blob = store.put_blob(b"// gate-level\n", media_type="text/x-verilog")
    art = NetlistArtifact(
        artifact_id="d0.counter.netlist",
        design_id="d0", module_id="counter",
        netlist=blob, std_cell_lib="sky130_fd_sc_hd",
        cell_count=10,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _layout(store: SqliteArtifactStore) -> LayoutArtifact:
    blob = store.put_blob(b"# DEF\nEND DESIGN\n", media_type="text/x-def")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=blob, stage_reached="routed",
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _drop_report(text: str) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        (mount / "lvs.rpt").write_text(text)
    return _impl


def test_runner_match_passes(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Circuits match uniquely.\n"),
        side_effect=_drop_report(""),
    )
    svc = NetgenLVSService(sandbox=sandbox, store=store)
    report = svc.check_lvs(
        _netlist(store), _layout(store),
        layout_netlist_bytes=b"* extracted netlist\n",
    )
    assert report.gate_ok
    assert report.matched
    assert report.mismatch_count == 0

    cmd, _mount, staged = sandbox.calls[0]
    assert cmd[0] == NETGEN_BIN
    assert "-batch" in cmd
    assert cmd[-1] == "lvs.tcl"
    assert "design.nl.v" in staged
    assert "extracted.spice" in staged


def test_runner_mismatch_blocks_gate(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Netlists do not match.\n2 device mismatches.\n",
                      rc=1),
        side_effect=_drop_report(""),
    )
    svc = NetgenLVSService(sandbox=sandbox, store=store)
    report = svc.check_lvs(
        _netlist(store), _layout(store),
        layout_netlist_bytes=b"* extracted netlist\n",
    )
    assert not report.gate_ok
    assert not report.matched
    assert report.mismatch_count == 2
    # Provenance points at BOTH input artifacts.
    refs = [r.artifact_id for r in report.provenance.inputs]
    assert "d0.counter.netlist" in refs
    assert "d0.counter.layout" in refs


# --------------------------------------------------------------------------- #
# F13.1 — netlist_bytes_override: when SIGNOFF feeds the LibreLane mapped
# netlist, LVS stages those bytes as the synth-side input instead of
# NetlistArtifact.netlist.
# --------------------------------------------------------------------------- #
def test_check_lvs_uses_override_bytes_for_synth_side(
    store: SqliteArtifactStore,
) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Circuits match uniquely.\n"),
        side_effect=_drop_report(""),
    )
    svc = NetgenLVSService(sandbox=sandbox, store=store)
    netlist = _netlist(store)
    mapped = b"module counter; sky130_fd_sc_hd__dfrtp_2 dff_0 (...); endmodule\n"

    svc.check_lvs(
        netlist, _layout(store),
        layout_netlist_bytes=b"* extracted netlist\n",
        netlist_bytes_override=mapped,
    )

    _cmd, _mount, staged = sandbox.calls[0]
    assert staged["design.nl.v"] == mapped
    # NetlistArtifact's blob was NOT staged.
    assert staged["design.nl.v"] != store.get_blob(netlist.netlist)


def test_check_lvs_falls_back_to_netlist_artifact_when_no_override(
    store: SqliteArtifactStore,
) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Circuits match uniquely.\n"),
        side_effect=_drop_report(""),
    )
    svc = NetgenLVSService(sandbox=sandbox, store=store)
    netlist = _netlist(store)

    svc.check_lvs(
        netlist, _layout(store),
        layout_netlist_bytes=b"* extracted netlist\n",
    )

    _cmd, _mount, staged = sandbox.calls[0]
    assert staged["design.nl.v"] == store.get_blob(netlist.netlist)


# --------------------------------------------------------------------------- #
# F18-mirror: ``netgen_log`` sidecar blob captures Netgen's raw stdout +
# stderr + report so an ``LVS.UNKNOWN`` failure is no longer silent. The
# blob is excluded from the LVSReport's content hash (transcript varies
# between executions) and resolves to ``None`` when no streams were
# produced (audit-distinct from "blob present but empty").
# --------------------------------------------------------------------------- #
def test_netgen_log_blob_captures_stdout_and_report(
    store: SqliteArtifactStore,
) -> None:
    """A healthy run still persists Netgen's stdout + report text as a
    sidecar blob so a future post-mortem on an LVS regression doesn't
    need to re-execute the flow. Both streams + the report carry
    section headers so the consumer can split them."""
    report_text = (
        "Subcircuit pins:\n"
        "Cell pin lists are equivalent.\n"
        "Final result: Netlists match uniquely.\n"
    )
    sandbox = StubSandbox(
        tool_run=_run(stdout="Circuits match uniquely.\n",
                      stderr="Warning: ::netgen::format\n"),
        side_effect=_drop_report(report_text),
    )
    svc = NetgenLVSService(sandbox=sandbox, store=store)
    report = svc.check_lvs(
        _netlist(store), _layout(store),
        layout_netlist_bytes=b"* extracted netlist\n",
    )
    assert report.gate_ok
    assert report.netgen_log is not None
    body = store.get_blob(report.netgen_log).decode()
    assert "=== stdout ===" in body
    assert "=== stderr ===" in body
    assert "=== lvs.rpt ===" in body
    assert "Circuits match uniquely" in body
    assert "::netgen::format" in body
    assert "Netlists match uniquely" in body


def test_netgen_log_is_none_when_all_streams_empty(
    store: SqliteArtifactStore,
) -> None:
    """No output to capture -> field stays None, not an empty blob.
    Keeps the audit's "no output captured" signal distinguishable from
    "a blob exists but it's empty"."""
    sandbox = StubSandbox(
        tool_run=_run(stdout="Circuits match uniquely.\n"),
        side_effect=None,           # no report file
    )
    # rebuild a stub with truly empty streams + no report.
    sandbox.tool_run = _run()
    svc = NetgenLVSService(sandbox=sandbox, store=store)
    report = svc.check_lvs(
        _netlist(store), _layout(store),
        layout_netlist_bytes=b"* extracted netlist\n",
    )
    assert report.netgen_log is None


def test_netgen_log_captured_when_lvs_unknown_fires(
    store: SqliteArtifactStore,
) -> None:
    """The whole point of the sidecar: when Netgen exits silently with
    ``LVS.UNKNOWN``, the log blob preserves whatever Tcl error / Netgen
    diagnostic actually came out, so the operator can read it instead
    of guessing. Verbatim from the live ``counter`` run."""
    netgen_stderr = (
        "No circuit has been declared for comparison\n"
        "    while executing\n"
        '"cells list -all -circuit1"\n'
        '    (file "/foss/pdks/sky130A/libs.tech/netgen/sky130A_setup.tcl" '
        'line 17)\n'
    )
    sandbox = StubSandbox(
        tool_run=_run(stdout="Netgen 1.5.318 compiled\n",
                      stderr=netgen_stderr, rc=1),
        side_effect=None,           # no report file produced
    )
    svc = NetgenLVSService(sandbox=sandbox, store=store)
    report = svc.check_lvs(
        _netlist(store), _layout(store),
        layout_netlist_bytes=b"* extracted netlist\n",
    )
    # The verdict is still LVS.UNKNOWN (the parser sees no match line)…
    assert not report.gate_ok
    assert any(v.code == "LVS.UNKNOWN" for v in report.violations)
    # …but the sidecar blob carries the *reason* Netgen failed.
    assert report.netgen_log is not None
    body = store.get_blob(report.netgen_log).decode()
    assert "No circuit has been declared" in body
    assert "cells list -all -circuit1" in body
