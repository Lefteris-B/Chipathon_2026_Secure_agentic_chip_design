"""F19.8 differential cocotb testbench for ``present80``.

Stim + expected outputs are embedded from the OracleArtifact at build
time; the DUT is compared cycle-by-cycle against the oracle's reference
outputs. On any mismatch the test fails with the canonical token
``DIFF|cycle=N|signal=S|expected=E|actual=A`` so
:func:`chip_agent.tools.trace.parse_failing_assertion` extracts
structured fields without fuzzy regex.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

STIM = [{'clk': 1, 'rst_n': 0, 'start': 0, 'plaintext': 0, 'key': 0},
 {'clk': 1, 'rst_n': 1, 'start': 0, 'plaintext': 0, 'key': 0},
 {'clk': 1, 'rst_n': 1, 'start': 0, 'plaintext': 0, 'key': 0},
 {'clk': 1, 'rst_n': 1, 'start': 0, 'plaintext': 0, 'key': 0},
 {'clk': 1, 'rst_n': 1, 'start': 0, 'plaintext': 0, 'key': 0},
 {'clk': 1, 'rst_n': 1, 'start': 0, 'plaintext': 0, 'key': 0},
 {'clk': 1,
  'rst_n': 1,
  'start': 1,
  'plaintext': 18446744073709551615,
  'key': 1208925819614629174706175},
 {'clk': 1,
  'rst_n': 1,
  'start': 1,
  'plaintext': 18446744073709551615,
  'key': 1208925819614629174706175},
 {'clk': 1,
  'rst_n': 1,
  'start': 1,
  'plaintext': 18446744073709551615,
  'key': 1208925819614629174706175},
 {'clk': 1,
  'rst_n': 1,
  'start': 1,
  'plaintext': 18446744073709551615,
  'key': 1208925819614629174706175},
 {'clk': 1, 'rst_n': 1, 'start': 1, 'plaintext': 1, 'key': 1},
 {'clk': 1, 'rst_n': 1, 'start': 0, 'plaintext': 2, 'key': 2},
 {'clk': 1, 'rst_n': 1, 'start': 1, 'plaintext': 3, 'key': 3},
 {'clk': 1, 'rst_n': 1, 'start': 0, 'plaintext': 4, 'key': 4},
 {'clk': 1, 'rst_n': 1, 'start': 1, 'plaintext': 5, 'key': 5},
 {'clk': 1, 'rst_n': 1, 'start': 0, 'plaintext': 6, 'key': 6},
 {'clk': 1,
  'rst_n': 1,
  'start': 1,
  'plaintext': 16422101724900707500,
  'key': 979069905495959290439266},
 {'clk': 1,
  'rst_n': 1,
  'start': 1,
  'plaintext': 8791662011684601223,
  'key': 336736176150837069602009},
 {'clk': 1,
  'rst_n': 1,
  'start': 1,
  'plaintext': 13942126818862981423,
  'key': 605695141290847876509244},
 {'clk': 1,
  'rst_n': 1,
  'start': 0,
  'plaintext': 6091063652223914538,
  'key': 855414273795512157074206},
 {'clk': 1,
  'rst_n': 1,
  'start': 1,
  'plaintext': 8799277236024914180,
  'key': 629836800185805914305438},
 {'clk': 1,
  'rst_n': 1,
  'start': 0,
  'plaintext': 259023674760142153,
  'key': 964300596656784761852140},
 {'clk': 1,
  'rst_n': 1,
  'start': 0,
  'plaintext': 6145258598325499690,
  'key': 461962973692725283992552},
 {'clk': 1,
  'rst_n': 1,
  'start': 0,
  'plaintext': 1682637359498011204,
  'key': 728851316858544900400642}]
EXPECTED = [{'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0},
 {'ciphertext': 0, 'done': 0}]
RESET_RELEASE_CYCLE = 1
CLK = 'clk'
OUTPUT_PORTS = ['ciphertext', 'done']


@cocotb.test()
async def test_present80_diff(dut):
    """Drive STIM through the DUT; assert outputs match EXPECTED.

    F19.4d: ``clk`` is driven solely by the Clock() coroutine. The
    per-row write loops skip the CLK port to avoid contention with
    the clock driver (writing dut.clk=1 every row stalls the
    rising-edge generator, so @(posedge clk) never fires).

    F19.4e: a 1ns Timer delay between the rising edge and the
    output read lets NBAs from @(posedge clk) settle, so reads
    observe the post-edge value rather than the pre-edge one.
    (RisingEdge fires in the active region BEFORE the non-blocking
    assignment region settles.)
    """
    cocotb.start_soon(Clock(getattr(dut, CLK), 10, units="ns").start())

    for port, value in STIM[0].items():
        if port == CLK:
            continue
        getattr(dut, port).value = value
    await RisingEdge(getattr(dut, CLK))

    for i in range(1, len(STIM)):
        for port, value in STIM[i].items():
            if port == CLK:
                continue
            getattr(dut, port).value = value
        await RisingEdge(getattr(dut, CLK))

        if i < RESET_RELEASE_CYCLE:
            continue
        await Timer(1, units="ns")
        for port in OUTPUT_PORTS:
            expected = EXPECTED[i].get(port)
            if expected is None:
                # Oracle did not pin this output at this cycle; skip
                # so we don't surface a spurious DIFF on an unmodeled
                # signal.
                continue
            actual = int(getattr(dut, port).value)
            assert actual == expected, (
                f"DIFF|cycle={i}|signal={port}|"
                f"expected={expected}|actual={actual}"
            )
