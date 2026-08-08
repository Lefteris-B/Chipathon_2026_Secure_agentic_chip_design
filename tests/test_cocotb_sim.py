"""F2.3 acceptance: cocotb sim parser + runner wiring over a stub sandbox."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import (
    BlobRef,
    Provenance,
    RTLArtifact,
    Stage,
    TestbenchArtifact,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.cocotb_sim import (
    DEFAULT_SIMULATOR,
    RESULTS_XML,
    RUNNER_SCRIPT,
    SimulationService,
    extract_diff_token,
    extract_failure_block,
    parse_sim_results,
    runner_script,
)

# --------------------------------------------------------------------------- #
# Parser tests — JUnit XML produced by cocotb
# --------------------------------------------------------------------------- #
_PASS_XML = """\
<testsuites>
  <testsuite name="counter" tests="2" failures="0" errors="0">
    <testcase classname="test_counter" name="test_basic"/>
    <testcase classname="test_counter" name="test_overflow"/>
  </testsuite>
</testsuites>
"""

_FAIL_XML = """\
<testsuites>
  <testsuite name="counter" tests="2" failures="1" errors="0">
    <testcase classname="test_counter" name="test_basic"/>
    <testcase classname="test_counter" name="test_overflow">
      <failure type="AssertionError">at cycle 5, dut.q == 3 expected 4</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_clean_run_passes() -> None:
    p = parse_sim_results(_PASS_XML)
    assert p.passed
    assert p.tests_total == 2
    assert p.tests_passed == 2
    assert p.failing_assertions == []
    assert p.violations == []


def test_failed_assertion_captured() -> None:
    p = parse_sim_results(_FAIL_XML)
    assert not p.passed
    assert p.tests_total == 2
    assert p.tests_passed == 1
    assert len(p.failing_assertions) == 1
    assert "cycle 5" in p.failing_assertions[0]
    assert "test_overflow" in p.failing_assertions[0]
    assert p.violations[0].code == "ASSERT_FAIL"
    assert p.violations[0].severity == "error"
    assert p.violations[0].location == "test_counter::test_overflow"
    assert "expected 4" in p.violations[0].message


def test_empty_xml_yields_no_results_violation() -> None:
    p = parse_sim_results("")
    assert not p.passed
    assert p.tests_total == 0
    assert any(v.code == "SIM.NO_RESULTS" for v in p.violations)


def test_malformed_xml_yields_malformed_violation() -> None:
    p = parse_sim_results("<not really xml")
    assert not p.passed
    assert p.violations[0].code == "SIM.MALFORMED_RESULTS"
    assert "raw_xml" in p.violations[0].detail


# --------------------------------------------------------------------------- #
# Stdout-fallback regression — cocotb sometimes writes an empty <failure/>
# element whose body, message= attribute, and type= attribute are all blank.
# The assertion message + traceback only live in the simulator stdout. The
# fallback recovers them so the outer-loop FailureDiagnosis has real signal
# instead of just the test name.
#
# The failure mode reproduced here is the one that trapped a real run on a
# UART baud-counter at ``runs/.../baud_gen.sim v2``, where ``test_disable_
# stops_counter`` failed with body="", classname=test_baud_tick_gen, and the
# outer loop burned 3 attempts trying to guess the bug from the name alone.
# --------------------------------------------------------------------------- #
_EMPTY_FAIL_XML = """\
<testsuites>
  <testsuite name="baud_gen" tests="2" failures="1" errors="0">
    <testcase classname="test_baud_tick_gen" name="test_reset_state"/>
    <testcase classname="test_baud_tick_gen" name="test_disable_stops_counter">
      <failure/>
    </testcase>
  </testsuite>
</testsuites>
"""

_COCOTB_STDOUT_WITH_TRACEBACK = (
    "   460.00ns INFO     cocotb.regression                  "
    "Running test test_disable_stops_counter (2/2)\n"
    "   620.00ns ERROR    cocotb.regression                  "
    "Test Failed: test_disable_stops_counter (took 160.00ns)\n"
    "                                                        "
    "Traceback (most recent call last):\n"
    "                                                          "
    'File "/work/test_baud_tick_gen.py", line 119, '
    "in test_disable_stops_counter\n"
    "                                                            "
    "assert count_after_disable == count_at_disable, \\\n"
    "                                                        "
    "AssertionError: Counter should freeze when disabled: "
    "expected 50, got 60\n"
    "   620.00ns INFO     cocotb.regression                  "
    "**************************************************\n"
)


