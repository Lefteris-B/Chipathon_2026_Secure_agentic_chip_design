"""F2.1 acceptance: verible lint parser + runner over a stub sandbox."""

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
from chip_agent.tools.verible import (
    VERIBLE_BIN,
    LintParse,
    VeribleLintService,
    parse_lint_output,
)


# --------------------------------------------------------------------------- #
# Pure parser tests — these are the F2.1 AC.
# --------------------------------------------------------------------------- #
def _run(stdout: str = "", stderr: str = "", *, rc: int = 0) -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.1,
    )


def test_clean_rtl_passes() -> None:
    p = parse_lint_output(_run(), source_file="counter.v")
    assert isinstance(p, LintParse)
    assert p.passed
    assert p.violations == []
    assert p.metrics == {"violations": 0.0, "errors": 0.0}


def test_inferred_latch_yields_latch_inferred_error() -> None:
    # Verible-style finding for a missing default arm that infers a latch.
    out = (
        "counter.v:12:5: case statement is missing a default case "
        "[Style: case-missing-default]\n"
    )
    p = parse_lint_output(_run(out, rc=1), source_file="counter.v")
    assert not p.passed
    assert len(p.violations) == 1
    v = p.violations[0]
    assert v.code == "LATCH_INFERRED"
    assert v.severity == "error"
    assert v.location == "counter.v:12:5"
    assert "missing a default" in v.message
    assert v.detail["rule"] == "Style: case-missing-default"


def test_keyword_latch_fallback_when_rule_unknown() -> None:
    out = "counter.v:20:1: implicit latch inferred for q [Style: unknown-rule]\n"
    p = parse_lint_output(_run(out, rc=1), source_file="counter.v")
    assert any(v.code == "LATCH_INFERRED" and v.severity == "error"
               for v in p.violations)


def test_port_width_mismatch_mapped() -> None:
    out = "counter.v:8:7: width mismatch [implicit-bit-width]\n"
    p = parse_lint_output(_run(out, rc=1), source_file="counter.v")
    assert p.violations[0].code == "PORT_WIDTH_MISMATCH"
    assert p.violations[0].severity == "error"


def test_unknown_rule_becomes_lint_unknown_warning() -> None:
    out = "counter.v:3:1: a style nit [Style: prefix-style]\n"
    p = parse_lint_output(_run(out, rc=1), source_file="counter.v")
    assert p.violations[0].code == "LINT.UNKNOWN"
    assert p.violations[0].severity == "warning"
    assert p.violations[0].detail["raw_line"] == out.strip()


def test_unmatched_line_only_emitted_on_failure() -> None:
    # When verible exits non-zero with cryptic stderr, we keep the raw text.
    err = "Internal error: parser exploded\n"
    p_fail = parse_lint_output(_run(stderr=err, rc=2), source_file="counter.v")
    assert any(v.code == "LINT.UNKNOWN" and v.message.startswith("Internal error")
               for v in p_fail.violations)

    # On success, identical-looking noise is ignored.
    p_ok = parse_lint_output(_run(stderr=err, rc=0), source_file="counter.v")
    assert p_ok.violations == []
    assert p_ok.passed


def test_multiple_findings_aggregated() -> None:
    out = (
        "counter.v:12:5: case statement missing default [Style: case-missing-default]\n"
        "counter.v:18:9: undeclared net foo [undeclared-identifier]\n"
        "counter.v:25:1: width mismatch [implicit-bit-width]\n"
    )
    p = parse_lint_output(_run(out, rc=1), source_file="counter.v")
    codes = [v.code for v in p.violations]
    assert codes == ["LATCH_INFERRED", "UNDECLARED_ID", "PORT_WIDTH_MISMATCH"]
    assert p.metrics["errors"] == 3.0


def test_returncode_zero_with_findings_still_fails() -> None:
    # Defensive: even if verible reports rc=0 but we found an error-class issue,
    # we should NOT report passed. (Some lint configs emit rc=0 for warnings only.)
    out = "counter.v:1:1: case statement missing default [Style: case-missing-default]\n"
    p = parse_lint_output(_run(out, rc=0), source_file="counter.v")
    assert not p.passed
    assert p.metrics["errors"] == 1.0


