# Gate-level simulation (T1.3)

Replays the 17 pre-silicon SPI transactions
([`runs/spi_slave_regs_r3/tb/`](../../runs/spi_slave_regs_r3/tb/)) on the shipped netlist
`verilog/gl/D10_D.pnl.v` at the 13 ns clock, with SDF extracted from the shipped routed DEF.

```bash
test/gl/run.sh     # exits non-zero if any run differs from its expected result
```

## What it runs

| Timing | RC | SCLK ratios |
|---|---|---|
| Zero-delay | none | clk/16, clk/8 |
| ss 125C 4.5V | max | clk/16, clk/8 |
| tt 25C 5.0V | nom | clk/16, clk/8 |
| ff -40C 5.5V | min | clk/16, clk/8 |

A pass needs 13 of 13 reads correct, `done` on 17 of 17 frames and all 26 tie-off pins at
their RTL values. X counts as a failure. The script also requires:

- injected faults (wrong expected value, stuck-low MOSI) to **fail** under SDF timing,
- `done` delay ordered ss > tt > ff, which shows the SDF reached the simulation,
- no unmatched cell arcs.

## Result (2026-11-06)

All 8 runs pass, and all 3 injected-fault runs fail as they should. `done` delay after the
clock edge is 5.75 ns (ss), 3.17 ns (tt) and 2.02 ns (ff). The SDFs reproduce signoff STA for
ss setup (+2.319 vs +2.316 ns) and for hold at all three corners (+1.155, +0.485, +0.200 ns).

## What it does not cover

- **Setup and hold:** iverilog ignores SDF timing checks, so these rest on signoff STA.
- **Pads:** the testbench drives the core ports directly, so pad delays are not included.
- **Metastability:** inputs change on the falling clock edge, so the synchronizers are not
  exercised at the setup/hold boundary.
- **Over-long frames:** T1.4 in [`docs/TEST_PLAN.md`](../../docs/TEST_PLAN.md).

## iverilog pitfalls handled here

Each of these looks like a clean run or a false failure:

- **Timescale must compile first.** Otherwise cell delays compile as 1 second and every output
  stays X.
- **Empty typical field.** `write_sdf` emits `(a::b)` and iverilog reads typical, so delays
  stay at the library default of 1 ns and the run still passes. `sim.sh` fills the field in.
- **Edge-sensitive paths.** `(posedge S => (Z : ...))` does not match SDF `IOPATH S Z`, leaving
  114 arcs at 1 ns. `lib_sim.py` rewrites only `specify` text and checks the logic is unchanged.

`D10_D.sdc` is a copy of the flow's final SDC. Its I/O delays differ from signoff, so tt and ff
worst setup slacks read about 0.6 ns higher than the signoff report. That does not affect the SDF.
