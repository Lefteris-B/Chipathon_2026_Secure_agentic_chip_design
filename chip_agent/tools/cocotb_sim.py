"""cocotb-driven simulation as a typed tool service (F2.3).

Stages the RTL + a cocotb testbench into a sandbox mount, writes a tiny
driver script that calls ``cocotb_tools.runner``, runs it in the pinned
image, then parses cocotb's JUnit-style ``results.xml`` into a typed
:class:`SimulationResult`. The VCD waveform, if produced, is stored as a
content-addressed blob via the artifact store.

The parser is the only consumer of cocotb output.
"""

from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from chip_agent.design_state import (
    BlobRef,
    Provenance,
    RTLArtifact,
    SimulationResult,
    Stage,
    TestbenchArtifact,
    ToolRun,
    ToolVersion,
    Violation,
)
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike

__all__ = [
    "DEFAULT_SIMULATOR",
    "RESULTS_XML",
    "RUNNER_SCRIPT",
    "SimParse",
    "SimulationService",
    "extract_diff_token",
    "extract_failure_block",
    "parse_sim_results",
]


DEFAULT_SIMULATOR = "verilator"   # icarus also fine; both ship in iic-osic-tools
RESULTS_XML = "results.xml"
WAVEFORM_VCD = "dump.vcd"
RUNNER_SCRIPT = "run_sim.py"


# Cocotb's default log formatter emits ``<time>ns <LEVEL> <name> <message>``;
# multi-line log records' continuation lines are NOT re-prefixed with a
# timestamp, so this regex anchors only true log headers.
_LOG_HEADER_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*ns\s+(?:INFO|WARNING|ERROR|DEBUG|CRITICAL)\b"
)

# F19.8b: the WARNING regression announcement cocotb emits BEFORE the
# ERROR + traceback. Cocotb's runner sometimes pulls this line into the
# JUnit ``<failure>`` element's body instead of the AssertionError —
# trapping the parser into treating "<name> failed" as the entire failure
# signal. When the XML body matches this shape AND we have stdout/stderr
# available, we fall through to :func:`extract_failure_block` to recover
# the structured traceback (which carries the F19.8 DIFF token).
_WARNING_PREAMBLE_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*ns\s+WARNING\b.*\bfailed\b",
    re.IGNORECASE,
)

# F19.8b: the canonical F19.8 differential-TB assertion token shape.
# We promote any DIFF token we find in stdout/stderr to the front of
# ``failing_assertions`` so the F20.6 trace parser sees it first even
# when the XML body / extraction fell back to the WARNING preamble.
_DIFF_TOKEN_RE = re.compile(
    r"DIFF\|cycle=\d+\|signal=[\w.]+\|expected=-?\d+\|actual=-?\d+",
)


def extract_diff_token(text: str) -> str | None:
    """F19.8b: return the first ``DIFF|...`` token in ``text``, else None.

    The token shape is the canonical message :mod:`chip_agent.agents.differential_tb`
    emits on assertion failure
    (``DIFF|cycle=N|signal=S|expected=E|actual=A``). When we recover this
    from stdout/stderr the F20.6 trace parser can populate
    :attr:`FailureDiagnosis.cycle` / ``failing_signal`` / ``expected`` /
    ``actual`` from the structured fields rather than the looser
    fallback patterns.
    """
    if not text:
        return None
    m = _DIFF_TOKEN_RE.search(text)
    return m.group(0) if m else None


