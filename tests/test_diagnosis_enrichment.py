"""F20.6: pure-function tests for the diagnosis enrichment helpers.

Covers the failing-test source slice (cocotb decorator + def body
extraction) and the VCD cycle-window summary + signal snapshot.
Both helpers are designed to return empty results on any parse
failure so the diagnosis pipeline never crashes — these tests pin
both the happy path and the fallback behaviour.
"""

from __future__ import annotations

from chip_agent.tools.diagnosis_enrichment import (
    extract_failing_test_source,
    parse_vcd_window,
)

_TESTBENCH_SOURCE = b'''\
"""cocotb harness for the 8-bit counter."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge


@cocotb.test()
async def test_increment(dut):
    """q advances by 1 per enabled cycle."""
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.rst_n.value = 0
    dut.en.value = 0
    await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    dut.en.value = 1
    for cycle in range(3):
        await RisingEdge(dut.clk)
        assert int(dut.q.value) == cycle + 1


@cocotb.test()
async def test_reset(dut):
    """rst_n low forces q to 0."""
    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    dut.rst_n.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.q.value) == 0
'''


def _vcd_with_counter(cycles: int = 6) -> bytes:
    """Build a minimal VCD where clk toggles every 5ns and q increments
    once per rising clock edge once en is high. Used to pin the cycle
    counter + signal snapshot logic deterministically.
    """
    header = (
        "$timescale 1ns $end\n"
        "$scope module top $end\n"
        '$var wire 1 ! clk $end\n'
        '$var wire 1 # rst_n $end\n'
        '$var wire 1 $ en $end\n'
        '$var wire 8 % q $end\n'
        "$upscope $end\n"
        "$enddefinitions $end\n"
    )
    # Initial state: clk=0, rst_n=1, en=1, q=0.
    body = "#0\n0!\n1#\n1$\nb00000000 %\n"
    # Each cycle pair: rising edge then falling edge. q increments
    # on each rising edge (mirrors the counter semantics).
    time = 0
    q_value = 0
    for _ in range(cycles):
        time += 5
        q_value = (q_value + 1) & 0xFF
        # rising edge — q updates here so the post-edge snapshot at
        # this cycle reflects the new q.
        body += f"#{time}\n1!\nb{q_value:08b} %\n"
        time += 5
        # falling edge — no signal change other than clk.
        body += f"#{time}\n0!\n"
    return (header + body).encode("utf-8")


# --------------------------------------------------------------------------- #
# extract_failing_test_source
# --------------------------------------------------------------------------- #
def test_extract_failing_test_source_finds_named_test() -> None:
    body = extract_failing_test_source(_TESTBENCH_SOURCE, "test_increment")
    assert body, "expected non-empty body for known test"
    # Decorator and def line for the requested test must be present.
    assert "@cocotb.test()" in body
    assert "async def test_increment(dut):" in body
    # The other test must NOT leak into the slice.
    assert "test_reset" not in body
    # The assertion line from inside the body should be present.
    assert "cycle + 1" in body


def test_extract_failing_test_source_returns_empty_on_unknown_name() -> None:
    assert extract_failing_test_source(_TESTBENCH_SOURCE, "test_nonexistent") == ""


def test_extract_failing_test_source_returns_empty_on_garbage_input() -> None:
    """Non-UTF-8 bytes must surface as empty string, not raise."""
    assert extract_failing_test_source(b"\xff\xfe\xff", "test_increment") == ""


def test_extract_failing_test_source_returns_empty_on_empty_test_name() -> None:
    assert extract_failing_test_source(_TESTBENCH_SOURCE, "") == ""


# --------------------------------------------------------------------------- #
# parse_vcd_window
# --------------------------------------------------------------------------- #
def test_parse_vcd_window_renders_centered_summary() -> None:
    vcd = _vcd_with_counter(cycles=8)
    summary, snapshot = parse_vcd_window(vcd, failure_cycle=4, radius=2)
    assert summary, "expected non-empty window summary"
    # Window spans cycles 2..6 (5 entries).
    lines = summary.splitlines()
    assert len(lines) == 5
    assert lines[0].startswith("Cycle 2")
    assert lines[-1].startswith("Cycle 6")
    # Failure cycle is marked.
    assert "(FAILURE)" in lines[2]   # the centre of the window is cycle 4
    assert "Cycle 4 (FAILURE)" in summary
    # Snapshot at the failure cycle carries the pre-edge q value (q
    # advances later in the same timestamp; the snapshot pins q's
    # observable value AT the rising edge, matching how a real DUT
    # would surface it to a cocotb harness).
    assert snapshot.get("q") == "0x4"
    assert snapshot.get("clk") == "1"


def test_parse_vcd_window_returns_snapshot_at_failure_cycle() -> None:
    vcd = _vcd_with_counter(cycles=6)
    _, snapshot = parse_vcd_window(vcd, failure_cycle=3)
    # On cycle 3 the rising edge captures q's pre-edge value (the
    # counter advances later in the same timestamp).
    assert snapshot["q"] == "0x3"
    assert snapshot["clk"] == "1"
    assert snapshot["rst_n"] == "1"
    assert snapshot["en"] == "1"


def test_parse_vcd_window_truncates_at_vcd_start() -> None:
    """Asking for a window that would extend before cycle 0 must still
    produce a coherent summary starting at cycle 0."""
    vcd = _vcd_with_counter(cycles=4)
    summary, _snapshot = parse_vcd_window(vcd, failure_cycle=1, radius=3)
    lines = summary.splitlines()
    # Window is [0, 4] -> 4 lines (only 4 rising edges in 4 cycles).
    assert lines[0].startswith("Cycle 0")
    assert "(FAILURE)" in lines[1]   # cycle 1 is the failure


def test_parse_vcd_window_returns_empty_on_malformed_vcd() -> None:
    summary, snapshot = parse_vcd_window(b"this is not a VCD", failure_cycle=5)
    assert summary == ""
    assert snapshot == {}


def test_parse_vcd_window_returns_empty_on_missing_clock() -> None:
    """A VCD with no clk / clock signal can't be cycle-windowed; the
    helper falls back to empty results so the diagnosis simply omits
    the section."""
    no_clock = (
        b"$scope module top $end\n"
        b'$var wire 8 % q $end\n'
        b"$upscope $end\n"
        b"$enddefinitions $end\n"
        b"#0\nb00000000 %\n"
    )
    summary, snapshot = parse_vcd_window(no_clock, failure_cycle=0)
    assert summary == ""
    assert snapshot == {}


def test_parse_vcd_window_returns_empty_when_failure_cycle_out_of_range() -> None:
    """When the requested failure cycle is past the last recorded
    rising clock edge, the window is empty and the helper falls back
    to empty results rather than raising."""
    vcd = _vcd_with_counter(cycles=3)
    summary, snapshot = parse_vcd_window(vcd, failure_cycle=999, radius=1)
    assert summary == ""
    assert snapshot == {}
