"""F2.2 acceptance: verilator elaborate parser + runner over a stub sandbox."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import (
    Provenance,
    RTLArtifact,
    Stage,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.verilator import (
    VERILATOR_BIN,
    ElaborateParse,
    VerilatorElaborateService,
    parse_elaborate_output,
)


def _run(stdout: str = "", stderr: str = "", *, rc: int = 0) -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.1,
    )


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_clean_elaborate_passes() -> None:
    p = parse_elaborate_output(_run(), source_file="counter.v")
    assert isinstance(p, ElaborateParse)
    assert p.passed
    assert p.violations == []


def test_port_width_mismatch_with_location() -> None:
    # The F2.2 AC: a port-width mismatch -> structured violation with file:line.
    err = (
        "%Warning-WIDTH: counter.v:8:5: Operator ASSIGN expects 8 bits on the "
        "Assign LHS, but Assign LHS's VARREF 'q' generates 4 bits.\n"
    )
    p = parse_elaborate_output(_run(stderr=err, rc=1), source_file="counter.v")
    assert not p.passed
    assert len(p.violations) == 1
    v = p.violations[0]
    assert v.code == "PORT_WIDTH_MISMATCH"
    assert v.severity == "error"
    assert v.location == "counter.v:8:5"
    assert "expects 8 bits" in v.message
    assert v.detail["rule"] == "WIDTH"
    assert v.detail["verilator_severity"] == "Warning"


def test_width_keyword_fallback_when_rule_absent() -> None:
    err = (
        "%Warning: counter.v:10:1: Operator ASSIGN expects 4 bits on the LHS\n"
    )
    p = parse_elaborate_output(_run(stderr=err, rc=1), source_file="counter.v")
    assert p.violations[0].code == "PORT_WIDTH_MISMATCH"


def test_inferred_latch_mapped() -> None:
    err = "%Warning-LATCH: counter.v:14:5: Variable 'next_q' latched\n"
    p = parse_elaborate_output(_run(stderr=err, rc=0), source_file="counter.v")
    assert p.violations[0].code == "LATCH_INFERRED"
    assert p.violations[0].severity == "error"


def test_error_severity_propagates() -> None:
    err = (
        "%Error: counter.v:3:1: syntax error, unexpected end of file\n"
    )
    p = parse_elaborate_output(_run(stderr=err, rc=1), source_file="counter.v")
    assert not p.passed
    assert p.violations[0].code == "SYNTAX"
    assert p.violations[0].severity == "error"


def test_unknown_rule_becomes_elaborate_unknown() -> None:
    err = "%Warning-ZZZUNKNOWNZZZ: counter.v:1:1: weird stylistic note\n"
    p = parse_elaborate_output(_run(stderr=err, rc=0), source_file="counter.v")
    assert p.violations[0].code == "ELABORATE.UNKNOWN"
    assert p.violations[0].severity == "warning"


def test_summary_line_without_position_kept_on_failure() -> None:
    err = (
        "%Warning-WIDTH: counter.v:8:5: width mismatch\n"
        "%Error: Exiting due to 1 error(s)\n"
    )
    p = parse_elaborate_output(_run(stderr=err, rc=2), source_file="counter.v")
    codes = [v.code for v in p.violations]
    assert "PORT_WIDTH_MISMATCH" in codes
    assert "ELABORATE.UNKNOWN" in codes  # the summary line preserved


def test_unused_parameter_warnings_do_not_block_gate() -> None:
    """``verilator --lint-only -Wall`` treats UNUSEDPARAM as a warning and
    exits non-zero with the summary line ``%Error: Exiting due to N
    warning(s)``. The historical pass predicate ``rc == 0 and error_count
    == 0`` made every such warning fatal, which kept the RTL repair loop
    rewriting a clean baud_counter against pure unused-param noise. Lines
    modeled on Verilator's actual UNUSEDPARAM output for the v2 RTL that
    triggered ``runs/.../baud_counter.elaborate v1``."""
    err = (
        "%Warning-UNUSEDPARAM: baud_counter.v:2:23: Parameter is not used: "
        "'CLKS_PER_BIT'\n"
        "%Warning-UNUSEDPARAM: baud_counter.v:4:23: Parameter is not used: "
        "'HALF_BIT'\n"
        "%Error: Exiting due to 2 warning(s)\n"
    )
    p = parse_elaborate_output(_run(stderr=err, rc=1), source_file="baud_counter.v")
    # Findings still surface (UNUSEDPARAM unmapped → ELABORATE.UNKNOWN; the
    # summary line is captured as a positionless ELABORATE.UNKNOWN).
    assert len(p.violations) == 3
    assert all(v.severity == "warning" for v in p.violations)
    assert p.metrics["errors"] == 0.0
    # Gate advances: no errors → rc=1 explained by parsed warnings.
    assert p.passed


def test_silent_nonzero_rc_still_fails() -> None:
    """The pass-predicate change must not regress the defensive case: if
    verilator exits non-zero with no parseable output, the gate must
    still fail so the inner loop can react."""
    p = parse_elaborate_output(_run("", "", rc=1), source_file="counter.v")
    assert p.violations == []
    assert not p.passed


def test_returncode_zero_with_error_still_fails() -> None:
    err = "%Error: counter.v:1:1: bad token [SYNTAX]\n"  # rule is ignored here
    p = parse_elaborate_output(_run(stderr=err, rc=0), source_file="counter.v")
    assert not p.passed
    assert p.metrics["errors"] >= 1.0


# --------------------------------------------------------------------------- #
# Runner wiring with a stub sandbox
# --------------------------------------------------------------------------- #
@dataclass
class StubSandbox:
    tool_run: ToolRun
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
        return self.tool_run


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


def _stage_rtl(store: SqliteArtifactStore, *, source_text: str,
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


def test_elaborate_runner_command_shape(store: SqliteArtifactStore) -> None:
    rtl = _stage_rtl(store, source_text="module counter; endmodule\n")
    sandbox = StubSandbox(tool_run=_run())
    svc = VerilatorElaborateService(sandbox=sandbox, store=store)

    result = svc.elaborate(rtl)

    assert result.passed
    assert result.checker is not None and result.checker.name == "verilator"
    assert result.artifact_id == "d0.counter.elaborate"
    assert result.provenance.inputs == [rtl.ref()]

    cmd, _mount, staged = sandbox.calls[0]
    assert cmd[0] == VERILATOR_BIN
    assert cmd[1:3] == ["--lint-only", "-Wall"]
    assert cmd[-1] == "counter.v"
    assert staged["counter.v"] == b"module counter; endmodule\n"


def test_elaborate_runner_surfaces_width_mismatch(
    store: SqliteArtifactStore,
) -> None:
    rtl = _stage_rtl(store, source_text="// stub\n")
    bad = (
        "%Warning-WIDTH: counter.v:8:5: Operator ASSIGN expects 8 bits "
        "but generates 4 bits\n"
    )
    sandbox = StubSandbox(tool_run=_run(stderr=bad, rc=1))
    svc = VerilatorElaborateService(sandbox=sandbox, store=store)

    result = svc.elaborate(rtl)

    assert not result.passed
    assert not result.gate_ok
    assert any(v.code == "PORT_WIDTH_MISMATCH" and v.location == "counter.v:8:5"
               for v in result.violations)


def test_elaborate_extra_flags_pass_through(store: SqliteArtifactStore) -> None:
    rtl = _stage_rtl(store, source_text="// stub\n", language="systemverilog")
    sandbox = StubSandbox(tool_run=_run())
    svc = VerilatorElaborateService(sandbox=sandbox, store=store)
    svc.elaborate(rtl, extra_flags=["--top-module", "counter"])
    cmd = sandbox.calls[0][0]
    assert "--top-module" in cmd
    # SystemVerilog gets a .sv extension.
    assert cmd[-1] == "counter.sv"
