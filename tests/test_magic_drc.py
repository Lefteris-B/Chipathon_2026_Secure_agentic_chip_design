"""F6.3 — Magic DRC service. Parser + runner over a stub sandbox."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import (
    LayoutArtifact,
    Provenance,
    Stage,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.magic_drc import (
    MAGIC_BIN,
    DRCParse,
    MagicDRCService,
    build_drc_script,
    parse_librelane_drc_report,
    parse_magic_drc_output,
)


def _run(*, rc: int = 0, stdout: str = "", stderr: str = "") -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.1,
    )


def test_drc_script_invokes_drc_check_and_report() -> None:
    s = build_drc_script(def_file="d.def", top_module="counter")
    assert "def read d.def" in s
    assert "load counter" in s
    assert "drc check" in s
    assert "drc count total" in s


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_clean_run_passes() -> None:
    log = (
        "Loading DEF...\n"
        "Total DRC errors found: 0\n"
    )
    p = parse_magic_drc_output(_run(stdout=log))
    assert isinstance(p, DRCParse)
    assert p.passed
    assert p.violation_count == 0
    assert p.violations == []


def test_summary_only_synthesizes_rollup_violation() -> None:
    log = "Total DRC errors found: 7\n"
    p = parse_magic_drc_output(_run(stdout=log, rc=0))
    assert not p.passed
    assert p.violation_count == 7
    assert any(v.code == "DRC.RULE" and "7" in v.message for v in p.violations)


def test_per_rule_counts_aggregate() -> None:
    log = (
        "metal1 minimum spacing: 3 errors\n"
        "metal2 minimum width: 1 error\n"
        "Total DRC errors found: 4\n"
    )
    p = parse_magic_drc_output(_run(stdout=log))
    assert not p.passed
    assert p.violation_count == 4
    rules = [v for v in p.violations if v.code == "DRC.RULE"]
    assert len(rules) == 2  # one per rule line


def test_named_rule_violation_lines() -> None:
    report = (
        "metal1 minimum spacing (M.1)\n"
        "metal2 minimum width (M.2)\n"
    )
    p = parse_magic_drc_output(_run(rc=0), report_text=report)
    assert not p.passed  # no Total found -> fall back to rule-line count
    assert p.violation_count == 2
    assert {v.location for v in p.violations} == {"M.1", "M.2"}


def test_nonzero_returncode_fails_even_with_zero_total() -> None:
    log = "Total DRC errors found: 0\n"
    p = parse_magic_drc_output(_run(stdout=log, rc=1))
    assert not p.passed


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
        (mount / "drc.rpt").write_text(text)
    return _impl


def test_runner_clean(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Total DRC errors found: 0\n"),
        side_effect=_drop_report(""),
    )
    svc = MagicDRCService(sandbox=sandbox, store=store)
    report = svc.check_drc(_layout(store))
    assert report.gate_ok
    assert report.violation_count == 0

    cmd, _mount, staged = sandbox.calls[0]
    assert cmd[0] == MAGIC_BIN
    assert "-T" in cmd and "gf180mcuD" in cmd
    assert cmd[-1] == "drc.tcl"
    assert "design.def" in staged
    assert "drc.tcl" in staged


def test_runner_failure_surfaces_count(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout=""),
        side_effect=_drop_report(
            "metal1 minimum spacing (M.1)\n"
            "Total DRC errors found: 1\n"
        ),
    )
    svc = MagicDRCService(sandbox=sandbox, store=store)
    report = svc.check_drc(_layout(store))
    assert not report.gate_ok
    assert report.violation_count == 1


# --------------------------------------------------------------------------- #
# F12.2 — LibreLane cross-check: when the SIGNOFF stage feeds LibreLane's
# own Magic DRC report bytes through ``librelane_report_bytes``, the parser
# runs over both and emits a ``LIBRELANE_DRC_DISAGREES`` violation on
# count divergence. Our re-run is still the binding gate.
# --------------------------------------------------------------------------- #
def test_parse_librelane_drc_report_recognises_summary() -> None:
    bytes_ = b"Loading DEF...\nTotal DRC errors found: 7\n"
    parse = parse_librelane_drc_report(bytes_)
    assert parse.violation_count == 7


def test_drc_agreement_does_not_emit_disagree_violation(
    store: SqliteArtifactStore,
) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Total DRC errors found: 0\n"),
        side_effect=_drop_report(""),
    )
    svc = MagicDRCService(sandbox=sandbox, store=store)
    report = svc.check_drc(
        _layout(store),
        librelane_report_bytes=b"Total DRC errors found: 0\n",
    )
    assert report.gate_ok
    assert report.violation_count == 0
    assert report.metrics["librelane_drc_violations"] == 0.0
    assert all(
        v.code != "LIBRELANE_DRC_DISAGREES" for v in report.violations
    )


def test_drc_disagreement_emits_informational_violation(
    store: SqliteArtifactStore,
) -> None:
    """Our re-run says 0; LibreLane's report says 3. The gate stays
    PASSED (our re-run is binding), but the disagreement is logged."""
    sandbox = StubSandbox(
        tool_run=_run(stdout="Total DRC errors found: 0\n"),
        side_effect=_drop_report(""),
    )
    svc = MagicDRCService(sandbox=sandbox, store=store)
    report = svc.check_drc(
        _layout(store),
        librelane_report_bytes=b"Total DRC errors found: 3\n",
    )
    # Our re-run is binding — gate stays open since WE found 0.
    assert report.violation_count == 0
    assert report.gate_ok  # our re-run is clean; the info violation does not close the gate
    # LibreLane count surfaces via metrics + a typed info violation.
    assert report.metrics["librelane_drc_violations"] == 3.0
    disagree = [
        v for v in report.violations if v.code == "LIBRELANE_DRC_DISAGREES"
    ]
    assert len(disagree) == 1
    assert disagree[0].severity == "info"  # not error — does not close gate
    assert disagree[0].detail["rerun_count"] == 0
    assert disagree[0].detail["librelane_count"] == 3


def test_drc_without_librelane_report_does_not_record_metric(
    store: SqliteArtifactStore,
) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Total DRC errors found: 0\n"),
        side_effect=_drop_report(""),
    )
    svc = MagicDRCService(sandbox=sandbox, store=store)
    report = svc.check_drc(_layout(store))
    assert "librelane_drc_violations" not in report.metrics