def test_empty_failure_body_falls_back_to_stdout_traceback() -> None:
    """The <failure/> element is empty; the real signal lives in cocotb's
    stdout. The parser must recover the AssertionError + traceback so the
    outer loop's NL summary contains the actual bug, not just the test name."""
    p = parse_sim_results(_EMPTY_FAIL_XML, run_stdout=_COCOTB_STDOUT_WITH_TRACEBACK)
    assert not p.passed
    assert p.tests_total == 2
    assert p.tests_passed == 1
    assert len(p.violations) == 1
    msg = p.violations[0].message
    assert "AssertionError" in msg
    assert "Counter should freeze when disabled" in msg
    assert "expected 50, got 60" in msg
    # The Traceback header is also preserved so a future regex can fish out
    # the file:line of the failing assertion.
    assert "Traceback" in msg
    assert "test_baud_tick_gen.py" in msg
    # And the failing_assertions entry carries the recovered body too.
    assert any("expected 50, got 60" in a for a in p.failing_assertions)


def test_existing_failure_body_wins_over_stdout() -> None:
    """If cocotb actually filled the <failure> body, the stdout fallback must
    NOT clobber it — stdout is a backup, not an override."""
    p = parse_sim_results(_FAIL_XML, run_stdout=_COCOTB_STDOUT_WITH_TRACEBACK)
    assert "cycle 5" in p.violations[0].message
    assert "expected 4" in p.violations[0].message
    # The stdout assertion (from a different test) must not leak in.
    assert "Counter should freeze" not in p.violations[0].message


def test_stdout_with_no_matching_block_keeps_default_message() -> None:
    """If the stdout has no anchor for this test name, the parser must fall
    through to the pre-existing "test failed without a message" default —
    behavior identical to the no-stdout path."""
    unrelated = (
        "   100.00ns INFO     cocotb.regression  Running test other_test (1/1)\n"
        "   200.00ns INFO     cocotb.regression  Test Passed: other_test\n"
    )
    p = parse_sim_results(_EMPTY_FAIL_XML, run_stdout=unrelated)
    assert p.violations[0].message == "test failed without a message"


def test_stdout_fallback_reads_stderr_when_stdout_silent() -> None:
    """Cocotb's Python logger sometimes routes via stderr (depending on
    handler config); the parser must check both streams."""
    p = parse_sim_results(
        _EMPTY_FAIL_XML,
        run_stdout="",
        run_stderr=_COCOTB_STDOUT_WITH_TRACEBACK,
    )
    assert "expected 50, got 60" in p.violations[0].message


def test_multiple_empty_failures_each_get_their_own_block() -> None:
    """Two failing tests in the same run must each pick up their own
    stdout block, not bleed into each other."""
    xml = """\
<testsuites>
  <testsuite name="baud_gen" tests="3" failures="2" errors="0">
    <testcase classname="test_baud_tick_gen" name="test_reset_state"/>
    <testcase classname="test_baud_tick_gen" name="test_mid_tick_at_cycle_217">
      <failure/>
    </testcase>
    <testcase classname="test_baud_tick_gen" name="test_disable_stops_counter">
      <failure/>
    </testcase>
  </testsuite>
</testsuites>
"""
    stdout = (
        "   200.00ns ERROR    cocotb.regression  "
        "Test Failed: test_mid_tick_at_cycle_217\n"
        "                                        "
        "AssertionError: mid_tick asserted at count=216, expected 217\n"
        "   300.00ns ERROR    cocotb.regression  "
        "Test Failed: test_disable_stops_counter\n"
        "                                        "
        "AssertionError: Counter should freeze when disabled: "
        "expected 50, got 60\n"
        "   400.00ns INFO     cocotb.regression  end\n"
    )
    p = parse_sim_results(xml, run_stdout=stdout)
    msgs = {v.detail["test"]: v.message for v in p.violations}
    assert "count=216, expected 217" in msgs["test_mid_tick_at_cycle_217"]
    assert "expected 50, got 60" in msgs["test_disable_stops_counter"]
    # No cross-contamination.
    assert "count=216" not in msgs["test_disable_stops_counter"]
    assert "expected 50" not in msgs["test_mid_tick_at_cycle_217"]


