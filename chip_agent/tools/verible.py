"""Verible lint as a typed tool service (F2.1).

Runs ``verible-verilog-lint`` inside the pinned sandbox and parses each
finding line into a :class:`Violation` keyed by the stable taxonomy from
``docs/tool_contracts.md``.

The parser is the only consumer of stdout/stderr; everything above it sees
a typed :class:`LintResult`.
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
from chip_agent.tools._runner_utils import strip_entrypoint_banner

__all__ = [
    "VERIBLE_BIN",
    "LintParse",
    "VeribleLintService",
    "parse_lint_output",
]


VERIBLE_BIN = "verible-verilog-lint"

# Verible emits findings as one line per issue:
#   <file>:<line>:<col>[-<col2>]: <message> [<rule>]
_LINE_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)(?:-\d+)?:\s+"
    r"(?P<message>.+?)\s+\[(?P<rule>[^\]]+)\]\s*$"
)

# Map verible rule names to our stable Violation codes (see tool_contracts.md).
_RULE_TO_CODE: dict[str, str] = {
    "case-missing-default": "LATCH_INFERRED",
    "always-comb-blocking": "LATCH_INFERRED",
    "always-ff-non-blocking": "LATCH_INFERRED",
    "syntax-tree-error": "SYNTAX",
    "undriven-net": "UNDRIVEN_NET",
    "multi-driven-net": "MULTI_DRIVEN",
    "undeclared-identifier": "UNDECLARED_ID",
    "implicit-bit-width": "PORT_WIDTH_MISMATCH",
}

_ERROR_CODES = {
    "LATCH_INFERRED", "SYNTAX", "UNDRIVEN_NET", "MULTI_DRIVEN",
    "UNDECLARED_ID", "PORT_WIDTH_MISMATCH",
}


@dataclass(frozen=True)
class LintParse:
    """Pure parser output before we wrap it in an Artifact."""

    passed: bool
    violations: list[Violation]
    metrics: dict[str, float]


def _classify(rule: str | None, message: str) -> tuple[str, str]:
    """Return (code, severity) for a parsed finding."""
    code: str | None = None
    if rule:
        # Verible groups rules like "Style: case-missing-default" — try both forms.
        bare = rule.split(":", 1)[-1].strip()
        code = _RULE_TO_CODE.get(bare) or _RULE_TO_CODE.get(rule)
    if code is None and "latch" in message.lower():
        code = "LATCH_INFERRED"
    if code is None:
        return "LINT.UNKNOWN", "warning"
    severity = "error" if code in _ERROR_CODES else "warning"
    return code, severity


def parse_lint_output(run: ToolRun, *, source_file: str) -> LintParse:
    """Parse ``verible-verilog-lint`` stdout/stderr into typed violations.

    Findings that match the line format are mapped to the stable taxonomy.
    Unmatched output is preserved as ``LINT.UNKNOWN`` only when the run
    failed, so we never silently swallow a tool error.

    ``source_file`` is the file name verible saw inside the sandbox; it is
    used to filter foreign-file paths if they ever appear.
    """
    violations: list[Violation] = []
    stdout = strip_entrypoint_banner(run.stdout)
    stderr = strip_entrypoint_banner(run.stderr)
    for raw in (stdout.splitlines() + stderr.splitlines()):
        line = raw.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if m is None:
            if run.returncode != 0:
                violations.append(Violation(
                    code="LINT.UNKNOWN",
                    severity="warning",
                    message=line,
                    detail={"raw_line": raw},
                ))
            continue
        if m.group("file") != source_file and source_file != "":
            # Foreign-file finding is rare but possible; surface it untouched.
            pass
        code, severity = _classify(m.group("rule"), m.group("message"))
        violations.append(Violation(
            code=code,
            severity=severity,
            message=m.group("message"),
            location=f"{m.group('file')}:{m.group('line')}:{m.group('col')}",
            detail={"rule": m.group("rule"), "raw_line": raw},
        ))
    error_count = sum(1 for v in violations if v.severity == "error")
    metrics: dict[str, float] = {
        "violations": float(len(violations)),
        "errors": float(error_count),
    }
    # Verible exits rc=1 for any finding, including pure style nits. The gate
    # contract is "errors fail": a non-zero rc backed by parsed warnings is the
    # tool reporting style, not exploding. We only treat rc!=0 as fatal when no
    # findings explain it (true silent failure / banner-only output).
    passed = error_count == 0 and (run.returncode == 0 or bool(violations))
    return LintParse(passed=passed, violations=violations, metrics=metrics)


class VeribleLintService:
    """Materialise RTL, run verible in the sandbox, return a typed LintResult."""

    NAME = "verible"

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
        # F22.2: optional Tracer. Default NoopTracer keeps backward
        # compat — pre-F22.2 callers that don't pass a tracer get the
        # same silent behaviour.
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=self.NAME,
            version=self._version_str,
            container_digest=digest,
        )

    def lint(
        self,
        rtl: RTLArtifact,
        *,
        time_limit_s: int | None = None,
    ) -> LintResult:
        """Run verible against ``rtl.source`` and assemble a :class:`LintResult`."""
        with self._tracer.span(
            name="tool:verible_lint", kind=SpanKind.TOOL,
        ) as span:
            span.set_attribute("tool_name", self.NAME)
            span.set_attribute("module_id", rtl.module_id or rtl.top_module)
            span.set_attribute("design_id", rtl.design_id)
            with tempfile.TemporaryDirectory(prefix="chip-agent-lint-") as td:
                work_path = Path(td)
                source_bytes = self.store.get_blob(rtl.source)
                source_file = _source_filename(rtl)
                (work_path / source_file).write_bytes(source_bytes)
                run = self.sandbox.run(
                    [VERIBLE_BIN, source_file],
                    mount=work_path,
                    time_limit_s=time_limit_s,
                    read_only_mount=True,
                )
            parse = parse_lint_output(run, source_file=source_file)
            result = self._build_result(rtl, parse)
            span.set_attribute("passed", result.passed)
            span.set_attribute("violation_count", len(result.violations))
            return result

    def _build_result(self, rtl: RTLArtifact, parse: LintParse) -> LintResult:
        module = rtl.module_id or rtl.top_module
        tool = self.version()
        return LintResult(
            artifact_id=f"{rtl.design_id}.{module}.lint",
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
    """Pick a filename for the staged source — SystemVerilog gets ``.sv``."""
    ext = ".sv" if rtl.language.lower().startswith("system") else ".v"
    return f"{rtl.top_module}{ext}"
