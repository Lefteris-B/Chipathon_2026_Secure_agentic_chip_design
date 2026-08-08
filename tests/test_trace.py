"""F2.4 acceptance: trace -> TraceFacts + FailureDiagnosis with NL summary."""

from __future__ import annotations

from chip_agent.design_state import (
    ArtifactKind,
    BlobRef,
    Provenance,
    RTLArtifact,
    SimulationResult,
    Stage,
    TraceFacts,
    Violation,
)
from chip_agent.tools.trace import (
    build_failure_diagnosis,
    failing_test_name_from_sim,
    make_trace_facts,
    parse_failing_assertion,
    render_nl_summary,
)


# --------------------------------------------------------------------------- #
# parse_failing_assertion — the regex heuristic
# --------------------------------------------------------------------------- #
def test_ack_low_at_cycle_pattern() -> None:
    # The exact AC example from FEATURES.md.
    facts = parse_failing_assertion("ack low at cycle 5, expected high")
    assert facts.failing_signal == "ack"
    assert facts.cycle == 5
    assert facts.actual == "low"
    assert facts.expected == "high"


def test_dut_dotted_signal_with_eq_pattern() -> None:
    facts = parse_failing_assertion("at cycle 12, dut.q == 3 expected 4")
    assert facts.failing_signal == "dut.q"
    assert facts.cycle == 12
    assert facts.actual == "3"
    assert facts.expected == "4"


def test_expected_to_be_pattern() -> None:
    facts = parse_failing_assertion(
        "Expected dut.q to be high at cycle 7, got low"
    )
    assert facts.failing_signal == "dut.q"
    assert facts.expected == "high"
    assert facts.cycle == 7
    assert facts.actual == "low"


def test_expected_actual_at_cycle_pattern() -> None:
    facts = parse_failing_assertion(
        "dut.count expected 4 actual 3 at cycle 11"
    )
    assert facts.failing_signal == "dut.count"
    assert facts.expected == "4"
    assert facts.actual == "3"
    assert facts.cycle == 11


def test_unparseable_returns_empty_facts() -> None:
    facts = parse_failing_assertion("something went sideways")
    assert facts == TraceFacts()


def test_empty_text_returns_empty_facts() -> None:
    assert parse_failing_assertion("") == TraceFacts()


# --------------------------------------------------------------------------- #
# make_trace_facts — picks the first failing assertion off a SimulationResult
# --------------------------------------------------------------------------- #
def _sim(*, passed: bool = False, failing: list[str] | None = None) -> SimulationResult:
    return SimulationResult(
        artifact_id="d0.counter.sim",
        design_id="d0",
        module_id="counter",
        passed=passed,
        tests_total=10,
        tests_passed=9 if not passed else 10,
        failing_assertions=failing or [],
        violations=[
            Violation(code="ASSERT_FAIL", severity="error",
                      message=(failing or ["x"])[0])
        ] if failing else [],
        provenance=Provenance(produced_by=Stage.RTL),
    )


def test_make_trace_facts_picks_first_failing_assertion() -> None:
    sim = _sim(failing=[
        "ack low at cycle 5, expected high",
        "another unrelated failure",
    ])
    facts = make_trace_facts(sim)
    assert facts.failing_signal == "ack"
    assert facts.cycle == 5


def test_make_trace_facts_no_failures_returns_empty() -> None:
    sim = _sim(passed=True)
    assert make_trace_facts(sim) == TraceFacts()


# --------------------------------------------------------------------------- #
# render_nl_summary
# --------------------------------------------------------------------------- #
def test_nl_summary_full_facts() -> None:
    facts = TraceFacts(failing_signal="ack", cycle=5, expected="high", actual="low")
    s = render_nl_summary(facts)
    # The AC text shape — names signal/cycle/expected/actual.
    assert "ack" in s
    assert "cycle 5" in s
    assert "expected" in s.lower()
    assert "low" in s
    assert "high" in s


