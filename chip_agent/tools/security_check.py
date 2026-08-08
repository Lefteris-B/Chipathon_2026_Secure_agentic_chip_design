"""Structural security checks over a gate-level netlist (F6.4).

F6.4 is intentionally the *stub* version of the security gate — a small set
of cheap structural checks over the Verilog netlist text. The goal is the
fourth conjunct in the signoff gate: a typed :class:`SecurityReport` whose
``gate_ok`` closes when something suspicious shows up. The shipped checks:

* **always-on nets** — ``assign <net> = 1'b1`` (or hex / decimal
  equivalents). Surfaced as ``SECURITY.ALWAYS_ON_NET`` at error severity:
  a net unconditionally tied high is the textbook hardware-backdoor
  primitive and the F6.4 AC fixture.
* **always-off nets** — ``assign <net> = 1'b0`` etc. Advisory only
  (``SECURITY.ALWAYS_OFF_NET`` at warning severity): pulldowns and
  tied-low control ports are common and legitimate.
* **suspicious names** — net or instance identifiers containing
  ``backdoor`` / ``debug_force`` / ``test_force`` / ``scan_bypass``.
  Surfaced as ``SECURITY.SUSPICIOUS_NAME`` at error severity: even
  benign-looking names that match these patterns warrant a human review
  on the F6.5 gate.

The parser is pure of I/O and unit-tested independently. The service
wraps it with the netlist-fetch + provenance plumbing the signoff
driver consumes; there is no sandbox here — the analysis is small enough
to run in-process on the netlist text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from chip_agent.design_state import (
    LayoutArtifact,
    NetlistArtifact,
    Provenance,
    SecurityReport,
    Stage,
    ToolVersion,
    Violation,
)
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = [
    "ALWAYS_OFF_CHECK",
    "ALWAYS_ON_CHECK",
    "DEFAULT_CHECKS",
    "SUSPICIOUS_NAMES",
    "SUSPICIOUS_NAME_CHECK",
    "SecurityCheckService",
    "SecurityParse",
    "parse_netlist_security",
]


ALWAYS_ON_CHECK = "always_on_nets"
ALWAYS_OFF_CHECK = "always_off_nets"
SUSPICIOUS_NAME_CHECK = "suspicious_names"
DEFAULT_CHECKS: tuple[str, ...] = (
    ALWAYS_ON_CHECK,
    ALWAYS_OFF_CHECK,
    SUSPICIOUS_NAME_CHECK,
)

# Net identifiers we treat as a backdoor smell regardless of context.
SUSPICIOUS_NAMES: tuple[str, ...] = (
    "backdoor",
    "debug_force",
    "test_force",
    "scan_bypass",
    "security_bypass",
)

# `assign <net> = 1'b1;` / `1'h1` / `1'd1`. Accept bus-bit suffixes too
# (`assign sig[3] = 1'b1;`) — those are the same pattern the synth flow
# emits when a constant is tied to a wide net.
_ALWAYS_ON_RE = re.compile(
    r"^\s*assign\s+(?P<net>[a-zA-Z_]\w*(?:\[[^\]]+\])?)\s*=\s*"
    r"1\s*'\s*(?:b|h|d)\s*1\s*;",
    re.MULTILINE | re.IGNORECASE,
)
_ALWAYS_OFF_RE = re.compile(
    r"^\s*assign\s+(?P<net>[a-zA-Z_]\w*(?:\[[^\]]+\])?)\s*=\s*"
    r"1\s*'\s*(?:b|h|d)\s*0\s*;",
    re.MULTILINE | re.IGNORECASE,
)
# Identifier-shape regex used for the suspicious-name scan. We word-boundary
# the substring so `backdoor_disable_n` matches but `feedback` does not.
def _name_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"\b\w*{re.escape(name)}\w*\b", re.IGNORECASE)


@dataclass(frozen=True)
class SecurityParse:
    passed: bool
    suspicious_structures: int
    violations: list[Violation]
    metrics: dict[str, float]
    checks_run: list[str]


def parse_netlist_security(
    netlist_text: str, *, checks: tuple[str, ...] = DEFAULT_CHECKS,
) -> SecurityParse:
    """Run the F6.4 structural checks over ``netlist_text``.

    Pure of side effects. Returns a typed parse — the service wraps it into
    a :class:`SecurityReport` with provenance and a content-addressed id.
    """
    violations: list[Violation] = []
    always_on_count = 0
    always_off_count = 0
    suspicious_count = 0
    checks_run: list[str] = []

    if ALWAYS_ON_CHECK in checks:
        checks_run.append(ALWAYS_ON_CHECK)
        for m in _ALWAYS_ON_RE.finditer(netlist_text):
            net = m.group("net")
            always_on_count += 1
            violations.append(Violation(
                code="SECURITY.ALWAYS_ON_NET",
                severity="error",
                message=(
                    f"net {net!r} is unconditionally driven high "
                    f"(always-on signal)"
                ),
                location=net,
                detail={"net": net},
            ))

    if ALWAYS_OFF_CHECK in checks:
        checks_run.append(ALWAYS_OFF_CHECK)
        for m in _ALWAYS_OFF_RE.finditer(netlist_text):
            net = m.group("net")
            always_off_count += 1
            violations.append(Violation(
                code="SECURITY.ALWAYS_OFF_NET",
                severity="warning",  # pulldowns are common — advisory only
                message=(
                    f"net {net!r} is unconditionally tied low"
                ),
                location=net,
                detail={"net": net},
            ))

    if SUSPICIOUS_NAME_CHECK in checks:
        checks_run.append(SUSPICIOUS_NAME_CHECK)
        for name in SUSPICIOUS_NAMES:
            seen: set[str] = set()
            for m in _name_re(name).finditer(netlist_text):
                ident = m.group(0)
                key = ident.lower()
                if key in seen:
                    continue
                seen.add(key)
                suspicious_count += 1
                violations.append(Violation(
                    code="SECURITY.SUSPICIOUS_NAME",
                    severity="error",
                    message=(
                        f"identifier {ident!r} matches the suspicious-name "
                        f"pattern {name!r} (potential backdoor)"
                    ),
                    location=ident,
                    detail={"identifier": ident, "pattern": name},
                ))

    error_count = sum(1 for v in violations if v.severity == "error")
    metrics: dict[str, float] = {
        "always_on_nets": float(always_on_count),
        "always_off_nets": float(always_off_count),
        "suspicious_names": float(suspicious_count),
        "errors": float(error_count),
        "violations": float(len(violations)),
    }
    passed = error_count == 0
    return SecurityParse(
        passed=passed,
        suspicious_structures=always_on_count + suspicious_count,
        violations=violations,
        metrics=metrics,
        checks_run=checks_run,
    )


class SecurityCheckService:
    """Structural security checks over a synth netlist.

    A layout artifact is accepted for provenance — F6.4's checks all live
    on the Verilog netlist text, but future iterations (real layout-time
    antenna / IR / coverage checks) will read both. Passing it now keeps
    the signature stable.
    """

    NAME = "structural_security"

    def __init__(
        self,
        *,
        store: SqliteArtifactStore,
        version_str: str = "0.1.0",
        checks: tuple[str, ...] = DEFAULT_CHECKS,
        tracer: Tracer | None = None,
    ) -> None:
        self.store = store
        self._version_str = version_str
        self._checks = checks
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        return ToolVersion(name=self.NAME, version=self._version_str)

    def check_security(
        self,
        netlist: NetlistArtifact,
        *,
        layout: LayoutArtifact | None = None,
        top_module: str | None = None,
    ) -> SecurityReport:
        """Run the F6.4 checks against ``netlist`` and return a typed report."""
        del top_module  # accepted for signature symmetry with other signoff legs
        with self._tracer.span(
            name="tool:security_check", kind=SpanKind.TOOL,
        ) as span:
            span.set_attribute("tool_name", self.NAME)
            span.set_attribute("design_id", netlist.design_id)
            span.set_attribute("module_id", netlist.module_id or "")
            span.set_attribute("checks", list(self._checks))
            text = self.store.get_blob(netlist.netlist).decode(errors="replace")
            parse = parse_netlist_security(text, checks=self._checks)
            report = self._build_report(netlist, layout, parse)
            span.set_attribute("passed", report.passed)
            span.set_attribute("violation_count", len(report.violations))
            return report

    def _build_report(
        self,
        netlist: NetlistArtifact,
        layout: LayoutArtifact | None,
        parse: SecurityParse,
    ) -> SecurityReport:
        tool = self.version()
        module = netlist.module_id or netlist.artifact_id.split(".")[-2]
        inputs = [netlist.ref()]
        if layout is not None:
            inputs.append(layout.ref())
        return SecurityReport(
            artifact_id=f"{netlist.design_id}.{module}.security",
            design_id=netlist.design_id,
            module_id=netlist.module_id,
            passed=parse.passed,
            metrics=parse.metrics,
            violations=parse.violations,
            checker=tool,
            checks_run=parse.checks_run,
            suspicious_structures=parse.suspicious_structures,
            provenance=Provenance(
                produced_by=Stage.SIGNOFF,
                agent="security_specialist",
                tool=tool,
                inputs=inputs,
            ),
        )
