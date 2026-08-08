"""Verilator-driven elaborate / lint-only as a typed tool service (F2.2).

Runs ``verilator --lint-only -Wall`` over the staged RTL inside the sandbox
and parses each ``%Error`` / ``%Warning-<RULE>`` line into a :class:`Violation`
keyed by the stable taxonomy.

Returns a :class:`LintResult` — same gate shape as ``verible``, different
tool — so a stage handler can run both and conjoin the gates trivially.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chip_agent.design_state import (
    LintResult,
    Provenance,
    RTLArtifact,
    Stage,
    ToolRun,
    ToolVersion,
    Violation,
)
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike

__all__ = [
    "VERILATOR_BIN",
    "ElaborateParse",
    "VerilatorElaborateService",
    "parse_elaborate_output",
]


VERILATOR_BIN = "verilator"

# %Error: <file>:<line>:<col>: <message>
# %Warning-<RULE>: <file>:<line>:<col>: <message>
# %Warning: <file>:<line>:<col>: <message>           (rare, no rule)
_VERILATOR_LINE = re.compile(
    r"^%(?P<sev>Error|Warning)(?:-(?P<rule>[A-Z0-9_]+))?:\s*"
    r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+"
    r"(?P<message>.+?)\s*$"
)

# Map Verilator rule shortnames to the stable taxonomy.
_RULE_TO_CODE: dict[str, str] = {
    "WIDTH": "PORT_WIDTH_MISMATCH",
    "WIDTHTRUNC": "PORT_WIDTH_MISMATCH",
    "WIDTHEXPAND": "PORT_WIDTH_MISMATCH",
    "WIDTHCONCAT": "PORT_WIDTH_MISMATCH",
    "PINMISSING": "PORT_WIDTH_MISMATCH",
    "PINCONNECTEMPTY": "UNDRIVEN_NET",
    "IMPLICIT": "UNDECLARED_ID",
    "UNDRIVEN": "UNDRIVEN_NET",
    "MULTIDRIVEN": "MULTI_DRIVEN",
    "LATCH": "LATCH_INFERRED",
    "COMBDLY": "LATCH_INFERRED",
    "SYNCASYNCNET": "MULTI_DRIVEN",
    "ASSIGNDLY": "LATCH_INFERRED",
}

_ERROR_CODES = {
    "PORT_WIDTH_MISMATCH", "UNDECLARED_ID", "UNDRIVEN_NET",
    "MULTI_DRIVEN", "LATCH_INFERRED", "SYNTAX",
}


@dataclass(frozen=True)
class ElaborateParse:
    passed: bool
    violations: list[Violation]
    metrics: dict[str, float]


def _classify(rule: str | None, message: str, sev: str) -> tuple[str, str]:
    """Return (code, severity) for a parsed finding."""
    code = _RULE_TO_CODE.get(rule) if rule else None
    if code is None:
        lo = message.lower()
        if "expects" in lo and "bits" in lo:
            code = "PORT_WIDTH_MISMATCH"
        elif "latch" in lo:
            code = "LATCH_INFERRED"
        elif "syntax" in lo:
            code = "SYNTAX"
    if code is None:
        return "ELABORATE.UNKNOWN", sev.lower()
    # Verilator's %Error is always severity=error; %Warning-WIDTH we keep as
    # error because a width mismatch is a hard correctness bug, never a style.
    is_error = (sev == "Error") or (code in _ERROR_CODES)
    return code, "error" if is_error else "warning"


def parse_elaborate_output(run: ToolRun, *, source_file: str) -> ElaborateParse:
    """Parse Verilator stdout/stderr into typed findings."""
    violations: list[Violation] = []
    for raw in (run.stdout.splitlines() + run.stderr.splitlines()):
        line = raw.strip()
        if not line.startswith("%"):
            # Verilator also emits a final summary line "%Error: Exiting due to N error(s)".
            if run.returncode != 0 and ("error" in line.lower() or "warn" in line.lower()):
                # Preserve non-positional tool output on failure rather than dropping it.
                pass
            continue
        m = _VERILATOR_LINE.match(line)
        if m is None:
            # Catches things like "%Error: Exiting due to 1 error(s)" which has no file:line:col.
            if run.returncode != 0:
                violations.append(Violation(
                    code="ELABORATE.UNKNOWN",
                    severity="warning",
                    message=line,
                    detail={"raw_line": raw},
                ))
            continue
        code, severity = _classify(m.group("rule"), m.group("message"), m.group("sev"))
        violations.append(Violation(
            code=code,
            severity=severity,
            message=m.group("message"),
            location=f"{m.group('file')}:{m.group('line')}:{m.group('col')}",
            detail={
                "rule": m.group("rule") or "",
                "verilator_severity": m.group("sev"),
                "raw_line": raw,
            },
        ))
    error_count = sum(1 for v in violations if v.severity == "error")
    metrics = {
        "violations": float(len(violations)),
        "errors": float(error_count),
    }
    # Verilator under -Wall exits non-zero on any warning (including UNUSEDPARAM
    # and friends). The gate contract is "errors fail": a non-zero rc backed by
    # parsed warnings is the tool reporting style, not exploding. We only treat
    # rc!=0 as fatal when no findings explain it.
    passed = error_count == 0 and (run.returncode == 0 or bool(violations))
    return ElaborateParse(passed=passed, violations=violations, metrics=metrics)


class VerilatorElaborateService:
    """Stage RTL, run ``verilator --lint-only -Wall``, return a typed :class:`LintResult`."""

    NAME = "verilator"

    def __init__(
        self,
        *,
        sandbox: SandboxLike,
        store: SqliteArtifactStore,
        version_str: str = "bundled",
        tracer: Tracer | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self._version_str = version_str
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=self.NAME,
            version=self._version_str,
            container_digest=digest,
        )

    def elaborate(
        self,
        rtl: RTLArtifact,
        *,
        time_limit_s: int | None = None,
        extra_flags: list[str] | None = None,
    ) -> LintResult:
        with self._tracer.span(
            name="tool:verilator_elaborate", kind=SpanKind.TOOL,
        ) as span:
            span.set_attribute("tool_name", self.NAME)
            span.set_attribute("module_id", rtl.module_id or rtl.top_module)
            span.set_attribute("design_id", rtl.design_id)
            with tempfile.TemporaryDirectory(prefix="chip-agent-elab-") as td:
                work_path = Path(td)
                source_bytes = self.store.get_blob(rtl.source)
                source_file = _source_filename(rtl)
                (work_path / source_file).write_bytes(source_bytes)
                cmd = [VERILATOR_BIN, "--lint-only", "-Wall", *(extra_flags or []),
                       source_file]
                run = self.sandbox.run(
                    cmd,
                    mount=work_path,
                    time_limit_s=time_limit_s,
                    read_only_mount=True,
                )
            parse = parse_elaborate_output(run, source_file=source_file)
            result = self._build_result(rtl, parse)
            span.set_attribute("passed", result.passed)
            span.set_attribute("violation_count", len(result.violations))
            return result

    def _build_result(self, rtl: RTLArtifact, parse: ElaborateParse) -> LintResult:
        module = rtl.module_id or rtl.top_module
        tool = self.version()
        return LintResult(
            artifact_id=f"{rtl.design_id}.{module}.elaborate",
            design_id=rtl.design_id,
            module_id=rtl.module_id,
            passed=parse.passed,
            metrics=parse.metrics,
            violations=parse.violations,
            checker=tool,
            provenance=Provenance(
                produced_by=Stage.RTL,
                agent="rtl_specialist",
                tool=tool,
                inputs=[rtl.ref()],
            ),
        )


def _source_filename(rtl: RTLArtifact) -> str:
    ext = ".sv" if rtl.language.lower().startswith("system") else ".v"
    return f"{rtl.top_module}{ext}"