def test_iic_osic_entrypoint_banner_is_stripped() -> None:
    """The IIC-OSIC-TOOLS entrypoint prints a banner before exec'ing the
    user command. Without filtering, those lines hit the parser's
    LINT.UNKNOWN catch-all whenever verible itself exits non-zero — a
    real run on a UART receiver produced six fake "violations" from
    banner noise alone and kept the RTL repair loop spinning until the
    budget escalated to HUMAN. Verbatim lines from
    ``runs/.../baud_gen.lint v3`` reproduce the trap."""
    banner = (
        "[INFO] USER_ID: 1000, GROUP_ID: 1000\n"
        "[INFO] Final PATH variable: /foss/tools/bin:/foss/tools/sak:"
        "/usr/local/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:"
        "/usr/bin:/sbin:/bin:/foss/tools/kactus2:/foss/tools/klayout:"
        "/foss/tools/osic-multitool\n"
        "[INFO] Final PYTHONPATH variable: /usr/lib/python312.zip:"
        "/usr/lib/python3.12:/usr/lib/python3.12/lib-dynload\n"
        "[WARN] tcsh not found, falling back to bash\n"
    )
    # Banner alone with a non-zero rc — the historical bug would log six
    # LINT.UNKNOWN violations.
    p = parse_lint_output(_run(banner, rc=1), source_file="baud_gen.v")
    assert p.violations == []
    # Still respects the contract: rc != 0 with no findings should fail
    # so the inner loop can react — but it should fail on the rc, not on
    # phantom violations.
    assert not p.passed
    assert p.metrics["errors"] == 0.0
    assert p.metrics["violations"] == 0.0


def test_style_warnings_do_not_block_gate() -> None:
    """Verible style-class rules (``explicit-parameter-storage-type``,
    ``parameter-name-style``, etc.) exit rc=1 but report no errors. The
    historical pass predicate ``rc == 0 and error_count == 0`` made every
    such warning fatal, which trapped the RTL repair loop on a UART
    baud_counter against pure style nits until the budget escalated to
    HUMAN. Verbatim stdout from ``runs/.../baud_counter.lint v1``."""
    stdout = (
        "baud_counter.v:2:15-26: Explicitly define a storage type for every "
        "parameter and localparam, (CLKS_PER_BIT). [Style: constants] "
        "[explicit-parameter-storage-type]\n"
        "baud_counter.v:3:15-23: Explicitly define a storage type for every "
        "parameter and localparam, (CTR_WIDTH). [Style: constants] "
        "[explicit-parameter-storage-type]\n"
        "baud_counter.v:4:15-22: Explicitly define a storage type for every "
        "parameter and localparam, (HALF_BIT). [Style: constants] "
        "[explicit-parameter-storage-type]\n"
    )
    p = parse_lint_output(_run(stdout, rc=1), source_file="baud_counter.v")
    # Findings still surface so the agent sees them in diagnosis.
    assert len(p.violations) == 3
    assert all(v.severity == "warning" for v in p.violations)
    # But the gate advances: no errors → rc=1 explained by parsed warnings.
    assert p.passed
    assert p.metrics["errors"] == 0.0


def test_camelcase_style_warnings_do_not_block_gate() -> None:
    """Second incarnation of the same trap: the model's "fix" for the
    previous style warnings introduced new localparam names that tripped
    ``parameter-name-style``. Verbatim stdout from ``runs/.../baud_counter.lint v3``."""
    stdout = (
        "baud_counter.v:15:24-40: Non-type localparam names must be styled "
        "with CamelCase [Style: constants] [parameter-name-style]\n"
        "baud_counter.v:16:24-36: Non-type localparam names must be styled "
        "with CamelCase [Style: constants] [parameter-name-style]\n"
    )
    p = parse_lint_output(_run(stdout, rc=1), source_file="baud_counter.v")
    assert len(p.violations) == 2
    assert all(v.severity == "warning" for v in p.violations)
    assert p.passed


def test_silent_nonzero_rc_still_fails() -> None:
    """The pass-predicate change must not regress the defensive case: if
    verible exits non-zero with no parseable output (banner-stripped or
    truly empty), the gate must still fail so the inner loop can react."""
    p = parse_lint_output(_run("", rc=1), source_file="counter.v")
    assert p.violations == []
    assert not p.passed


