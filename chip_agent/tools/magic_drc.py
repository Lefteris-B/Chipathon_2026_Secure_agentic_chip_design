"""Magic DRC as a typed tool service (F6.3 — DRC leg).

Drives Magic in batch DRC mode over the routed DEF + PDK technology
file inside the sandbox. The parser reads Magic's report shape — either
the ``feedback`` block or the ``Total DRC errors found: N`` summary —
and emits a :class:`DRCReport`. The report's ``gate_ok`` inherits from
:class:`~chip_agent.design_state.VerificationArtifact`: PASSED only
when ``returncode == 0`` AND ``violation_count == 0`` AND no
error-severity violation rode along.

Parser and runner are split so the parser can be tested against canned
Magic output without spinning Docker.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chip_agent.design_state import (
    DRCReport,
    LayoutArtifact,
    Provenance,
    Stage,
    ToolRun,
    ToolVersion,
    Violation,
)
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike
from chip_agent.tools.pdk_paths import magic_tech

__all__ = [
    "MAGIC_BIN",
    "DRCParse",
    "MagicDRCService",
    "build_drc_script",
    "parse_librelane_drc_report",
    "parse_magic_drc_output",
]


MAGIC_BIN = "magic"

_TOTAL_RE = re.compile(
    r"^Total DRC errors? found:\s+(?P<n>\d+)\b",
    re.IGNORECASE,
)
# Magic emits per-rule lines like:
#   "Magic DRC: 3 errors in metal1 minimum spacing"
#   "metal1 minimum spacing (M.1)" followed by coordinates
_RULE_RE = re.compile(
    r"^(?P<message>[a-zA-Z][\w\s\-/(),.]+?)\s+\((?P<code>[A-Z][\w.]*)\)\s*$"
)
_COUNT_LINE_RE = re.compile(
    r"^(?P<rule>[a-zA-Z][\w\s\-/(),.]+?):\s+(?P<n>\d+)\s+errors?\s*$",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"^(?:Error|ERROR):\s*(?P<message>.+?)\s*$")


@dataclass(frozen=True)
class DRCParse:
    passed: bool
    violation_count: int
    violations: list[Violation]
    metrics: dict[str, float]


def build_drc_script(*, def_file: str, top_module: str, output_file: str = "drc.rpt") -> str:
    """Return a Magic TCL script that loads ``def_file`` and dumps a DRC report."""
    return "\n".join([
        f"def read {def_file}",
        f"load {top_module}",
        "select top cell",
        "drc check",
        f"drc listall why {output_file}",
        "drc count total",
        "quit -noprompt",
    ])


def parse_magic_drc_output(run: ToolRun, *, report_text: str | None = None) -> DRCParse:
    """Parse Magic's stdout/stderr (and optional DRC report file) into typed findings."""
    violations: list[Violation] = []
    total: int | None = None

    # 1) Streamed stdout — Magic prints rule lines + "Total DRC errors found".
    streams = run.stdout.splitlines() + run.stderr.splitlines()
    if report_text is not None:
        streams.extend(report_text.splitlines())

    for raw in streams:
        line = raw.strip()
        if not line:
            continue

        if m := _TOTAL_RE.match(line):
            n = int(m.group("n"))
            total = max(total or 0, n)
            continue
        if m := _COUNT_LINE_RE.match(line):
            n = int(m.group("n"))
            rule = m.group("rule").strip()
            violations.append(Violation(
                code="DRC.RULE",
                severity="error",
                message=f"{rule}: {n} error(s)",
                location=rule,
                detail={"rule": rule, "count": n, "raw_line": raw},
            ))
            continue
        if m := _RULE_RE.match(line):
            # A "<message> (<CODE>)" rule line — count it as one violation.
            violations.append(Violation(
                code="DRC.RULE",
                severity="error",
                message=m.group("message").strip(),
                location=m.group("code"),
                detail={"rule_code": m.group("code"), "raw_line": raw},
            ))
            continue
        if m := _ERROR_RE.match(line):
            violations.append(Violation(
                code="DRC.ERROR",
                severity="error",
                message=m.group("message"),
                detail={"raw_line": raw},
            ))

    if total is None:
        # No explicit summary — fall back to the parsed rule lines.
        total = sum(1 for v in violations if v.code == "DRC.RULE")
    elif total > 0 and not any(v.code == "DRC.RULE" for v in violations):
        # Magic only printed the summary — synthesize one rolled-up violation.
        violations.append(Violation(
            code="DRC.RULE",
            severity="error",
            message=f"{total} DRC violation(s) reported (no per-rule detail)",
            detail={"total": total},
        ))

    error_count = sum(1 for v in violations if v.severity == "error")
    metrics: dict[str, float] = {
        "drc_violations": float(total),
        "errors": float(error_count),
        "violations": float(len(violations)),
    }
    passed = run.returncode == 0 and total == 0 and error_count == 0
    return DRCParse(
        passed=passed,
        violation_count=total,
        violations=violations,
        metrics=metrics,
    )