# --------------------------------------------------------------------------- #
# extract_failure_block direct unit tests
# --------------------------------------------------------------------------- #
def test_extract_failure_block_finds_test_failed_anchor() -> None:
    block = extract_failure_block(
        _COCOTB_STDOUT_WITH_TRACEBACK,
        test_name="test_disable_stops_counter",
        classname="test_baud_tick_gen",
    )
    assert "Test Failed: test_disable_stops_counter" in block
    assert "AssertionError" in block
    # The capture window stops at the next log header (the trailing INFO
    # banner line at sim time 620.00ns).
    assert "**************************************************" not in block


def test_extract_failure_block_handles_legacy_failed_anchor() -> None:
    """Older cocotb versions log ``<test_name> failed`` instead of
    ``Test Failed: <test_name>``. Both must anchor."""
    legacy = (
        "100.00ns ERROR cocotb.regression  test_disable_stops_counter failed\n"
        "                                  AssertionError: oops\n"
        "200.00ns INFO  cocotb.regression  done\n"
    )
    block = extract_failure_block(legacy, test_name="test_disable_stops_counter")
    assert "AssertionError: oops" in block


def test_extract_failure_block_returns_empty_when_not_found() -> None:
    assert extract_failure_block("", "anything") == ""
    assert extract_failure_block("no relevant content\n", "ghost_test") == ""


def test_extract_failure_block_caps_runaway_logs_at_80_lines() -> None:
    """A hostile / chatty log without a terminating header line must not
    drag the whole stdout into the prompt — capture is bounded."""
    chatty = "100.00ns ERROR cocotb.regression  Test Failed: t\n" + "x\n" * 500
    block = extract_failure_block(chatty, test_name="t")
    assert block.count("\n") <= 80


def test_extract_failure_block_prefers_test_failed_over_warning_line() -> None:
    """Cocotb emits a WARNING preamble line BEFORE the ERROR + traceback
    block. The previous line-outer / pattern-inner anchor walk let the
    generic ``<name> failed`` regex match the WARNING line first,
    capturing it as a single-line block with no traceback. The UART RX
    run hit exactly this trap: nl_summary was the WARNING string alone,
    the outer-loop model had no assertion message to reason over, and
    the repair attempts spiralled into prose-into-RTL garbage.

    Verbatim cocotb log shape (timestamps shortened for readability)."""
    stdout = (
        "  1560.01ns WARNING  cocotb.regression                  "
        "test_rx_shift_register.test_incremental_shift_evolution failed\n"
        "  1560.02ns ERROR    cocotb.regression                  "
        "Test Failed: test_incremental_shift_evolution (took 200.00ns)\n"
        "                                                          "
        "Traceback (most recent call last):\n"
        "                                                            "
        'File "/work/test_rx_shift_register.py", line 119, in '
        "test_incremental_shift_evolution\n"
        "                                                              "
        "assert actual_bits == expected_bits, \\\n"
        "                                                          "
        "AssertionError: shift bits diverged at cycle 8: "
        "expected 0xA5, got 0x5A\n"
        "  1560.03ns INFO     cocotb.regression                  "
        "**************************************************\n"
    )
    block = extract_failure_block(
        stdout,
        test_name="test_incremental_shift_evolution",
        classname="test_rx_shift_register",
    )
    # The block must START at the ERROR + ``Test Failed:`` line, NOT the
    # earlier WARNING line — otherwise the capture window ends at the
    # next log header (the ERROR line itself) and the traceback is lost.
    assert "Test Failed:" in block
    assert "AssertionError" in block
    assert "expected 0xA5, got 0x5A" in block
    assert "Traceback" in block