def extract_failure_block(stdout: str, test_name: str, classname: str = "") -> str:
    """Find the cocotb traceback block for ``test_name`` in ``stdout``.

    Cocotb (≥1.7) sometimes writes the JUnit ``<failure/>`` element with no
    body and no ``message=`` attribute. The actual assertion message + Python
    traceback always lands in the simulator stdout via cocotb's logger. This
    helper recovers it: anchor on the ``Test Failed:`` / ``<name> failed``
    log header that names this test, then capture forward until the next
    real log header (continuation lines without timestamps are kept).

    Returns ``""`` when no anchor is found — the caller falls back to the
    pre-existing ``"test failed without a message"`` default.
    """
    if not stdout or not test_name:
        return ""
    qualified = f"{classname}.{test_name}" if classname else test_name
    # Anchors ordered most-specific first; the qualified form is preferred so
    # we don't accidentally capture a block for a sibling test that shares a
    # common name fragment. Critically: iterate PATTERNS at the outer loop
    # and LINES at the inner. The earlier line-outer / pattern-inner shape
    # let cocotb's WARNING preamble line (which matches the generic ``<name>
    # failed`` anchor) win over the ERROR + ``Test Failed:`` line that
    # actually carries the traceback. The UART RX run produced exactly that
    # shape: nl_summary captured the WARNING string alone, the outer-loop
    # model had no assertion message to reason over, and the repair attempts
    # spiralled into prose-into-RTL garbage.
    test_failed_anchors = [
        re.compile(rf"Test Failed:\s*{re.escape(qualified)}\b"),
        re.compile(rf"Test Failed:\s*{re.escape(test_name)}\b"),
    ]
    warning_anchors = [
        re.compile(rf"\b{re.escape(qualified)}\s+failed\b"),
        re.compile(rf"\b{re.escape(test_name)}\s+failed\b"),
    ]
    lines = stdout.splitlines()

    # Most-specific first: the ERROR / Test Failed lines carry the
    # AssertionError + traceback (and the F19.8 DIFF token); we want
    # those over the WARNING preamble whenever both are present.
    start: int | None = None
    for pattern in test_failed_anchors:
        for i, line in enumerate(lines):
            if pattern.search(line):
                start = i
                break
        if start is not None:
            break

    # Fall back to the WARNING anchor only when no Test Failed line
    # was found. F19.8b: when we DO land on the WARNING line, search
    # forward (bounded) for a subsequent ERROR / Test Failed line that
    # mentions the same test name — that's where the traceback lives.
    # If we find one, restart capture there.
    if start is None:
        for pattern in warning_anchors:
            for i, line in enumerate(lines):
                if pattern.search(line):
                    start = i
                    break
            if start is not None:
                break
        if start is not None:
            for k in range(start + 1, min(len(lines), start + 20)):
                line_k = lines[k]
                if any(p.search(line_k) for p in test_failed_anchors):
                    start = k
                    break

    if start is None:
        return ""
    # Capture from the anchor forward to the next log header (a new record)
    # or EOF. Cap at 80 lines so a hostile log can't blow up the prompt.
    end = min(len(lines), start + 80)
    for j in range(start + 1, end):
        if _LOG_HEADER_RE.match(lines[j]):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


@dataclass(frozen=True)
class SimParse:
    """Parsed cocotb JUnit XML."""

    passed: bool
    tests_total: int
    tests_passed: int
    failing_assertions: list[str]
    violations: list[Violation]