def parse_librelane_drc_report(report_bytes: bytes) -> DRCParse:
    """Parse the Magic DRC report LibreLane emits during ``route``.

    LibreLane's report is the same Magic output shape as our own re-run
    produces, so we feed the bytes through ``parse_magic_drc_output``
    with a synthetic zero-returncode ``ToolRun``. The result is used by
    :meth:`MagicDRCService.check_drc` only for cross-checking — never
    as the binding gate.
    """
    synthetic_run = ToolRun(
        returncode=0, stdout="", stderr="",
        artifacts_dir="/tmp", duration_s=0.0,
    )
    return parse_magic_drc_output(
        synthetic_run, report_text=report_bytes.decode("utf-8", errors="replace"),
    )


class MagicDRCService:
    """Stage the routed DEF, run Magic DRC, return a typed :class:`DRCReport`."""

    NAME = "magic"

    def __init__(
        self,
        *,
        sandbox: SandboxLike,
        store: SqliteArtifactStore,
        version_str: str = "bundled",
        script_filename: str = "drc.tcl",
        def_filename: str = "design.def",
        report_filename: str = "drc.rpt",
        tech: str = magic_tech("gf180mcuD"),
        tracer: Tracer | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self._version_str = version_str
        self._script_filename = script_filename
        self._def_filename = def_filename
        self._report_filename = report_filename
        self._tech = tech
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=self.NAME,
            version=self._version_str,
            container_digest=digest,
        )

    def check_drc(
        self,
        layout: LayoutArtifact,
        *,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        librelane_report_bytes: bytes | None = None,
    ) -> DRCReport:
        """Run Magic DRC over ``layout`` and return a typed report.

        ``librelane_report_bytes`` (F12.2) — when supplied, LibreLane's
        own Magic DRC report from the ``route`` step is parsed and
        cross-checked against our re-run. If the violation counts
        disagree by ≥1, an informational
        ``LIBRELANE_DRC_DISAGREES`` violation lands in the report (with
        the two counts in ``detail``). Our re-run is the binding gate
        regardless — LibreLane's report is for triage.
        """
        top = top_module or layout.module_id or layout.artifact_id.split(".")[-2]
        with self._tracer.span(
            name="tool:magic_drc", kind=SpanKind.TOOL,
        ) as span:
            span.set_attribute("tool_name", self.NAME)
            span.set_attribute("design_id", layout.design_id)
            span.set_attribute("module_id", layout.module_id or top)
            span.set_attribute("top_module", top)
            with tempfile.TemporaryDirectory(prefix="chip-agent-drc-") as td:
                work = Path(td)
                (work / self._def_filename).write_bytes(
                    self.store.get_blob(layout.def_file),
                )
                (work / self._script_filename).write_text(build_drc_script(
                    def_file=self._def_filename,
                    top_module=top,
                    output_file=self._report_filename,
                ))
                run = self.sandbox.run(
                    [MAGIC_BIN, "-dnull", "-noconsole", "-T", self._tech,
                     self._script_filename],
                    mount=work,
                    time_limit_s=time_limit_s,
                    read_only_mount=False,
                )
                report_path = work / self._report_filename
                report_text = (
                    report_path.read_text() if report_path.exists() else None
                )

            parse = parse_magic_drc_output(run, report_text=report_text)
            librelane_parse = (
                parse_librelane_drc_report(librelane_report_bytes)
                if librelane_report_bytes else None
            )
            report = self._build_report(
                layout, parse, top_module=top, librelane_parse=librelane_parse,
            )
            span.set_attribute("passed", report.passed)
            span.set_attribute("violation_count", len(report.violations))
            return report

    def _build_report(
        self,
        layout: LayoutArtifact,
        parse: DRCParse,
        *,
        top_module: str,
        librelane_parse: DRCParse | None = None,
    ) -> DRCReport:
        tool = self.version()
        module = layout.module_id or top_module
        violations = list(parse.violations)
        metrics = dict(parse.metrics)
        # F12.2: cross-check against LibreLane's own report when supplied.
        # The gate stays on our re-run; the disagreement is informational.
        if librelane_parse is not None:
            metrics["librelane_drc_violations"] = float(
                librelane_parse.violation_count,
            )
            if librelane_parse.violation_count != parse.violation_count:
                violations.append(Violation(
                    code="LIBRELANE_DRC_DISAGREES",
                    severity="info",
                    message=(
                        f"LibreLane's Magic DRC reported "
                        f"{librelane_parse.violation_count} violation(s) "
                        f"but our re-run found {parse.violation_count}"
                    ),
                    detail={
                        "rerun_count": parse.violation_count,
                        "librelane_count": librelane_parse.violation_count,
                    },
                ))
        return DRCReport(
            artifact_id=f"{layout.design_id}.{module}.drc",
            design_id=layout.design_id,
            module_id=layout.module_id,
            passed=parse.passed,
            metrics=metrics,
            violations=violations,
            checker=tool,
            violation_count=parse.violation_count,
            provenance=Provenance(
                produced_by=Stage.SIGNOFF,
                agent="signoff_specialist",
                tool=tool,
                inputs=[layout.ref()],
            ),
        )