def test_nl_summary_falls_back_to_raw_when_partial() -> None:
    facts = TraceFacts(failing_signal="ack")  # missing cycle/expected/actual
    raw = "ack glitch, cycle unknown"
    s = render_nl_summary(facts, raw_assertion=raw)
    assert s == f"Assertion failed: {raw}"


def test_nl_summary_with_no_info_at_all() -> None:
    s = render_nl_summary(TraceFacts())
    assert "no parseable" in s.lower() or "failure" in s.lower()


# --------------------------------------------------------------------------- #
# build_failure_diagnosis
# --------------------------------------------------------------------------- #
def _rtl() -> RTLArtifact:
    return RTLArtifact(
        artifact_id="d0.counter.rtl",
        design_id="d0",
        module_id="counter",
        top_module="counter",
        source=BlobRef(path="00/0", sha256="0" * 64, size_bytes=4),
        provenance=Provenance(produced_by=Stage.RTL),
    )


def test_build_failure_diagnosis_from_seeded_bug() -> None:
    rtl = _rtl()
    rtl.content_hash = rtl.compute_content_hash()
    sim = _sim(failing=["ack low at cycle 5, expected high"])
    sim.content_hash = sim.compute_content_hash()

    diag = build_failure_diagnosis(sim, rtl)

    # All four deterministic fields are populated (the F2.4 AC).
    assert diag.failing_signal == "ack"
    assert diag.cycle == 5
    assert diag.expected == "high"
    assert diag.actual == "low"
    # An NL summary is produced even without a model.
    assert "ack" in diag.nl_summary
    assert "cycle 5" in diag.nl_summary
    # suspected_cause is left for the router/model to fill.
    assert diag.suspected_cause is None
    # Provenance carries both inputs and the produced-by stage.
    assert diag.kind is ArtifactKind.DIAGNOSIS
    assert {r.artifact_id for r in diag.provenance.inputs} == {
        "d0.counter.sim", "d0.counter.rtl",
    }


def test_build_failure_diagnosis_when_sim_passed() -> None:
    rtl = _rtl()
    rtl.content_hash = rtl.compute_content_hash()
    sim = _sim(passed=True)
    sim.content_hash = sim.compute_content_hash()
    diag = build_failure_diagnosis(sim, rtl)
    # No failing assertion -> empty facts but a graceful summary.
    assert diag.failing_signal is None
    assert diag.cycle is None
    assert "failure" in diag.nl_summary.lower() or "no parseable" in diag.nl_summary.lower()


def test_build_failure_diagnosis_accepts_suspected_cause_and_target_stage() -> None:
    rtl = _rtl()
    rtl.content_hash = rtl.compute_content_hash()
    sim = _sim(failing=["ack low at cycle 5, expected high"])
    sim.content_hash = sim.compute_content_hash()
    diag = build_failure_diagnosis(
        sim, rtl,
        suspected_cause="missing else branch in next-state logic",
        target_stage=Stage.RTL,
    )
    assert diag.suspected_cause == "missing else branch in next-state logic"
    assert diag.target_stage is Stage.RTL
    assert diag.target_module == "counter"


# --------------------------------------------------------------------------- #
# F20.6 — diagnosis enrichment integration
# --------------------------------------------------------------------------- #
def test_failing_test_name_extracted_from_qualified_assertion() -> None:
    """`failing_assertions[0]` carries `<classname>::<name>` or
    `<classname>::<name>: <body>` — extract just <name>."""
    sim = _sim(failing=[
        "tests::test_increment: q off by one at cycle 4",
    ])
    assert failing_test_name_from_sim(sim) == "test_increment"


def test_failing_test_name_extracted_from_bare_name() -> None:
    """No classname prefix is still parseable."""
    sim = _sim(failing=["test_reset: rst_n held high"])
    assert failing_test_name_from_sim(sim) == "test_reset"


def test_failing_test_name_empty_when_no_failures() -> None:
    assert failing_test_name_from_sim(_sim(passed=True)) == ""


