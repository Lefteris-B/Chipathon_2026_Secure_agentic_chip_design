"""OpenSTA static timing analysis as a typed tool service (F6.3 — STA leg).

Runs an OpenSTA TCL script inside the sandbox that reads liberty, the
gate-level netlist, and an SDC constraints file, then dumps WNS / TNS /
violation counts. The parser pulls those into a :class:`TimingReport`;
the report's ``gate_ok`` inherits from
:class:`~chip_agent.design_state.VerificationArtifact` — PASSED only
when ``returncode == 0`` AND the parser saw no negative slack AND no
error-severity violation rode along.

The constraint surface intentionally stays terse for F6.3: a
clock-period number and an optional inline SDC text. The full set of
LibreLane signoff knobs lives in `librelane.py`; this module's job is
just to drive OpenSTA and turn its output into a typed report.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chip_agent.design_state import (
    CornerTiming,
    MultiCornerSTAReport,
    NetlistArtifact,
    Provenance,
    Stage,
    TimingReport,
    ToolRun,
    ToolVersion,
    Violation,
)
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike
from chip_agent.tools.pdk_paths import liberty_path as liberty_path_for

__all__ = [
    "OPENSTA_BIN",
    "OpenSTAService",
    "STAParse",
    "build_sta_script",
    "default_sdc",
    "parse_multi_corner_sta",
    "parse_sta_output",
]


OPENSTA_BIN = "sta"

# WNS / TNS lines vary in OpenSTA versions. We accept both the standalone
# `wns` / `tns` commands and the `report_wns` / `report_tns` output. Numbers
# are nanoseconds; units may be elided.
_WNS_RE = re.compile(
    r"^(?:wns|worst slack|report_wns)\s*[:=]?\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?:ns)?\s*$",
    re.IGNORECASE,
)
_TNS_RE = re.compile(
    r"^(?:tns|total negative slack|report_tns)\s*[:=]?\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?:ns)?\s*$",
    re.IGNORECASE,
)
# Violation summary lines: "Setup violations: N", "Hold violations: N",
# or the "VIOLATED" markers on `report_checks` output.
_SETUP_RE = re.compile(r"^Setup violations:\s+(?P<n>\d+)\b", re.IGNORECASE)
_HOLD_RE = re.compile(r"^Hold violations:\s+(?P<n>\d+)\b", re.IGNORECASE)
_VIOLATED_PATH_RE = re.compile(
    r"^\s*VIOLATED\b.*\s(?P<slack>-?\d+(?:\.\d+)?)\s*$"
)
_ERROR_RE = re.compile(r"^(?:Error|ERROR):\s*(?P<message>.+?)\s*$")
# F13.3: OpenSTA's read_verilog early-failure shape, observed in the F11.7
# live run as "Error: 171 design.nl.v line 18, syntax error". Pattern is
# ``<errno> <file> line <line>, syntax error`` after the generic ``Error:``
# prefix. We surface this distinctly from the generic STA.ERROR so the
# operator sees "STA.READ_VERILOG_ERROR design.nl.v line 18" in the gate's
# violation list — direct pointer at the offending construct.
_READ_VERILOG_ERROR_RE = re.compile(
    r"^(?:Error|ERROR):\s*"
    r"(?:\d+\s+)?"                          # OpenSTA's internal error code
    r"(?P<file>\S+\.(?:v|sv|nl\.v))\s+"     # netlist file (.v / .sv / .nl.v)
    r"line\s+(?P<line>\d+)\s*,\s*"
    r"(?P<reason>syntax error|read_verilog\s+failed|.+?)\s*$",
    re.IGNORECASE,
)
# F21.2: OpenSTA ``report_power`` line shapes. The canonical output has
# leading-name + columns of mW values; we want the labelled totals only —
# leakage / switching / internal / total. Each pattern matches one
# population line.
_POWER_LEAKAGE_RE = re.compile(
    r"^\s*leakage\s*[:=]?\s*(?P<value>-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(?:mW|W)?\s*$",
    re.IGNORECASE,
)
_POWER_SWITCHING_RE = re.compile(
    r"^\s*switching\s*[:=]?\s*(?P<value>-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(?:mW|W)?\s*$",
    re.IGNORECASE,
)
_POWER_INTERNAL_RE = re.compile(
    r"^\s*internal\s*[:=]?\s*(?P<value>-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(?:mW|W)?\s*$",
    re.IGNORECASE,
)
_POWER_TOTAL_RE = re.compile(
    r"^\s*total\s*[:=]?\s*(?P<value>-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(?:mW|W)?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class STAParse:
    passed: bool
    wns_ns: float | None
    tns_ns: float | None
    setup_violations: int
    hold_violations: int
    violations: list[Violation]
    metrics: dict[str, float]


def default_sdc(*, clock_period_ns: float, clock_port: str = "clk") -> str:
    """Minimal SDC for the F6.3 demo path — one clock, no IO delays.

    Real signoff plugs the full SDC the design ships; this is the floor we
    can drive from a NetlistArtifact alone.
    """
    return (
        f"create_clock -name clk -period {clock_period_ns:g} "
        f"[get_ports {clock_port}]\n"
    )


def build_sta_script(
    *,
    netlist_file: str,
    sdc_file: str,
    liberty_file: str,
    top_module: str,
    sdf_file: str | None = None,
) -> str:
    """Return an OpenSTA TCL script that loads the design + SDC and
    dumps WNS / TNS / violation counts the parser keys off of.

    When ``sdf_file`` is supplied (F12.1: the SDF LibreLane dumped during
    CTS), ``read_sdf`` is appended after ``read_sdc`` so timing analysis
    reflects the post-CTS delays the layout actually has, not the
    pre-layout estimate the netlist alone gives.
    """
    lines = [
        f"read_liberty {liberty_file}",
        f"read_verilog {netlist_file}",
        f"link_design {top_module}",
        f"read_sdc {sdc_file}",
    ]
    if sdf_file is not None:
        lines.append(f"read_sdf {sdf_file}")
    lines += [
        "report_checks -path_delay min_max -format short",
        # Print the slack numbers on their own lines so the parser can grep
        # them with anchored regexes.
        'puts "wns [report_wns]"',
        'puts "tns [report_tns]"',
        "exit",
    ]
    return "\n".join(lines)


def parse_sta_output(run: ToolRun) -> STAParse:
    """Parse OpenSTA stdout/stderr into a typed parse result."""
    wns_ns: float | None = None
    tns_ns: float | None = None
    setup = 0
    hold = 0
    violations: list[Violation] = []

    for raw in (run.stdout.splitlines() + run.stderr.splitlines()):
        line = raw.strip()
        if not line:
            continue

        if (m := _WNS_RE.match(line)) and wns_ns is None:
            wns_ns = float(m.group("value"))
            continue
        if (m := _TNS_RE.match(line)) and tns_ns is None:
            tns_ns = float(m.group("value"))
            continue
        if m := _SETUP_RE.match(line):
            setup = max(setup, int(m.group("n")))
            continue
        if m := _HOLD_RE.match(line):
            hold = max(hold, int(m.group("n")))
            continue
        if m := _VIOLATED_PATH_RE.match(line):
            # A "VIOLATED" path with a negative slack tightens our setup count
            # so a parser that misses the summary line still detects the gate
            # closure.
            slack = float(m.group("slack"))
            if slack < 0:
                setup += 1
                violations.append(Violation(
                    code="STA.SETUP_VIOLATION",
                    severity="error",
                    message=f"setup path violated by {slack:g} ns",
                    detail={"slack_ns": slack, "raw_line": raw},
                ))
            continue
        # F13.3: check the specific read_verilog-error shape BEFORE the
        # generic STA.ERROR fallback so the operator gets a typed pointer
        # at the offending file + line in ``detail`` instead of the
        # un-attributable raw message.
        if m := _READ_VERILOG_ERROR_RE.match(line):
            violations.append(Violation(
                code="STA.READ_VERILOG_ERROR",
                severity="error",
                message=(
                    f"OpenSTA could not parse {m.group('file')} "
                    f"at line {m.group('line')}: {m.group('reason')}"
                ),
                location=f"{m.group('file')}:{m.group('line')}",
                detail={
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "reason": m.group("reason"),
                    "raw_line": raw,
                },
            ))
            continue
        if m := _ERROR_RE.match(line):
            violations.append(Violation(
                code="STA.ERROR",
                severity="error",
                message=m.group("message"),
                detail={"raw_line": raw},
            ))

    # Promote negative slack to typed setup-violation findings even when
    # OpenSTA only printed wns/tns and not the path list.
    if wns_ns is not None and wns_ns < 0 and setup == 0:
        setup = 1
        violations.append(Violation(
            code="STA.SETUP_VIOLATION",
            severity="error",
            message=f"WNS {wns_ns:g} ns is negative",
            detail={"wns_ns": wns_ns},
        ))
    if setup > 0 and not any(v.code == "STA.SETUP_VIOLATION" for v in violations):
        violations.append(Violation(
            code="STA.SETUP_VIOLATION", severity="error",
            message=f"{setup} setup violation(s) reported",
            detail={"setup_violations": setup},
        ))
    if hold > 0:
        violations.append(Violation(
            code="STA.HOLD_VIOLATION", severity="error",
            message=f"{hold} hold violation(s) reported",
            detail={"hold_violations": hold},
        ))

    error_count = sum(1 for v in violations if v.severity == "error")
    metrics: dict[str, float] = {
        "wns_ns": wns_ns if wns_ns is not None else 0.0,
        "tns_ns": tns_ns if tns_ns is not None else 0.0,
        "setup_violations": float(setup),
        "hold_violations": float(hold),
        "errors": float(error_count),
        "violations": float(len(violations)),
    }
    passed = (
        run.returncode == 0
        and error_count == 0
        and (wns_ns is None or wns_ns >= 0)
        and (tns_ns is None or tns_ns >= 0)
        and setup == 0
        and hold == 0
    )
    return STAParse(
        passed=passed,
        wns_ns=wns_ns,
        tns_ns=tns_ns,
        setup_violations=setup,
        hold_violations=hold,
        violations=violations,
        metrics=metrics,
    )


def _parse_one_corner_report(corner: str, body: bytes) -> tuple[CornerTiming, list[Violation]]:
    """F21.2: parse a single per-corner OpenSTA stdout into a CornerTiming.

    Reuses the same line-matchers ``parse_sta_output`` uses (WNS / TNS /
    Setup / Hold / VIOLATED path / read_verilog error / generic STA error).
    Violations carry the corner tag in ``location`` and ``detail['corner']``
    so the downstream MultiCornerSTAReport can attribute each entry. No
    subprocess; no rc — the report came from LibreLane's internal STA run,
    not from a NoeSI sandbox call.
    """
    text = body.decode("utf-8", errors="replace")
    wns_ns: float | None = None
    tns_ns: float | None = None
    setup = 0
    hold = 0
    violations: list[Violation] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if (m := _WNS_RE.match(line)) and wns_ns is None:
            wns_ns = float(m.group("value"))
            continue
        if (m := _TNS_RE.match(line)) and tns_ns is None:
            tns_ns = float(m.group("value"))
            continue
        if m := _SETUP_RE.match(line):
            setup = max(setup, int(m.group("n")))
            continue
        if m := _HOLD_RE.match(line):
            hold = max(hold, int(m.group("n")))
            continue
        if m := _VIOLATED_PATH_RE.match(line):
            slack = float(m.group("slack"))
            if slack < 0:
                setup += 1
                violations.append(Violation(
                    code="STA.SETUP_VIOLATION",
                    severity="error",
                    message=f"setup path violated by {slack:g} ns at corner={corner}",
                    location=f"corner={corner}",
                    detail={"corner": corner, "slack_ns": slack, "raw_line": raw},
                ))
            continue
        # F13.3-equivalent: typed read_verilog error per corner. The
        # location string ``corner=<c>:<file>:<line>`` is what F21.3's
        # tuner will grep for to attribute the failure.
        if m := _READ_VERILOG_ERROR_RE.match(line):
            violations.append(Violation(
                code="STA.READ_VERILOG_ERROR",
                severity="error",
                message=(
                    f"OpenSTA could not parse {m.group('file')} at line "
                    f"{m.group('line')} at corner={corner}: {m.group('reason')}"
                ),
                location=f"corner={corner}:{m.group('file')}:{m.group('line')}",
                detail={
                    "corner": corner,
                    "file": m.group("file"),
                    "line": int(m.group("line")),
                    "reason": m.group("reason"),
                    "raw_line": raw,
                },
            ))
            continue
        if m := _ERROR_RE.match(line):
            violations.append(Violation(
                code="STA.ERROR",
                severity="error",
                message=f"{m.group('message')} (corner={corner})",
                location=f"corner={corner}",
                detail={"corner": corner, "raw_line": raw},
            ))

    # Promotion logic mirrors parse_sta_output: negative WNS without an
    # explicit setup-violations summary still gates the corner; an
    # explicit "Setup violations: N>0" line also gets a typed violation
    # so the SIGNOFF gate sees the same shape regardless of which form
    # OpenSTA used.
    if wns_ns is not None and wns_ns < 0 and setup == 0:
        setup = 1
        violations.append(Violation(
            code="STA.SETUP_VIOLATION",
            severity="error",
            message=f"WNS {wns_ns:g} ns is negative at corner={corner}",
            location=f"corner={corner}",
            detail={"corner": corner, "wns_ns": wns_ns},
        ))
    if setup > 0 and not any(v.code == "STA.SETUP_VIOLATION" for v in violations):
        violations.append(Violation(
            code="STA.SETUP_VIOLATION",
            severity="error",
            message=f"{setup} setup violation(s) reported at corner={corner}",
            location=f"corner={corner}",
            detail={"corner": corner, "setup_violations": setup},
        ))
    if hold > 0:
        violations.append(Violation(
            code="STA.HOLD_VIOLATION",
            severity="error",
            message=f"{hold} hold violation(s) reported at corner={corner}",
            location=f"corner={corner}",
            detail={"corner": corner, "hold_violations": hold},
        ))

    return (
        CornerTiming(
            corner=corner,
            wns_ns=wns_ns,
            tns_ns=tns_ns,
            setup_violations=setup,
            hold_violations=hold,
            power_metrics={},  # populated by caller via _parse_one_power_report
        ),
        violations,
    )


def _parse_one_power_report(body: bytes) -> dict[str, float]:
    """F21.2: line-grep an OpenSTA ``report_power`` blob into a flat dict.

    Returns the populated subset of ``{leakage, switching, internal, total}``
    in mW. Empty dict if no recognisable line is found — graceful fallback
    so an off-shape power blob doesn't break the timing parse.
    """
    text = body.decode("utf-8", errors="replace")
    metrics: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "leakage" not in metrics and (m := _POWER_LEAKAGE_RE.match(line)):
            metrics["leakage"] = float(m.group("value"))
            continue
        if "switching" not in metrics and (m := _POWER_SWITCHING_RE.match(line)):
            metrics["switching"] = float(m.group("value"))
            continue
        if "internal" not in metrics and (m := _POWER_INTERNAL_RE.match(line)):
            metrics["internal"] = float(m.group("value"))
            continue
        if "total" not in metrics and (m := _POWER_TOTAL_RE.match(line)):
            metrics["total"] = float(m.group("value"))
            continue
    return metrics


def parse_multi_corner_sta(
    corner_reports: dict[str, bytes],
    power_reports: dict[str, bytes] | None = None,
    *,
    design_id: str,
    artifact_id: str,
    module_id: str | None = None,
) -> MultiCornerSTAReport:
    """F21.2: build a typed ``MultiCornerSTAReport`` from per-corner blobs.

    Pure parser — no subprocess, no router. Reuses the existing
    OpenSTA line-matchers (``_WNS_RE`` / ``_TNS_RE`` / ``_SETUP_RE`` /
    ``_HOLD_RE`` / ``_READ_VERILOG_ERROR_RE`` / ``_ERROR_RE``) per corner;
    every violation is tagged with the corner so SIGNOFF + F21.3 can
    attribute failures correctly. ``power_reports`` is optional; when
    supplied, the corresponding corner's ``CornerTiming.power_metrics``
    is populated.

    Sorts corners deterministically by tag so the artifact's content hash
    is stable across dict-iteration-order drift between Python runs.

    ``passed`` = all corners' WNS ≥ 0 AND no error-severity violations.
    Inherited ``gate_ok`` adds the no-error check.
    """
    power_reports = power_reports or {}
    corners: list[CornerTiming] = []
    all_violations: list[Violation] = []
    for corner in sorted(corner_reports.keys()):
        timing, vios = _parse_one_corner_report(corner, corner_reports[corner])
        if corner in power_reports:
            timing = timing.model_copy(
                update={"power_metrics": _parse_one_power_report(power_reports[corner])}
            )
        corners.append(timing)
        all_violations.extend(vios)

    error_count = sum(1 for v in all_violations if v.severity == "error")
    all_wns_ok = all(
        c.wns_ns is None or c.wns_ns >= 0 for c in corners
    )
    metrics: dict[str, float] = {
        "corners_checked": float(len(corners)),
        "errors": float(error_count),
        "violations": float(len(all_violations)),
    }
    # Worst-corner aggregates as top-level metrics so F21.3's tuner can read
    # them without walking the corners list.
    parsed_wns = [c.wns_ns for c in corners if c.wns_ns is not None]
    parsed_tns = [c.tns_ns for c in corners if c.tns_ns is not None]
    if parsed_wns:
        metrics["worst_wns_ns"] = min(parsed_wns)
    if parsed_tns:
        metrics["worst_tns_ns"] = min(parsed_tns)
    return MultiCornerSTAReport(
        artifact_id=artifact_id,
        design_id=design_id,
        module_id=module_id,
        corners=corners,
        passed=(all_wns_ok and error_count == 0),
        violations=all_violations,
        metrics=metrics,
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


class OpenSTAService:
    """Run OpenSTA over a synth netlist and return a typed :class:`TimingReport`."""

    NAME = "opensta"

    def __init__(
        self,
        *,
        sandbox: SandboxLike,
        store: SqliteArtifactStore,
        version_str: str = "bundled",
        script_filename: str = "timing.tcl",
        sdc_filename: str = "constraints.sdc",
        sdf_filename: str = "design.sdf",
        netlist_filename: str = "design.nl.v",
        liberty_path: str = liberty_path_for(
            "gf180mcuD", "gf180mcu_fd_sc_mcu7t5v0",
        ),
        tracer: Tracer | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self._version_str = version_str
        self._script_filename = script_filename
        self._sdc_filename = sdc_filename
        self._sdf_filename = sdf_filename
        self._netlist_filename = netlist_filename
        self._liberty_path = liberty_path
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=self.NAME,
            version=self._version_str,
            container_digest=digest,
        )

    def check_timing(
        self,
        netlist: NetlistArtifact,
        *,
        clock_period_ns: float,
        sdc_text: str | None = None,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        sdf_bytes: bytes | None = None,
        netlist_bytes_override: bytes | None = None,
    ) -> TimingReport:
        """Run OpenSTA over ``netlist`` and return a typed report.

        ``sdf_bytes`` (F12.1) — when supplied, the SDF is staged into the
        sandbox work dir and ``read_sdf`` is prepended to the timing pass
        so analysis reflects the post-CTS delays the layout actually has.
        The produced report carries ``metrics["sdf_used"] = 1.0`` so
        downstream gates can tell whether the verdict is post-layout
        (real) or pre-layout (estimate-only).

        ``netlist_bytes_override`` (F13.1) — when supplied, those bytes are
        staged as ``design.nl.v`` instead of ``store.get_blob(netlist.netlist)``.
        SIGNOFF uses this to pass LibreLane's final PDK-mapped netlist
        (from ``layout.librelane_mapped_netlist``) so OpenSTA's
        ``read_verilog`` parses cleanly. The produced report carries
        ``metrics["used_mapped_netlist"] = 1.0`` when the override fires.
        """
        top = top_module or netlist.module_id or netlist.artifact_id.split(".")[-2]
        with self._tracer.span(
            name="tool:opensta_check_timing", kind=SpanKind.TOOL,
        ) as span:
            span.set_attribute("tool_name", self.NAME)
            span.set_attribute("design_id", netlist.design_id)
            span.set_attribute("module_id", netlist.module_id or top)
            span.set_attribute("top_module", top)
            span.set_attribute("clock_period_ns", clock_period_ns)
            span.set_attribute("sdf_used", bool(sdf_bytes))
            sdc = sdc_text if sdc_text is not None else default_sdc(
                clock_period_ns=clock_period_ns,
            )
            netlist_bytes = (
                netlist_bytes_override
                if netlist_bytes_override is not None
                else self.store.get_blob(netlist.netlist)
            )
            used_mapped = netlist_bytes_override is not None
            with tempfile.TemporaryDirectory(prefix="chip-agent-sta-") as td:
                work = Path(td)
                (work / self._netlist_filename).write_bytes(netlist_bytes)
                (work / self._sdc_filename).write_text(sdc)
                sdf_filename: str | None = None
                if sdf_bytes:
                    sdf_filename = self._sdf_filename
                    (work / sdf_filename).write_bytes(sdf_bytes)
                (work / self._script_filename).write_text(build_sta_script(
                    netlist_file=self._netlist_filename,
                    sdc_file=self._sdc_filename,
                    liberty_file=self._liberty_path,
                    top_module=top,
                    sdf_file=sdf_filename,
                ))
                run = self.sandbox.run(
                    [OPENSTA_BIN, "-no_init", "-no_splash",
                     "-exit", self._script_filename],
                    mount=work,
                    time_limit_s=time_limit_s,
                    read_only_mount=False,
                )

            parse = parse_sta_output(run)
            report = self._build_report(
                netlist, parse, top_module=top, sdf_used=bool(sdf_bytes),
                used_mapped_netlist=used_mapped,
            )
            span.set_attribute("passed", report.passed)
            span.set_attribute("wns_ns", report.wns_ns)
            return report

    def _build_report(
        self,
        netlist: NetlistArtifact,
        parse: STAParse,
        *,
        top_module: str,
        sdf_used: bool,
        used_mapped_netlist: bool = False,
    ) -> TimingReport:
        tool = self.version()
        module = netlist.module_id or top_module
        # F12.1: record whether the gate ran against a real SDF so callers
        # can distinguish post-CTS truth from pre-layout estimate.
        metrics = dict(parse.metrics)
        metrics["sdf_used"] = 1.0 if sdf_used else 0.0
        # F13.1: record whether STA read LibreLane's sky130-mapped netlist
        # (real signoff input) or the intermediate Yosys netlist (estimate
        # only, frequently un-parseable on docker).
        metrics["used_mapped_netlist"] = 1.0 if used_mapped_netlist else 0.0
        return TimingReport(
            artifact_id=f"{netlist.design_id}.{module}.timing",
            design_id=netlist.design_id,
            module_id=netlist.module_id,
            passed=parse.passed,
            metrics=metrics,
            violations=parse.violations,
            checker=tool,
            wns_ns=parse.wns_ns,
            tns_ns=parse.tns_ns,
            setup_violations=parse.setup_violations,
            hold_violations=parse.hold_violations,
            provenance=Provenance(
                produced_by=Stage.SIGNOFF,
                agent="signoff_specialist",
                tool=tool,
                inputs=[netlist.ref()],
            ),
        )