def parse_sim_results(
    xml_text: str,
    *,
    run_stdout: str = "",
    run_stderr: str = "",
) -> SimParse:
    """Convert cocotb's JUnit-flavoured XML into a typed :class:`SimParse`.

    cocotb emits one ``<testcase>`` per test; failures carry a ``<failure>``
    child whose text body is the assertion message. An empty / malformed XML
    is treated as a run that failed before any test executed.

    Cocotb ≥1.7 sometimes emits an empty ``<failure/>`` element with no body
    and no ``message=`` attribute, which used to leave the diagnosis with
    nothing but the test name. When that happens we recover the assertion +
    traceback from ``run_stdout`` / ``run_stderr`` via
    :func:`extract_failure_block` so the outer loop has real signal to
    reason over.
    """
    if not xml_text.strip():
        return SimParse(
            passed=False, tests_total=0, tests_passed=0,
            failing_assertions=[],
            violations=[Violation(
                code="SIM.NO_RESULTS",
                severity="error",
                message="cocotb produced no results.xml — simulation failed to start",
            )],
        )
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return SimParse(
            passed=False, tests_total=0, tests_passed=0,
            failing_assertions=[],
            violations=[Violation(
                code="SIM.MALFORMED_RESULTS",
                severity="error",
                message=f"could not parse cocotb results.xml: {e}",
                detail={"raw_xml": xml_text[:512]},
            )],
        )

    cases = [root] if root.tag == "testcase" else list(root.iter("testcase"))

    failing_assertions: list[str] = []
    violations: list[Violation] = []
    tests_total = len(cases)
    tests_passed = 0
    for tc in cases:
        name = tc.get("name", "<unnamed>")
        classname = tc.get("classname", "")
        failure = tc.find("failure")
        error = tc.find("error")
        node = failure if failure is not None else error
        if node is None:
            tests_passed += 1
            continue
        body = (node.text or node.get("message") or "").strip()
        # F19.8b: cocotb sometimes pulls the WARNING regression-announcement
        # line into the JUnit failure body INSTEAD of the AssertionError
        # traceback. When the body matches that shape AND we have logs to
        # search, treat the body as empty so we fall through to
        # ``extract_failure_block`` and recover the real assertion.
        if (
            body
            and _WARNING_PREAMBLE_RE.match(body)
            and (run_stdout or run_stderr)
        ):
            recovered = (
                extract_failure_block(run_stdout, name, classname)
                or extract_failure_block(run_stderr, name, classname)
            )
            if recovered:
                body = recovered
        if not body:
            body = (
                extract_failure_block(run_stdout, name, classname)
                or extract_failure_block(run_stderr, name, classname)
            )
        loc = f"{classname}::{name}" if classname else name
        failing_assertions.append(f"{loc}: {body}" if body else loc)
        violations.append(Violation(
            code="ASSERT_FAIL",
            severity="error",
            message=body or "test failed without a message",
            location=loc,
            detail={
                "test": name,
                "classname": classname,
                "kind": node.tag,
                "failure_type": node.get("type", ""),
            },
        ))

    # F19.8b: belt-and-braces DIFF-token preservation. Even if the
    # body-recovery branch above missed the right block, scan the full
    # log streams for the canonical F19.8 token shape. When found,
    # prepend it to the first failing_assertions entry so the F20.6
    # trace parser sees it first.
    if failing_assertions:
        diff = (
            extract_diff_token(run_stdout)
            or extract_diff_token(run_stderr)
        )
        if diff and diff not in failing_assertions[0]:
            failing_assertions[0] = f"{diff} :: {failing_assertions[0]}"
            if violations and diff not in violations[0].message:
                violations[0] = Violation(
                    code=violations[0].code,
                    severity=violations[0].severity,
                    message=f"{diff} :: {violations[0].message}",
                    location=violations[0].location,
                    detail=violations[0].detail,
                )

    passed = tests_total > 0 and tests_passed == tests_total
    return SimParse(
        passed=passed,
        tests_total=tests_total,
        tests_passed=tests_passed,
        failing_assertions=failing_assertions,
        violations=violations,
    )


def runner_script(
    *,
    simulator: str,
    sources: list[str],
    hdl_toplevel: str,
    test_module: str,
    results_xml: str = RESULTS_XML,
    waves: bool = True,
    seed: int | None = None,
) -> str:
    """Render the Python driver that runs cocotb inside the sandbox.

    The shape is the same one cocotb's own docs recommend — ``get_runner`` for
    one simulator family, ``build`` then ``test``. Returning a string keeps
    this trivially unit-testable.
    """
    lines = [
        "# Auto-generated by chip_agent.tools.cocotb_sim — do not edit.",
        "from cocotb_tools.runner import get_runner",
        "",
        f"runner = get_runner({simulator!r})",
        "runner.build(",
        f"    sources={sources!r},",
        f"    hdl_toplevel={hdl_toplevel!r},",
        '    build_dir="build",',
        f"    waves={waves!r},",
        ")",
        "runner.test(",
        f"    hdl_toplevel={hdl_toplevel!r},",
        f"    test_module={test_module!r},",
        f"    results_xml={results_xml!r},",
    ]
    if seed is not None:
        lines.append(f"    seed={seed!r},")
    lines += [
        f"    waves={waves!r},",
        ")",
        "",
    ]
    return "\n".join(lines)