def test_build_failure_diagnosis_carries_pre_extracted_enriched_fields() -> None:
    """Callers without a store seam can inject the three enriched
    strings directly. Used by the cross-stage feedback path and by
    unit tests that don't want to spin up a SqliteArtifactStore."""
    rtl = _rtl()
    rtl.content_hash = rtl.compute_content_hash()
    sim = _sim(failing=["tests::test_increment: q off by one at cycle 4"])
    sim.content_hash = sim.compute_content_hash()

    diag = build_failure_diagnosis(
        sim, rtl,
        test_source="@cocotb.test()\nasync def test_increment(dut):\n    pass\n",
        window_vcd_summary="Cycle 4 (FAILURE): clk=1, q=0x4",
        active_signals_at_failure_cycle={"clk": "1", "q": "0x4"},
    )
    assert "test_increment" in diag.test_source
    assert "FAILURE" in diag.window_vcd_summary
    assert diag.active_signals_at_failure_cycle == {"clk": "1", "q": "0x4"}


def test_build_failure_diagnosis_fetches_enrichment_from_store_when_provided(
    tmp_path,
) -> None:
    """End-to-end: when both testbench and store are supplied, the
    function fetches the testbench source + waveform itself and runs
    the helpers. Confirms the F20.6 wire path the rtl_stage callsites
    will exercise in production."""
    from chip_agent.design_state import TestbenchArtifact
    from chip_agent.store import SqliteArtifactStore

    tb_source = (
        b"import cocotb\n\n"
        b"@cocotb.test()\n"
        b"async def test_increment(dut):\n"
        b"    assert int(dut.q.value) == cycle + 1\n"
    )
    # Minimal VCD where clk has one rising edge that lands at cycle 0,
    # which we treat as the failure cycle.
    vcd_bytes = (
        b"$scope module top $end\n"
        b'$var wire 1 ! clk $end\n'
        b'$var wire 8 # q $end\n'
        b"$upscope $end\n"
        b"$enddefinitions $end\n"
        b"#0\n0!\nb00000000 #\n"
        b"#5\n1!\nb00000001 #\n"
    )

    store = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "runs",
    )
    try:
        tb_blob = store.put_blob(tb_source, media_type="text/x-python")
        vcd_blob = store.put_blob(vcd_bytes, media_type="application/vcd")

        rtl = _rtl()
        rtl.content_hash = rtl.compute_content_hash()
        sim = _sim(failing=["tests::test_increment: q expected 1 actual 0 at cycle 0"])
        sim.waveform = vcd_blob
        sim.content_hash = sim.compute_content_hash()

        tb = TestbenchArtifact(
            artifact_id="d0.counter.tb",
            design_id="d0",
            module_id="counter",
            target_module="counter",
            source=tb_blob,
            provenance=Provenance(produced_by=Stage.RTL),
        )

        diag = build_failure_diagnosis(
            sim, rtl, testbench=tb, store=store,
        )

        # Testbench source slice carries the failing test's body.
        assert "test_increment" in diag.test_source
        assert "@cocotb.test()" in diag.test_source
        # VCD window includes the failure-marked cycle.
        assert "(FAILURE)" in diag.window_vcd_summary
        assert "Cycle 0" in diag.window_vcd_summary
        # Snapshot dict at the failure cycle.
        assert diag.active_signals_at_failure_cycle.get("clk") == "1"
    finally:
        store.close()


def test_build_failure_diagnosis_leaves_enriched_fields_empty_without_inputs() -> None:
    """Backwards compatibility: callers that don't pass testbench /
    store / pre-extracted strings still get an artifact — the three
    enriched fields default to empty values, so existing tests and
    callers see no behaviour change."""
    rtl = _rtl()
    rtl.content_hash = rtl.compute_content_hash()
    sim = _sim(failing=["ack low at cycle 5, expected high"])
    sim.content_hash = sim.compute_content_hash()

    diag = build_failure_diagnosis(sim, rtl)

    assert diag.test_source == ""
    assert diag.window_vcd_summary == ""
    assert diag.active_signals_at_failure_cycle == {}
