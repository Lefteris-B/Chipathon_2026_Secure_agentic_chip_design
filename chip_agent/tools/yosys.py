"""Yosys synthesis as a typed tool service (F6.1).

Runs an open-source synth script through Yosys inside the sandbox and parses
the stdout for the F6.1 AC signals:

* ``Latch inferred for signal …``        — surfaced as ``LATCH_INFERRED``
* ``Re-definition of module …``,
  ``module … is blackbox …``,
  ``Found blackbox module …``           — surfaced as ``BLACKBOX_MODULE``
* ``Number of cells: N``                  — recorded as the ``cells`` metric
* any ``ERROR: …`` / ``Warning: …`` line  — surfaced as a generic violation

Returns ``(NetlistArtifact, SynthesisReport)`` so a stage handler can write
both into the content-addressed store atomically. The synth report inherits
:meth:`~chip_agent.design_state.VerificationArtifact.gate_ok` from
``VerificationArtifact``: PASSED only when the run returned 0 AND no
error-severity violation was found. ``LATCH_INFERRED`` and
``BLACKBOX_MODULE`` are error-severity by default — both are correctness
defects the inner loop must address before SYNTH can hand off to PHYSICAL.

Parser and runner are split so unit tests can feed canned Yosys logs in
without spinning Docker, and so an integration test can swap in a real
sandbox.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chip_agent.design_state import (
    BlobRef,
    NetlistArtifact,
    Provenance,
    RTLArtifact,
    Stage,
    SynthesisReport,
    ToolRun,
    ToolVersion,
    Violation,
)
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike

__all__ = [
    "YOSYS_BIN",
    "YosysParse",
    "YosysSynthError",
    "YosysSynthService",
    "build_synth_script",
    "parse_yosys_output",
]


YOSYS_BIN = "yosys"

# Yosys log shapes the parser cares about. Anchored to the start of stripped
# lines; we run the source over both stdout and stderr because Yosys writes
# warnings to both depending on the pass.
_LATCH_RE = re.compile(
    r"^(?:Warning:\s*)?Latch inferred for signal\s+`?(?P<signal>[^']+?)'?"
    r"(?:\s+from process\s+`?(?P<process>[^']+?)'?\.?)?\s*$",
    re.IGNORECASE,
)
_BLACKBOX_RES = (
    re.compile(
        r"^(?:Warning:\s*)?Found blackbox\s+module\s+`?(?P<module>[^']+?)'?\.?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:Warning:\s*)?Blackbox module\s+`?(?P<module>[^']+?)'?\s+instantiated",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:Warning:\s*)?Module\s+`?(?P<module>[^']+?)'?\s+is\s+blackbox\b",
        re.IGNORECASE,
    ),
)
_ERROR_RE = re.compile(r"^ERROR:\s*(?P<message>.+?)\s*$")
_WARN_RE = re.compile(r"^Warning:\s*(?P<message>.+?)\s*$")
_CELLS_RE = re.compile(r"^Number of cells:\s+(?P<n>\d+)\b")
_WIRES_RE = re.compile(r"^Number of wires:\s+(?P<n>\d+)\b")
# Yosys ≥0.4x rewrote ``stat`` output to a count-first, label-after shape with
# no colon, e.g. ``22 cells`` / ``11 wires`` (plus auxiliary lines like
# ``32 wire bits``, ``3 public wires`` that the strict end-anchor rejects).
# Both formats now coexist so the parser stays portable across IIC image bumps.
_CELLS_RE_NEW = re.compile(r"^(?P<n>\d+)\s+cells$")
_WIRES_RE_NEW = re.compile(r"^(?P<n>\d+)\s+wires$")


@dataclass(frozen=True)
class YosysParse:
    passed: bool
    violations: list[Violation]
    metrics: dict[str, float]
    cell_count: int
    inferred_latches: int
    blackbox_modules: list[str]


class YosysSynthError(RuntimeError):
    """Yosys ran but produced no usable netlist (e.g. write_verilog stage failed)."""


def build_synth_script(
    *,
    source_file: str,
    top_module: str,
    netlist_file: str,
    language: str = "verilog",
) -> str:
    """Return a Yosys script that elaborates ``source_file`` and writes a
    gate-level netlist to ``netlist_file``.

    Kept minimal on purpose: a real LibreLane flow tunes far more passes,
    but the F6.1 inner loop only needs Yosys's ``synth`` to surface
    inferred-latch / blackbox / cell-count signals.

    The reader is always invoked with ``-sv`` regardless of the
    ``language`` field on the source RTL. SystemVerilog mode in Yosys is
    a strict superset of Verilog-2001 — V-2001 code parses unchanged —
    and LLM-generated RTL routinely mixes dialects (e.g. ``always_ff``
    inside a module labelled ``language="verilog"``). Strict V-2001
    mode silently *drops* unrecognised constructs and emits an empty
    netlist with ``cell_count=0``, which surfaces only as a
    no-violations / no-cells gate failure at SYNTH — extremely confusing
    to triage. Always-``-sv`` removes the trap. The ``language`` field
    is still honoured for file-extension choice elsewhere in the wrapper.
    """
    return "\n".join([
        f"read_verilog -sv {source_file}",
        f"hierarchy -check -top {top_module}",
        f"synth -top {top_module}",
        "stat",
        f"write_verilog -noattr {netlist_file}",
    ])


def _strip_yosys_name(name: str) -> str:
    """Yosys decorates identifiers with leading ``\\``; strip it for reporting."""
    return name.lstrip("\\")


def parse_yosys_output(run: ToolRun) -> YosysParse:
    """Parse Yosys stdout/stderr into typed findings.

    Latches and blackboxes are error-severity (they block ``gate_ok``);
    other warnings ride at ``warning`` severity and are advisory only.
    """
    violations: list[Violation] = []
    blackbox_modules: list[str] = []
    inferred_latches = 0
    cell_count = 0
    wire_count = 0

    for raw in (run.stdout.splitlines() + run.stderr.splitlines()):
        line = raw.strip()
        if not line:
            continue

        if m := (_CELLS_RE.match(line) or _CELLS_RE_NEW.match(line)):
            cell_count = max(cell_count, int(m.group("n")))
            continue
        if m := (_WIRES_RE.match(line) or _WIRES_RE_NEW.match(line)):
            wire_count = max(wire_count, int(m.group("n")))
            continue

        if m := _LATCH_RE.match(line):
            signal = _strip_yosys_name(m.group("signal"))
            inferred_latches += 1
            violations.append(Violation(
                code="LATCH_INFERRED",
                severity="error",
                message=f"Yosys inferred a latch on signal {signal!r}",
                location=signal,
                detail={"signal": signal, "raw_line": raw},
            ))
            continue

        blackbox_hit = False
        for rx in _BLACKBOX_RES:
            m = rx.match(line)
            if m is None:
                continue
            module = _strip_yosys_name(m.group("module"))
            if module not in blackbox_modules:
                blackbox_modules.append(module)
            violations.append(Violation(
                code="BLACKBOX_MODULE",
                severity="error",
                message=f"Yosys treated module {module!r} as a blackbox (no body found)",
                location=module,
                detail={"module": module, "raw_line": raw},
            ))
            blackbox_hit = True
            break
        if blackbox_hit:
            continue

        if m := _ERROR_RE.match(line):
            violations.append(Violation(
                code="SYNTH.ERROR",
                severity="error",
                message=m.group("message"),
                detail={"raw_line": raw},
            ))
            continue
        if m := _WARN_RE.match(line):
            # Skip the warning lines we've already classified above.
            violations.append(Violation(
                code="SYNTH.WARNING",
                severity="warning",
                message=m.group("message"),
                detail={"raw_line": raw},
            ))

    error_count = sum(1 for v in violations if v.severity == "error")
    metrics: dict[str, float] = {
        "violations": float(len(violations)),
        "errors": float(error_count),
        "cells": float(cell_count),
        "wires": float(wire_count),
        "inferred_latches": float(inferred_latches),
        "blackbox_modules": float(len(blackbox_modules)),
    }
    # cell_count > 0 is mandatory: a real design always synthesises to at
    # least its register cells. Zero-cell + clean-rc almost always means
    # the ``stat`` output was suppressed (e.g. ``yosys -q``) and the gate
    # would silently rubber-stamp an unmapped netlist that PHYSICAL can't
    # place — exactly the trap that landed the spine at await_human after
    # synth promoted in 1s and physical aborted in 2s. Fails closed.
    passed = run.returncode == 0 and error_count == 0 and cell_count > 0
    return YosysParse(
        passed=passed,
        violations=violations,
        metrics=metrics,
        cell_count=cell_count,
        inferred_latches=inferred_latches,
        blackbox_modules=blackbox_modules,
    )


class YosysSynthService:
    """Stage RTL, run Yosys ``synth -top <top>``, return a typed pair.

    The service is a thin wrapper around the parser + sandbox seam, so unit
    tests can run :func:`parse_yosys_output` directly and the service tests
    can inject a stub sandbox to assert the staging / artifact wiring.
    """

    NAME = "yosys"

    def __init__(
        self,
        *,
        sandbox: SandboxLike,
        store: SqliteArtifactStore,
        version_str: str = "bundled",
        script_filename: str = "synth.ys",
        netlist_filename: str = "netlist.v",
        tracer: Tracer | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self._version_str = version_str
        self._script_filename = script_filename
        self._netlist_filename = netlist_filename
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=self.NAME,
            version=self._version_str,
            container_digest=digest,
        )

    def synthesize(
        self,
        rtl: RTLArtifact,
        *,
        std_cell_lib: str = "gf180mcu_fd_sc_mcu7t5v0",
        time_limit_s: int | None = None,
    ) -> tuple[NetlistArtifact, SynthesisReport]:
        """Run Yosys synth on ``rtl`` and return ``(netlist, report)``.

        The netlist is staged to the content-addressed blob store; the
        report carries the gate-input metrics + violations the control
        graph reads via ``gate_ok``.
        """
        with self._tracer.span(
            name="tool:yosys_synthesize", kind=SpanKind.TOOL,
        ) as span:
            span.set_attribute("tool_name", self.NAME)
            span.set_attribute("design_id", rtl.design_id)
            span.set_attribute("module_id", rtl.module_id or rtl.top_module)
            span.set_attribute("std_cell_lib", std_cell_lib)
            source_file = _source_filename(rtl)
            netlist_file = self._netlist_filename

            with tempfile.TemporaryDirectory(prefix="chip-agent-synth-") as td:
                work_path = Path(td)
                (work_path / source_file).write_bytes(self.store.get_blob(rtl.source))
                script = build_synth_script(
                    source_file=source_file,
                    top_module=rtl.top_module,
                    netlist_file=netlist_file,
                    language=rtl.language,
                )
                (work_path / self._script_filename).write_text(script)

                # Do NOT pass ``-q``: it silences the ``stat`` command's
                # cell-count output, which is what the parser keys off to
                # surface a non-zero ``cell_count``. With ``-q`` the parser
                # only ever sees rc=0 + no errors and rubber-stamps an
                # unmapped netlist that PHYSICAL can't place.
                run = self.sandbox.run(
                    [YOSYS_BIN, "-s", self._script_filename],
                    mount=work_path,
                    time_limit_s=time_limit_s,
                    read_only_mount=False,
                )
                parse = parse_yosys_output(run)

                netlist_path = work_path / netlist_file
                netlist_bytes = (
                    netlist_path.read_bytes() if netlist_path.exists() else b""
                )

            if parse.passed and not netlist_bytes:
                raise YosysSynthError(
                    f"yosys reported success for {rtl.top_module!r} "
                    f"but wrote no netlist"
                )

            netlist_ref = self.store.put_blob(
                netlist_bytes, media_type="text/x-verilog",
            )
            netlist = self._build_netlist(
                rtl, netlist_ref, std_cell_lib=std_cell_lib,
                cell_count=parse.cell_count,
            )
            report = self._build_report(rtl, parse)
            span.set_attribute("passed", report.passed)
            span.set_attribute("cell_count", report.cell_count)
            return netlist, report

    def _build_netlist(
        self,
        rtl: RTLArtifact,
        netlist_ref: BlobRef,
        *,
        std_cell_lib: str,
        cell_count: int,
    ) -> NetlistArtifact:
        module = rtl.module_id or rtl.top_module
        tool = self.version()
        return NetlistArtifact(
            artifact_id=f"{rtl.design_id}.{module}.netlist",
            design_id=rtl.design_id,
            module_id=rtl.module_id,
            netlist=netlist_ref,
            std_cell_lib=std_cell_lib,
            cell_count=cell_count,
            provenance=Provenance(
                produced_by=Stage.SYNTH,
                agent="synth_specialist",
                tool=tool,
                inputs=[rtl.ref()],
            ),
        )

    def _build_report(self, rtl: RTLArtifact, parse: YosysParse) -> SynthesisReport:
        module = rtl.module_id or rtl.top_module
        tool = self.version()
        return SynthesisReport(
            artifact_id=f"{rtl.design_id}.{module}.synth_report",
            design_id=rtl.design_id,
            module_id=rtl.module_id,
            passed=parse.passed,
            metrics=parse.metrics,
            violations=parse.violations,
            checker=tool,
            cell_count=parse.cell_count,
            inferred_latches=parse.inferred_latches,
            provenance=Provenance(
                produced_by=Stage.SYNTH,
                agent="synth_specialist",
                tool=tool,
                inputs=[rtl.ref()],
            ),
        )


def _source_filename(rtl: RTLArtifact) -> str:
    ext = ".sv" if rtl.language.lower().startswith("system") else ".v"
    return f"{rtl.top_module}{ext}"