def test_extract_failure_block_falls_back_to_warning_when_no_test_failed() -> None:
    """If the cocotb log doesn't have a ``Test Failed:`` line at all
    (older versions, or a custom logger), the generic ``<name> failed``
    anchor must still match. Pin the fallback so removing the WARNING
    anchor never silently regresses."""
    stdout = (
        "  100.00ns WARNING  cocotb.regression  "
        "test_a.test_b failed\n"
        "                                       "
        "AssertionError: oops\n"
    )
    block = extract_failure_block(
        stdout, test_name="test_b", classname="test_a",
    )
    assert "test_b failed" in block
    assert "AssertionError: oops" in block


def test_error_node_treated_like_failure() -> None:
    xml = """\
    <testsuites>
      <testsuite name="x" tests="1" failures="0" errors="1">
        <testcase classname="m" name="t">
          <error type="ImportError">cannot import foo</error>
        </testcase>
      </testsuite>
    </testsuites>
    """
    p = parse_sim_results(xml)
    assert not p.passed
    assert p.violations[0].detail["kind"] == "error"
    assert "ImportError" in p.violations[0].detail["failure_type"]


# --------------------------------------------------------------------------- #
# runner_script renderer
# --------------------------------------------------------------------------- #
def test_runner_script_renders_get_runner_and_seed() -> None:
    script = runner_script(
        simulator="verilator",
        sources=["counter.v"],
        hdl_toplevel="counter",
        test_module="test_counter",
        seed=42,
    )
    assert "from cocotb_tools.runner import get_runner" in script
    assert "get_runner('verilator')" in script
    assert "sources=['counter.v']" in script
    assert "hdl_toplevel='counter'" in script
    assert "test_module='test_counter'" in script
    assert "seed=42" in script
    assert RESULTS_XML in script


def test_runner_script_without_seed_omits_seed_line() -> None:
    script = runner_script(
        simulator="icarus",
        sources=["counter.sv"],
        hdl_toplevel="counter",
        test_module="test_counter",
    )
    assert "seed=" not in script  # only kwargs we set; cocotb picks its own


