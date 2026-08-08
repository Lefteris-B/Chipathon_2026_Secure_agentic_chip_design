"""Netgen LVS as a typed tool service (F6.3 — LVS leg).

Runs Netgen's ``lvs`` command between the synth netlist and the extracted
layout netlist inside the sandbox. The parser keys on the canonical
Netgen verdict lines — ``Circuits match uniquely`` for the pass case,
and ``Netlists do not match`` / ``Property errors`` / mismatch counts
for the fail cases — and returns a typed :class:`LVSReport`.

The report's ``gate_ok`` inherits from
:class:`~chip_agent.design_state.VerificationArtifact`: PASSED only
when ``returncode == 0`` AND ``matched`` AND no error-severity violation
rode along.

Parser and runner are split for the same reason as the rest of the M6
services — canned Netgen output drives the parser unit tests, the
runner test injects a stub sandbox.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chip_agent.design_state import (
    BlobRef,
    LayoutArtifact,
    LVSReport,
    NetlistArtifact,
    Provenance,
    Stage,
    ToolRun,
    ToolVersion,
    Violation,
)
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike
from chip_agent.tools.pdk_paths import netgen_setup

__all__ = [
    "NETGEN_BIN",
    "LVSParse",
    "NetgenLVSService",
    "build_lvs_script",
    "parse_netgen_lvs_output",
]


NETGEN_BIN = "netgen"

# Netgen verdicts — match lines first (clean exit), then the various failure
# shapes. Counts come from "N device mismatches" / "N net mismatches" /
# "N property errors".
# Netgen prints ``Circuits match uniquely.`` to stdout but writes
# ``Final result: Netlists match uniquely.`` to the report file — both
# must read as a pass verdict, or we'd lose the match when the parser
# is fed only the report file (and miss it again if a future image bump
# swaps the stdout wording). The ``Final result:`` prefix is optional
# because the stub-test inputs (and older Netgen image tags) emit the
# raw verdict line without it.
_MATCH_RE = re.compile(
    r"^(?:Final\s+result:\s+)?"
    r"(?:Circuits|Netlists)\s+match\s+"
    r"(?:uniquely|with\s+[0-9]+\s+symmetries)\.?\s*$",
    re.IGNORECASE,
)
_NOMATCH_RE = re.compile(
    r"^(?:Netlists do not match|Circuits do not match)\.?\s*$",
    re.IGNORECASE,
)
_COUNT_RES = (
    re.compile(r"^(?P<n>\d+)\s+device mismatch(?:es)?\s*\.?\s*$", re.IGNORECASE),
    re.compile(r"^(?P<n>\d+)\s+net mismatch(?:es)?\s*\.?\s*$", re.IGNORECASE),
    re.compile(r"^(?P<n>\d+)\s+pin mismatch(?:es)?\s*\.?\s*$", re.IGNORECASE),
    re.compile(r"^(?P<n>\d+)\s+property error(?:s)?\s*\.?\s*$", re.IGNORECASE),
)
_ERROR_RE = re.compile(r"^(?:Error|ERROR):\s*(?P<message>.+?)\s*$")


@dataclass(frozen=True)
class LVSParse:
    passed: bool
    matched: bool
    mismatch_count: int
    violations: list[Violation]
    metrics: dict[str, float]


def build_lvs_script(
    *,
    netlist_file: str,
    layout_netlist_file: str,
    top_module: str,
    setup_file: str = netgen_setup("gf180mcuD"),
    output_file: str = "lvs.rpt",
) -> str:
    """Return a Netgen TCL script that LVS-compares two netlists.

    The ``lvs`` command consumes the PDK setup file as its third
    positional argument and sources it internally with a circuit already
    declared for comparison. An explicit ``source <setup_file>`` BEFORE
    ``lvs`` makes the setup script's ``cells list -all -circuit1`` call
    fail with ``No circuit has been declared for comparison``, and
    Netgen exits before the LVS comparison runs — no report file, no
    parseable verdict, just ``LVS.UNKNOWN``. This is exactly the trap
    that bit the live ``counter`` run (sky130 LVS verdict was lost
    despite both netlists actually matching).
    """
    return "\n".join([
        f"lvs {{{layout_netlist_file} {top_module}}} "
        f"{{{netlist_file} {top_module}}} {setup_file} {output_file}",
        "quit",
    ])


def parse_netgen_lvs_output(
    run: ToolRun, *, report_text: str | None = None,
) -> LVSParse:
    """Parse Netgen LVS output into a typed parse result."""
    matched = False
    mismatch_total = 0
    violations: list[Violation] = []

    streams = run.stdout.splitlines() + run.stderr.splitlines()
    if report_text is not None:
        streams.extend(report_text.splitlines())

    for raw in streams:
        line = raw.strip()
        if not line:
            continue

        if _MATCH_RE.match(line):
            matched = True
            continue
        if _NOMATCH_RE.match(line):
            matched = False
            violations.append(Violation(
                code="LVS.MISMATCH",
                severity="error",
                message="Netgen reported a circuit mismatch",
                detail={"raw_line": raw},
            ))
            continue
        # Count lines for devices / nets / pins / properties.
        counted = False
        for rx in _COUNT_RES:
            if (m := rx.match(line)):
                n = int(m.group("n"))
                if n > 0:
                    mismatch_total += n
                    violations.append(Violation(
                        code="LVS.MISMATCH",
                        severity="error",
                        message=line,
                        detail={"count": n, "raw_line": raw},
                    ))
                counted = True
                break
        if counted:
            continue
        if m := _ERROR_RE.match(line):
            violations.append(Violation(
                code="LVS.ERROR",
                severity="error",
                message=m.group("message"),
                detail={"raw_line": raw},
            ))

    if not matched and mismatch_total == 0 and not violations:
        # Netgen ran to completion but emitted neither verdict line nor a
        # count — be conservative and call it a mismatch.
        matched = False
        violations.append(Violation(
            code="LVS.UNKNOWN",
            severity="error",
            message="Netgen produced no match verdict and no mismatch detail",
        ))
    if not matched and mismatch_total == 0 and any(
        v.code == "LVS.MISMATCH" for v in violations
    ):
        # We saw a "do not match" line without an explicit count — at least
        # surface a count of 1 so the gate metric isn't a silent zero.
        mismatch_total = 1

    error_count = sum(1 for v in violations if v.severity == "error")
    metrics: dict[str, float] = {
        "lvs_mismatches": float(mismatch_total),
        "matched": 1.0 if matched else 0.0,
        "errors": float(error_count),
        "violations": float(len(violations)),
    }
    passed = run.returncode == 0 and matched and error_count == 0
    return LVSParse(
        passed=passed,
        matched=matched,
        mismatch_count=mismatch_total,
        violations=violations,
        metrics=metrics,
    )


class NetgenLVSService:
    """Stage the synth netlist + extracted layout netlist, run Netgen LVS,
    return a typed :class:`LVSReport`.

    F6.3 keeps the layout-netlist source pragmatic: the caller supplies the
    extracted netlist bytes inline (typically produced by LibreLane's
    ``extract`` step). Real signoff will plumb this through the artifact
    store; the seam here is just ``layout_netlist_bytes``.
    """

    NAME = "netgen"

    def __init__(
        self,
        *,
        sandbox: SandboxLike,
        store: SqliteArtifactStore,
        version_str: str = "bundled",
        script_filename: str = "lvs.tcl",
        netlist_filename: str = "design.nl.v",
        layout_netlist_filename: str = "extracted.spice",
        report_filename: str = "lvs.rpt",
        setup_file: str = netgen_setup("gf180mcuD"),
        tracer: Tracer | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self._version_str = version_str
        self._script_filename = script_filename
        self._netlist_filename = netlist_filename
        self._layout_netlist_filename = layout_netlist_filename
        self._report_filename = report_filename
        self._setup_file = setup_file
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=self.NAME,
            version=self._version_str,
            container_digest=digest,
        )

    def check_lvs(
        self,
        netlist: NetlistArtifact,
        layout: LayoutArtifact,
        *,
        layout_netlist_bytes: bytes,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        netlist_bytes_override: bytes | None = None,
    ) -> LVSReport:
        """Run Netgen LVS and return a typed report.

        ``netlist_bytes_override`` (F13.1) — when supplied, those bytes
        are staged as the synth-side netlist instead of
        ``store.get_blob(netlist.netlist)``. SIGNOFF passes LibreLane's
        final PDK-mapped netlist so Netgen has a fair shot at matching
        the structurally-mapped extracted SPICE.
        """
        top = top_module or netlist.module_id or netlist.artifact_id.split(".")[-2]
        with self._tracer.span(
            name="tool:netgen_lvs", kind=SpanKind.TOOL,
        ) as span:
            span.set_attribute("tool_name", self.NAME)
            span.set_attribute("design_id", netlist.design_id)
            span.set_attribute("module_id", netlist.module_id or top)
            span.set_attribute("top_module", top)
            netlist_bytes = (
                netlist_bytes_override
                if netlist_bytes_override is not None
                else self.store.get_blob(netlist.netlist)
            )
            with tempfile.TemporaryDirectory(prefix="chip-agent-lvs-") as td:
                work = Path(td)
                (work / self._netlist_filename).write_bytes(netlist_bytes)
                (work / self._layout_netlist_filename).write_bytes(layout_netlist_bytes)
                (work / self._script_filename).write_text(build_lvs_script(
                    netlist_file=self._netlist_filename,
                    layout_netlist_file=self._layout_netlist_filename,
                    top_module=top,
                    setup_file=self._setup_file,
                    output_file=self._report_filename,
                ))
                run = self.sandbox.run(
                    [NETGEN_BIN, "-batch", "source", self._script_filename],
                    mount=work,
                    time_limit_s=time_limit_s,
                    read_only_mount=False,
                )
                report_path = work / self._report_filename
                report_text = (
                    report_path.read_text() if report_path.exists() else None
                )

            parse = parse_netgen_lvs_output(run, report_text=report_text)
            log_ref = self._maybe_put_log(run, report_text=report_text)
            report = self._build_report(
                netlist, layout, parse, top_module=top, log_ref=log_ref,
            )
            span.set_attribute("passed", report.passed)
            span.set_attribute("violation_count", len(report.violations))
            return report

    def _maybe_put_log(
        self, run: ToolRun, *, report_text: str | None,
    ) -> BlobRef | None:
        body_parts: list[str] = []
        if run.stdout:
            body_parts.append("=== stdout ===\n" + run.stdout)
        if run.stderr:
            body_parts.append("=== stderr ===\n" + run.stderr)
        if report_text:
            body_parts.append("=== lvs.rpt ===\n" + report_text)
        if not body_parts:
            return None
        return self.store.put_blob(
            "\n".join(body_parts).encode("utf-8", errors="replace"),
            media_type="text/plain",
        )

    def _build_report(
        self,
        netlist: NetlistArtifact,
        layout: LayoutArtifact,
        parse: LVSParse,
        *,
        top_module: str,
        log_ref: BlobRef | None = None,
    ) -> LVSReport:
        tool = self.version()
        module = netlist.module_id or top_module
        return LVSReport(
            artifact_id=f"{netlist.design_id}.{module}.lvs",
            design_id=netlist.design_id,
            module_id=netlist.module_id,
            passed=parse.passed,
            metrics=parse.metrics,
            violations=parse.violations,
            checker=tool,
            matched=parse.matched,
            mismatch_count=parse.mismatch_count,
            netgen_log=log_ref,
            provenance=Provenance(
                produced_by=Stage.SIGNOFF,
                agent="signoff_specialist",
                tool=tool,
                inputs=[netlist.ref(), layout.ref()],
            ),
        )