class SimulationService:
    """Run cocotb against an RTL artifact + Python testbench, return a SimulationResult."""

    NAME = "cocotb"

    def __init__(
        self,
        *,
        sandbox: SandboxLike,
        store: SqliteArtifactStore,
        simulator: str = DEFAULT_SIMULATOR,
        version_str: str = "bundled",
        tracer: Tracer | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self.simulator = simulator
        self._version_str = version_str
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=f"{self.NAME}+{self.simulator}",
            version=self._version_str,
            container_digest=digest,
        )

    def simulate(
        self,
        rtl: RTLArtifact,
        tb: TestbenchArtifact,
        *,
        seed: int = 0,
        time_limit_s: int | None = None,
    ) -> SimulationResult:
        if tb.framework != "cocotb":
            raise ValueError(
                f"SimulationService only drives cocotb, got framework={tb.framework!r}"
            )
        with self._tracer.span(
            name="tool:cocotb_simulate", kind=SpanKind.TOOL,
        ) as span:
            span.set_attribute("tool_name", self.NAME)
            span.set_attribute("simulator", self.simulator)
            span.set_attribute("module_id", rtl.module_id or rtl.top_module)
            span.set_attribute("design_id", rtl.design_id)
            span.set_attribute("seed", seed)
            with tempfile.TemporaryDirectory(prefix="chip-agent-sim-") as td:
                work_path = Path(td)
                source_file = _source_filename(rtl)
                tb_file = _tb_filename(tb)
                (work_path / source_file).write_bytes(self.store.get_blob(rtl.source))
                (work_path / tb_file).write_bytes(self.store.get_blob(tb.source))

                test_module = tb_file.removesuffix(".py")
                driver = runner_script(
                    simulator=self.simulator,
                    sources=[source_file],
                    hdl_toplevel=rtl.top_module,
                    test_module=test_module,
                    seed=seed,
                )
                (work_path / RUNNER_SCRIPT).write_text(driver)

                run = self.sandbox.run(
                    ["python", RUNNER_SCRIPT],
                    mount=work_path,
                    time_limit_s=time_limit_s,
                    extra_env={"COCOTB_RANDOM_SEED": str(seed)},
                )
                # cocotb_tools.runner writes results.xml + dump.vcd inside the
                # build_dir (``build/`` here) by default; we still tolerate
                # top-level placement so the unit tests (which stage files at
                # the mount root via StubSandbox) keep working.
                xml_text = _maybe_read(work_path / "build" / RESULTS_XML) or \
                    _maybe_read(work_path / RESULTS_XML)
                vcd_bytes = _maybe_read_bytes(work_path / "build" / WAVEFORM_VCD) or \
                    _maybe_read_bytes(work_path / WAVEFORM_VCD)

            parse = parse_sim_results(
                xml_text,
                run_stdout=run.stdout or "",
                run_stderr=run.stderr or "",
            )
            waveform = (
                self.store.put_blob(vcd_bytes, media_type="application/vcd")
                if vcd_bytes is not None
                else None
            )
            result = self._build_result(rtl, tb, run, parse, waveform, seed)
            span.set_attribute("passed", result.passed)
            span.set_attribute("tests_total", result.tests_total)
            span.set_attribute("tests_passed", result.tests_passed)
            return result

    def _build_result(
        self,
        rtl: RTLArtifact,
        tb: TestbenchArtifact,
        run: ToolRun,
        parse: SimParse,
        waveform: BlobRef | None,
        seed: int,
    ) -> SimulationResult:
        module = rtl.module_id or rtl.top_module
        tool = self.version()
        return SimulationResult(
            artifact_id=f"{rtl.design_id}.{module}.sim",
            design_id=rtl.design_id,
            module_id=rtl.module_id,
            passed=parse.passed and run.returncode == 0,
            tests_total=parse.tests_total,
            tests_passed=parse.tests_passed,
            failing_assertions=parse.failing_assertions,
            violations=parse.violations,
            waveform=waveform,
            checker=tool,
            metrics={
                "tests_total": float(parse.tests_total),
                "tests_passed": float(parse.tests_passed),
                "duration_s": run.duration_s,
            },
            provenance=Provenance(
                produced_by=Stage.RTL,
                agent="rtl_specialist",
                tool=tool,
                inputs=[rtl.ref(), tb.ref()],
                seed=seed,
            ),
        )


def _source_filename(rtl: RTLArtifact) -> str:
    ext = ".sv" if rtl.language.lower().startswith("system") else ".v"
    return f"{rtl.top_module}{ext}"


def _tb_filename(tb: TestbenchArtifact) -> str:
    # cocotb test modules are imported by name, so we use a stable filename.
    return f"test_{tb.target_module}.py"


def _maybe_read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _maybe_read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None