# --------------------------------------------------------------------------- #
# Runner wiring with a stub sandbox
# --------------------------------------------------------------------------- #
@dataclass
class StubSandbox:
    tool_run: ToolRun
    seed_files: dict[str, bytes] = field(default_factory=dict)  # written before parsing
    calls: list[tuple[list[str], Path, dict[str, bytes], dict[str, str]]] = field(
        default_factory=list,
    )

    def run(
        self, cmd: list[str], mount: Path | str, *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun:
        mp = Path(mount)
        staged = {p.name: p.read_bytes() for p in mp.iterdir() if p.is_file()}
        # Simulate cocotb's output by dropping files the parser will pick up.
        for name, body in self.seed_files.items():
            (mp / name).write_bytes(body)
        self.calls.append((list(cmd), mp, staged, dict(extra_env or {})))
        return self.tool_run


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


def _stage_rtl_and_tb(
    store: SqliteArtifactStore,
    *,
    rtl_text: str = "module counter; endmodule\n",
    tb_text: str = "# cocotb tb\n",
) -> tuple[RTLArtifact, TestbenchArtifact]:
    rtl_blob = store.put_blob(rtl_text.encode(), media_type="text/x-verilog")
    rtl = RTLArtifact(
        artifact_id="d0.counter.rtl", design_id="d0", module_id="counter",
        top_module="counter", language="verilog", source=rtl_blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(rtl)
    tb_blob = store.put_blob(tb_text.encode(), media_type="text/x-python")
    tb = TestbenchArtifact(
        artifact_id="d0.counter.tb", design_id="d0", module_id="counter",
        target_module="counter", framework="cocotb", source=tb_blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(tb)
    return (
        store.get_by_id(rtl.artifact_id),  # type: ignore[return-value]
        store.get_by_id(tb.artifact_id),   # type: ignore[return-value]
    )


def test_simulate_passes_known_good_counter(store: SqliteArtifactStore) -> None:
    rtl, tb = _stage_rtl_and_tb(store)
    sandbox = StubSandbox(
        tool_run=ToolRun(returncode=0, stdout="", stderr="", artifacts_dir="/tmp",
                         duration_s=0.5),
        seed_files={RESULTS_XML: _PASS_XML.encode(), "dump.vcd": b"$timescale\n#0\n"},
    )
    svc = SimulationService(sandbox=sandbox, store=store)

    result = svc.simulate(rtl, tb, seed=42)

    assert result.passed
    assert result.gate_ok
    assert result.tests_total == 2
    assert result.tests_passed == 2
    assert result.failing_assertions == []
    assert result.waveform is not None
    assert result.waveform.media_type == "application/vcd"
    # The seed flowed through to provenance.
    assert result.provenance.seed == 42
    # Both inputs are recorded.
    assert {r.artifact_id for r in result.provenance.inputs} == {
        "d0.counter.rtl", "d0.counter.tb",
    }


def test_simulate_seeded_bug_captures_failing_assertion(
    store: SqliteArtifactStore,
) -> None:
    rtl, tb = _stage_rtl_and_tb(store)
    sandbox = StubSandbox(
        tool_run=ToolRun(returncode=1, stdout="", stderr="", artifacts_dir="/tmp",
                         duration_s=0.5),
        seed_files={RESULTS_XML: _FAIL_XML.encode()},
    )
    svc = SimulationService(sandbox=sandbox, store=store)

    result = svc.simulate(rtl, tb, seed=7)

    assert not result.passed
    assert not result.gate_ok
    assert result.tests_total == 2
    assert result.tests_passed == 1
    assert any("cycle 5" in a for a in result.failing_assertions)
    assert result.violations[0].code == "ASSERT_FAIL"


def test_simulate_plumbs_run_stdout_into_failure_body(
    store: SqliteArtifactStore,
) -> None:
    """End-to-end: when cocotb produces an empty <failure/> in results.xml
    but writes the assertion traceback to stdout, ``SimulationService.simulate``
    must surface the traceback in the final ``LintResult.violations[0].message``.
    Without this plumbing the outer loop's FailureDiagnosis is reduced to the
    test name — which is what trapped the real UART baud_counter run."""
    rtl, tb = _stage_rtl_and_tb(store)
    sandbox = StubSandbox(
        tool_run=ToolRun(
            returncode=1,
            stdout=_COCOTB_STDOUT_WITH_TRACEBACK,
            stderr="",
            artifacts_dir="/tmp",
            duration_s=0.8,
        ),
        seed_files={RESULTS_XML: _EMPTY_FAIL_XML.encode()},
    )
    svc = SimulationService(sandbox=sandbox, store=store)

    result = svc.simulate(rtl, tb, seed=0)

    assert not result.passed
    assert result.tests_total == 2
    assert result.tests_passed == 1
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.code == "ASSERT_FAIL"
    assert "expected 50, got 60" in v.message
    assert "Traceback" in v.message
    # And the failing_assertions list (used downstream by trace.py to build
    # the FailureDiagnosis nl_summary) also carries the recovered body.
    assert any("expected 50, got 60" in a for a in result.failing_assertions)


def test_simulate_writes_driver_script_and_staged_sources(
    store: SqliteArtifactStore,
) -> None:
    rtl, tb = _stage_rtl_and_tb(store)
    sandbox = StubSandbox(
        tool_run=ToolRun(returncode=0, stdout="", stderr="", artifacts_dir="/tmp",
                         duration_s=0.1),
        seed_files={RESULTS_XML: _PASS_XML.encode()},
    )
    svc = SimulationService(sandbox=sandbox, store=store)
    svc.simulate(rtl, tb)

    cmd, _mount, staged, env = sandbox.calls[0]
    assert cmd == ["python", RUNNER_SCRIPT]
    assert "counter.v" in staged
    assert "test_counter.py" in staged
    assert RUNNER_SCRIPT in staged
    assert b"get_runner('verilator')" in staged[RUNNER_SCRIPT]
    assert env["COCOTB_RANDOM_SEED"] == "0"


def test_simulate_rejects_non_cocotb_framework(store: SqliteArtifactStore) -> None:
    rtl, _tb = _stage_rtl_and_tb(store)
    # A non-cocotb testbench (e.g. SystemVerilog) is out of scope here.
    bad_tb = TestbenchArtifact(
        artifact_id="d0.counter.tb_sv",
        design_id="d0",
        target_module="counter",
        framework="sv",
        source=BlobRef(path="00/0", sha256="0" * 64, size_bytes=4),
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(bad_tb)
    bad_tb = store.get_by_id("d0.counter.tb_sv")  # type: ignore[assignment]
    sandbox = StubSandbox(tool_run=ToolRun(returncode=0, stdout="", stderr="",
                                           artifacts_dir="/tmp", duration_s=0.0))
    svc = SimulationService(sandbox=sandbox, store=store)
    with pytest.raises(ValueError):
        svc.simulate(rtl, bad_tb)


def test_default_simulator_is_verilator(store: SqliteArtifactStore) -> None:
    assert DEFAULT_SIMULATOR == "verilator"
    sandbox = StubSandbox(tool_run=ToolRun(returncode=0, stdout="", stderr="",
                                           artifacts_dir="/tmp", duration_s=0.0))
    svc = SimulationService(sandbox=sandbox, store=store)
    assert svc.simulator == "verilator"
    assert svc.version().name.startswith("cocotb+")


# --------------------------------------------------------------------------- #
# F19.8b — Robust DIFF-token extraction when cocotb logs the WARNING
# preamble in the JUnit body instead of the AssertionError traceback.
# --------------------------------------------------------------------------- #
_WARNING_BODY_FAIL_XML = """\
<testsuites>
  <testsuite name="baud_tick_gen" tests="1" failures="1" errors="0">
    <testcase classname="test_baud_tick_gen" name="test_baud_tick_gen_diff">
      <failure>60.00ns WARNING  cocotb.regression                  test_baud_tick_gen.test_baud_tick_gen_diff failed</failure>
    </testcase>
  </testsuite>
</testsuites>
"""

_BAUD_GEN_STDOUT_WITH_DIFF = (
    "     0.00ns INFO     cocotb.regression                  Running test test_baud_tick_gen_diff (1/1)\n"
    "    60.00ns WARNING  cocotb.regression                  test_baud_tick_gen.test_baud_tick_gen_diff failed\n"
    "    60.00ns ERROR    cocotb.regression                  Test Failed: test_baud_tick_gen_diff (took 60.00ns)\n"
    "                                                          Traceback (most recent call last):\n"
    "                                                            File \"/work/test_baud_tick_gen.py\", line 38, in test_baud_tick_gen_diff\n"
    "                                                              assert actual == expected, (\n"
    "                                                          AssertionError: DIFF|cycle=6|signal=count|expected=217|actual=215\n"
    "    60.00ns INFO     cocotb.regression                  *** SIM TIME COMPLETED ***\n"
)


def test_warning_preamble_body_falls_back_to_stdout_extraction() -> None:
    """**F19.8b core fix.** When the JUnit ``<failure>`` body contains
    only the cocotb WARNING regression-announcement line (and the
    stdout has the real ERROR + traceback), the parser must NOT trust
    the WARNING body — fall through to stdout extraction.
    """
    p = parse_sim_results(
        _WARNING_BODY_FAIL_XML, run_stdout=_BAUD_GEN_STDOUT_WITH_DIFF,
    )
    assert not p.passed
    # The recovered violation message contains the AssertionError
    # traceback, not the WARNING preamble.
    msg = p.violations[0].message
    assert "AssertionError" in msg
    assert "DIFF|cycle=6|signal=count|expected=217|actual=215" in msg
    # The WARNING preamble line is NOT promoted as the failure body
    # (the extraction skipped it forward to the ERROR anchor).
    assert "WARNING" not in msg or "ERROR" in msg


def test_extract_failure_block_forwards_warning_to_error_anchor() -> None:
    """When extract_failure_block lands on the WARNING line, it must
    search forward for the ERROR / Test Failed anchor that carries
    the AssertionError traceback. The capture starts at the ERROR
    line, not the WARNING line."""
    block = extract_failure_block(
        _BAUD_GEN_STDOUT_WITH_DIFF,
        test_name="test_baud_tick_gen_diff",
        classname="test_baud_tick_gen",
    )
    assert "Test Failed: test_baud_tick_gen_diff" in block
    assert "AssertionError" in block
    assert "DIFF|cycle=6" in block
    # The block does NOT start with the WARNING line.
    assert not block.lstrip().startswith("60.00ns WARNING")


def test_extract_diff_token_finds_first_match() -> None:
    """Helper unit test: ``extract_diff_token`` matches the canonical
    F19.8 token shape and returns the first instance."""
    text = (
        "noise before\n"
        "AssertionError: DIFF|cycle=4|signal=q|expected=5|actual=4\n"
        "more noise DIFF|cycle=99|signal=ack|expected=1|actual=0\n"
    )
    assert (
        extract_diff_token(text)
        == "DIFF|cycle=4|signal=q|expected=5|actual=4"
    )
    assert extract_diff_token("") is None
    assert extract_diff_token("no diff tokens here") is None


def test_diff_token_promoted_when_only_in_stderr() -> None:
    """Belt-and-braces: even when stdout extraction misses the right
    block, scanning stderr for the canonical DIFF token shape promotes
    it to the front of failing_assertions[0] so F20.6 sees structured
    fields."""
    minimal_stderr = (
        "verilator_runtime: assertion failed:\n"
        "  AssertionError: DIFF|cycle=4|signal=q|expected=5|actual=4\n"
    )
    p = parse_sim_results(_FAIL_XML, run_stderr=minimal_stderr)
    # The XML body wins (existing behaviour) but the DIFF token is
    # promoted to the front of the recorded failing assertion.
    assert p.failing_assertions[0].startswith(
        "DIFF|cycle=4|signal=q|expected=5|actual=4 :: "
    )
    # The violation message gets the same prepend so the trace parser
    # sees the DIFF first.
    assert p.violations[0].message.startswith(
        "DIFF|cycle=4|signal=q|expected=5|actual=4 :: "
    )


def test_no_diff_token_no_promotion() -> None:
    """Back-compat: when no DIFF token exists in any stream, the
    failing_assertions entry is unchanged from the pre-F19.8b shape."""
    p = parse_sim_results(_FAIL_XML, run_stdout="random output\n")
    assert "DIFF|" not in p.failing_assertions[0]
    assert "::" not in p.failing_assertions[0].split(": ", 1)[1]


def test_diff_token_not_duplicated_when_already_in_body() -> None:
    """When the DIFF token is already present in the failure body
    (the F19.8 happy path), the promotion step doesn't double-prepend
    it. Avoids garbled prompts on the typical case."""
    stdout = (
        "    60.00ns ERROR    cocotb.regression                  "
        "Test Failed: t (took 60.00ns)\n"
        "                                                          "
        "AssertionError: DIFF|cycle=4|signal=q|expected=5|actual=4\n"
    )
    xml = """\
<testsuites>
  <testsuite name="x" tests="1" failures="1" errors="0">
    <testcase classname="c" name="t">
      <failure/>
    </testcase>
  </testsuite>
</testsuites>
"""
    p = parse_sim_results(xml, run_stdout=stdout)
    # The token appears exactly once in failing_assertions[0]:
    assert p.failing_assertions[0].count(
        "DIFF|cycle=4|signal=q|expected=5|actual=4",
    ) == 1


def test_uart_rx_baud_gen_fixture_yields_structured_diff_diagnosis() -> None:
    """**AC test**: a committed fixture mirroring the live UART RX
    ``baud_gen`` failure (WARNING in body, ERROR + DIFF token in
    stdout) flows through ``parse_sim_results`` and
    ``parse_failing_assertion`` to produce a FailureDiagnosis with
    populated ``cycle`` / ``failing_signal`` / ``expected`` /
    ``actual`` fields. Pins the end-to-end fix.
    """
    from pathlib import Path

    from chip_agent.tools.trace import parse_failing_assertion

    fixture_path = (
        Path(__file__).parent / "fixtures" / "cocotb" / "uart_rx_baud_gen.stdout"
    )
    stdout = fixture_path.read_text()
    parsed = parse_sim_results(
        _WARNING_BODY_FAIL_XML, run_stdout=stdout,
    )
    facts = parse_failing_assertion(parsed.failing_assertions[0])
    assert facts.cycle == 6
    assert facts.failing_signal == "count"
    assert facts.expected == "217"
    assert facts.actual == "215"
