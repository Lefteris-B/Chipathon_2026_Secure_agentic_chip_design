# D10_D silicon test plan: SPI-slave register file

How the packaged `D10_D` die is brought up, functionally tested, and
characterized once it is back from fab, and where the results go. The tests
use the same vectors as the pre-silicon testbench, so a silicon failure can be
traced back through gate-level and RTL simulation.

| | |
|---|---|
| Design | `D10_D` = `spi_slave_regs` core + pad-control tie-offs ([`repro/D10_D/D10_D.v`](../repro/D10_D/D10_D.v)) |
| Spec | [`specs/spi_slave_regs.md`](../specs/spi_slave_regs.md) |
| Layout | [`gds/D10_D.gds`](../gds/D10_D.gds), reproduced by [`repro/`](../repro/README.md) |
| PDK / cells | gf180mcuD, `gf180mcu_fd_sc_mcu7t5v0` (5 V) |
| Signoff clock | 13 ns (76.9 MHz), all 9 corners clean |

Status: **plan**. T1.1 and T1.2 are done. Everything else runs when packaged parts arrive,
and its results go in the [results table](#8-results).

------------------------------------------------------------------------

## 0. Device under test and bench

### 0.1 Pins

The pin names are fixed by the organizer's padframe template. This design
drives them as an SPI bus:

| Pad slot | Template name | Pad cell | Role | Dir |
|---|---|---|---|---|
| W18 | `VSS` | `dvss` | core ground | — |
| W19 | `VDD` | `dvdd` | core supply, 5.0 V nominal | — |
| W20 | `clk` | `in_s` (Schmitt) | system clock (the only clock domain) | in |
| W21 | `rst_n` | `in_c` | async reset, active low | in |
| W22 | `din` | `in_c` | MOSI | in |
| N01 | `load_en` | `in_c` | SCLK (sampled, **not** a clock) | in |
| N02 | `shift_out_en` | `in_c` | CS_n, active low | in |
| N03 | `dout` | `bi_t`, output-only | MISO | out |
| N04 | `done` | `bi_t`, output-only | transaction complete (level) | out |

### 0.2 Board requirements

These follow from how the pads are tied off in RTL. Check them before power-on.

- **No input may float.** PU and PD are tied to 0 on every input pad, so there
  is no on-chip pull. The board drives all five inputs, or fits external pulls:
  `rst_n` pulled low, `shift_out_en` (CS_n) pulled high, `clk`, `load_en` and
  `din` pulled low.
- **5 V I/O.** `dout`/`done` swing to the 5 V rail (16 mA drive setting,
  PDRV=11, fast slew). A 3.3 V master needs level shifting on both outputs and
  5 V-capable drive on the inputs, or a 5 V-tolerant MCU.
- **Reset release.** The core has no internal reset synchronizer: `rst_n`
  goes straight to the flops' async reset. Release `rst_n` **with `clk`
  stopped**, or accept a small chance of a metastable first cycle (test T2.3
  measures this). Hold CS_n high while reset is released.
- `dout`/`done` are plain outputs (OE=1 permanently). Never drive them from
  the board.

### 0.3 Equipment

| Item | Use |
|---|---|
| Bench supply with current readback (mA resolution, current limit) | VDD, IDD |
| SPI master + clock source: RP2040 (PIO generates `clk` and bit-bangs SPI) or FT232H + function generator | all functional tests |
| Oscilloscope, ≥100 MHz, 2+ channels | edges, output levels, MISO timing |
| Logic analyzer (optional) | protocol capture for failures |
| DMM | continuity, DC levels |
| Thermal chamber / heat gun + freeze spray (optional) | T6.4 |

### 0.4 Harness

A host-side Python harness sends the transactions listed in the spec's test
vector tables and checks MISO/`done`. The harness's transaction list is the
same one the pre-silicon cocotb testbench uses
([`runs/spi_slave_regs_r3/tb/`](../runs/spi_slave_regs_r3/tb/)), so a silicon
mismatch maps to a named vector. Each run logs chip ID, VDD, `clk` frequency,
SCLK ratio, temperature, and every transaction (sent / expected / received).
(Harness code: to be added under `test/bringup/` before parts arrive.)

### 0.5 Timing rules the master must respect

From the RTL (`spi_slave_regs` in `repro/D10_D/D10_D.v`):

- SCLK, CS_n, and MOSI are re-timed into `clk` through 2-FF synchronizers.
  The design needs **f_SCLK ≤ f_clk / 8**. The functional baseline uses
  **f_clk / 16** for margin.
- MISO changes 2–3 `clk` cycles (plus pad delay) after each SCLK falling edge.
  At f_clk/8 the master samples half an SCLK period (4 `clk`) after the fall,
  which leaves 1–2 cycles of margin. T6.2 measures where that margin runs out.
- After the 16th rising edge, `done` is valid within about 3 `clk` cycles. Read
  it after raising CS_n.

------------------------------------------------------------------------

## 1. Pre-silicon baseline (the reference silicon is compared to)

| ID | Check | Status / result |
|---|---|---|
| T1.1 | RTL vector sim: spec register vectors + framing tests | **done**: 17 transactions / 13 checked reads, `done` asserted on every frame (run `spi_slave_regs_r3`) |
| T1.2 | Signoff of the shipped macro, 9 corners | **done**: Magic DRC 0 (KLayout DRC is skipped by LibreLane for gf180mcuD), LVS 0, antenna 0, XOR 0, max-slew/cap 0, setup WS +2.80 ns / hold WS +0.19 ns (worst corner), PDN unconnected-shape count 0, IR drop worst 0.40 mV in the re-run (see [`repro/README.md`](../repro/README.md#pdn)) |
| T1.3 | Gate-level sim of `verilog/gl/D10_D.pnl.v` with SDF at ss / tt / ff, same vector set as T1.1, at f_SCLK = f_clk/8 and f_clk/16 | planned before parts arrive |
| T1.4 | Over-long-frame behavior in sim (see §5 note) | planned. Its result becomes the expected value for T5.4/T5.5 |

From the signoff STA (core only, pads excluded): critical path ≈ 10.2 ns at
the slow corner, ≈ 6.5 ns at typical. These figures predict f_clk(max) for T6.1.

------------------------------------------------------------------------

## 2. Power-up, DC, and reset

Each test runs per chip. A chip that fails T2.1 or T2.2 does not proceed.

| ID | Test | Procedure | Pass criterion |
|---|---|---|---|
| T2.1 | Continuity / ESD diodes (unpowered) | DMM diode mode, each signal pad to VSS and to VDD | Forward drop ≈ 0.4–0.8 V both ways on every signal pad; no short (< 10 Ω) VDD–VSS |
| T2.2 | Power-up | Inputs held per §0.2, `clk` off. Ramp VDD 0 → 5.0 V with a 20 mA current limit | No current-limit trip. Record IDD_static |
| T2.3 | Reset | Pulse `rst_n` low ≥ 1 µs with `clk` stopped, release, start `clk`. Repeat 1000× including releases with `clk` running | `done` = 0 and `dout` = 0 after every reset. Count failures when released with `clk` running |
| T2.4 | Dynamic current | IDD at f_clk = 1, 10, 25, 50 MHz, CS_n idle and then under continuous traffic | IDD rises roughly linearly with f_clk. Record the table (signoff estimate: 24.6 mW core at 76.9 MHz, typical corner, default activity) |
| T2.5 | Output levels | `dout`/`done` driving high and low, at 0 and 16 mA load | Within the `gf180mcu_fd_io__bi_t` datasheet VOH/VOL limits at 16 mA (fill limits in from the datasheet before testing) |

T2.2 and T2.4 are also the silicon check of the power path: the pad Metal2,
the `vss_conn`/`vdd_conn` bridge cells, and the core Metal4/5 grid. A chip
that powers up at the expected current and runs T3 at speed shows the grid
is connected. If the slot shares I/O supply rails with neighboring projects,
compare IDD between clock-on and clock-off rather than reading it as an
absolute value.

------------------------------------------------------------------------

## 3. Identity (first sign of life)

Settings: VDD = 5.0 V, f_clk = 10 MHz, f_SCLK = f_clk/16, room temperature.

| ID | Transaction | Expected |
|---|---|---|
| T3.1 | read `0x00` (cmd `0x00`) | MISO = `0x5A`, `done` = 1 |
| T3.2 | read `0x01` | MISO = `0x01`, `done` = 1 |
| T3.3 | read `0x02`, `0x10`–`0x13` right after reset | all `0x00` |

If T3.1 returns `0xB4` or `0x2D` (`0x5A` shifted by one bit), the bit phase is
off by one. Check the SCLK ratio and the master's sampling edge before
suspecting the silicon.

------------------------------------------------------------------------

## 4. Functional

Same settings as §3.

| ID | Test | Pass criterion |
|---|---|---|
| T4.1 | The 13 register vectors from the spec, in order | All 13 match |
| T4.2 | Exhaustive data: for each RW register (`0x02`, `0x10`–`0x13`) write and read back all 256 values | 1280/1280 match |
| T4.3 | Address isolation: write a distinct value to each RW register, then read all of them | No aliasing; each holds its own value |
| T4.4 | Unmapped reads: read every address `0x03`–`0x0F`, `0x14`–`0x7F` | All `0x00` |
| T4.5 | Read-only / unmapped writes: write `0xFF` to `0x00`, `0x01`, and every unmapped address, then re-read `0x00`/`0x01` and the RW registers | ID/VERSION unchanged, RW registers unchanged, `done` = 1 for each write frame |
| T4.6 | Reset clears state: write all RW registers, pulse `rst_n`, read | All `0x00` |

------------------------------------------------------------------------

## 5. Protocol framing

| ID | Scenario | Expected |
|---|---|---|
| T5.1 | 8 SCLK edges, then CS_n high (short frame), then read the target | No write. `done` = 0 |
| T5.2 | Short frame immediately followed by a valid 16-edge frame | Second frame correct |
| T5.3 | CS_n held high, SCLK toggled 100× with MOSI random | No state change. `done` keeps its previous value |
| T5.4 | 17-edge write frame | `done` = 0 at CS_n high. **The write committed on edge 16** (see note) |
| T5.5 | 32-edge frame (two commands back to back under one CS_n) | Characterize. Expected per RTL: acts as two transactions, `done` = 1 (see note) |
| T5.6 | CS_n glitch: CS_n high for 1, 2, 4 `clk` cycles mid-frame | ≥ 2 cycles: frame aborts (T5.1 behavior). Shorter: record what happens |
| T5.7 | Back-to-back frames with minimum CS_n-high time (2, 4, 8 `clk`) | Record the minimum gap that works reliably |

**Note: known spec deviation for over-long frames.** The spec says edges
beyond 16 are ignored and `done` is not asserted. In the RTL the bit counter
wraps to 0 on the 16th edge: edge 17 clears `done`, but edges 17–32 are then
decoded as a *second* command/data pair, and on edge 32 `done` sets and a
write (if RW=1) commits. So a 32-edge frame behaves like two transactions,
and in a 17-edge frame the first write has already been committed. The
pre-silicon vector set does not cover this. T1.4 confirms it in simulation,
and T5.4/T5.5 record what silicon does. This is a protocol-level deviation,
not a silicon defect: a master that sends 16-edge frames (all tests outside
§5.4–5.5) never triggers it.

------------------------------------------------------------------------

## 6. Margins and corners

The design has only two outputs and no scan chain or clock-out, so every
internal timing limit is found by sweeping and watching for SPI errors. Each
sweep point runs the full T4.1 + T4.2 suite. A point **passes** when it shows
zero mismatches in 3 consecutive runs.

| ID | Sweep | Range | Result recorded |
|---|---|---|---|
| T6.1 | f_clk at fixed ratio f_SCLK = f_clk/16 | 1 MHz → failure, 5 MHz steps above 25 MHz | f_clk(max). Compare with STA (§1): ~98 MHz ss / ~150 MHz tt core-only, less once pad delays count |
| T6.2 | SCLK ratio at f_clk = 10 MHz | f_clk/32, /16, /12, /10, /8, /7, /6, /5, /4 | Fastest working ratio. Spec minimum is /8 |
| T6.3 | VDD | 4.50, 4.75, 5.00, 5.25, 5.50 V, at f_clk = 10 MHz and at 0.8 × f_clk(max) | Shmoo plot VDD × f_clk |
| T6.4 | Temperature (optional) | cold (≈ 0 °C), room, hot (≈ 85 °C) at 5.0 V | f_clk(max) per temperature |

------------------------------------------------------------------------

## 7. Robustness

| ID | Test | Pass criterion |
|---|---|---|
| T7.1 | Soak: ≥ 8 h of random reads/writes (random address incl. unmapped, random data) at f_clk = 0.8 × f_clk(max), checked against a Python model of the register file | 0 mismatches. Record transaction count |
| T7.2 | Reset mid-frame: assert `rst_n` at a random SCLK edge 1000× | Next frame after reset is correct. Registers read `0x00` |
| T7.3 | Chip-to-chip spread: T3, T4.1, T6.1 on every available part | Pass/fail per chip + f_clk(max) distribution |

------------------------------------------------------------------------

## 8. Results

Filled per chip. Raw harness logs go in `test/results/<chip-id>/`.

| ID | Chip | Date | VDD | f_clk | Ratio | Temp | Result | Notes |
|---|---|---|---|---|---|---|---|---|
| T2.1 | | | | | | | | |
| T2.2 | | | | | | | | |
| T2.3 | | | | | | | | |
| T2.4 | | | | | | | | |
| T2.5 | | | | | | | | |
| T3.1–3.3 | | | | | | | | |
| T4.1–4.6 | | | | | | | | |
| T5.1–5.7 | | | | | | | | |
| T6.1 | | | | | | | | |
| T6.2 | | | | | | | | |
| T6.3 | | | | | | | | |
| T6.4 | | | | | | | | |
| T7.1–7.3 | | | | | | | | |

### Debug ladder (when something fails)

1. Fails T2: power/assembly. Check bond map vs §0.1, shorts, supply.
2. Passes T2, fails T3: drop to f_clk = 1 MHz, ratio /32. Probe SCLK/CS_n/MOSI
   at the pads and MISO/`done` with the scope. Check CS_n was high during reset.
3. Fails a specific vector: re-run that vector in the T1.3 gate-level sim at
   the same f_clk/ratio. If the sim also fails, the problem is the protocol or
   the master. If only silicon fails, record it as a silicon finding with VDD/f_clk/temp.