def test_banner_mixed_with_real_finding_keeps_only_finding() -> None:
    """Banner stripping must not eat genuine verible findings interleaved
    with the bootstrap noise."""
    mixed = (
        "[INFO] USER_ID: 1000, GROUP_ID: 1000\n"
        "counter.v:12:5: case statement is missing a default case "
        "[Style: case-missing-default]\n"
        "[INFO] Final PATH variable: /foss/tools/bin\n"
    )
    p = parse_lint_output(_run(mixed, rc=1), source_file="counter.v")
    assert len(p.violations) == 1
    assert p.violations[0].code == "LATCH_INFERRED"
    assert p.violations[0].location == "counter.v:12:5"


# --------------------------------------------------------------------------- #
# Runner wiring tests — stub sandbox captures the docker call and feeds canned
# verible output back through the parser into a LintResult.
# --------------------------------------------------------------------------- #
@dataclass
class StubSandbox:
    """Capture the docker invocation AND the staged-file contents at call time
    (the runner cleans up its tempdir before the test inspects anything)."""

    tool_run: ToolRun
    calls: list[tuple[list[str], Path, int | None, dict[str, bytes]]] = field(
        default_factory=list
    )

    def run(
        self, cmd: list[str], mount: Path | str, *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun:
        mount_path = Path(mount)
        staged = {p.name: p.read_bytes() for p in mount_path.iterdir() if p.is_file()}
        self.calls.append((list(cmd), mount_path, time_limit_s, staged))
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


def test_lint_runner_invokes_verible_with_staged_source(
    store: SqliteArtifactStore,
) -> None:
    rtl = _stage_rtl(store, source_text="module counter; endmodule\n")
    sandbox = StubSandbox(tool_run=_run())
    svc = VeribleLintService(sandbox=sandbox, store=store)

    result = svc.lint(rtl)

    assert result.passed
    assert result.gate_ok
    assert result.metrics["errors"] == 0.0
    assert result.checker is not None and result.checker.name == "verible"
    assert result.provenance.inputs == [rtl.ref()]
    assert result.artifact_id == "d0.counter.lint"

    assert len(sandbox.calls) == 1
    cmd, _mount, _timeout, staged = sandbox.calls[0]
    assert cmd[0] == VERIBLE_BIN
    assert cmd[1] == "counter.v"
    # The staged source actually landed in the mount dir at call time.
    assert staged["counter.v"] == b"module counter; endmodule\n"


def test_lint_runner_returns_violations_on_failure(
    store: SqliteArtifactStore,
) -> None:
    rtl = _stage_rtl(
        store, source_text="// stub\nmodule counter; endmodule\n",
        language="systemverilog",
    )
    bad_output = (
        "counter.sv:12:5: case statement missing default "
        "[Style: case-missing-default]\n"
    )
    sandbox = StubSandbox(tool_run=_run(bad_output, rc=1))
    svc = VeribleLintService(sandbox=sandbox, store=store)

    result = svc.lint(rtl)

    assert not result.passed
    assert not result.gate_ok
    assert len(result.violations) == 1
    assert result.violations[0].code == "LATCH_INFERRED"
    assert result.violations[0].severity == "error"
    # SystemVerilog gets a .sv extension.
    cmd = sandbox.calls[0][0]
    assert cmd[1] == "counter.sv"


def test_lint_runner_records_image_digest_in_tool_version() -> None:
    from chip_agent.settings import SandboxSettings
    from chip_agent.tools.sandbox import SandboxRunner

    digest = "sha256:" + "a" * 64
    sb = SandboxSettings(image="hpretl/iic-osic-tools", image_tag="2026.04",
                         image_digest=digest)

    # Build a real SandboxRunner but never actually invoke it; the service is
    # only asked for its version.
    @dataclass
    class _NoopProc:
        def run(self, argv: list[str], *, timeout: int | None = None) -> object:
            raise AssertionError("should not be called")

    runner = SandboxRunner(sb, process_runner=_NoopProc())  # type: ignore[arg-type]

    class _ProdStore:  # only used to satisfy the type — no methods invoked here
        pass

    svc = VeribleLintService(sandbox=runner, store=_ProdStore())  # type: ignore[arg-type]
    v = svc.version()
    assert v.container_digest == digest
    assert v.name == "verible"
