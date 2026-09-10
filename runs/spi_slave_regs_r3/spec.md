# SPI-slave register file (gf180mcu, D10 pin-constrained)

An SPI **slave** peripheral exposing an 8-bit-addressed register file.
Designed to fit the IEEE SCSS Chipathon Track D `D10_D` padframe slot, which
provides only **9 user pins** — so the SPI bus is the chip's entire external
interface.

Correctness against the test vectors below is the acceptance criterion. A
design that never asserts `done`, that mis-frames a transaction, or that
returns the wrong byte on a read is a failure even if it elaborates cleanly.

## Module

* Name: `spi_slave_regs`
* Top-level module ID: `spi_slave_regs`

Implement as a **single flat Verilog module** named `spi_slave_regs`. Do
**not** decompose it into sub-modules. All internal sequencing (bit counters,
byte phase) is internal and must not be exposed as a port.

**Coding-style requirement (for clean synthesis):** the RTL must be
**synthesizable by Yosys**, not merely simulatable — Verilator accepts some
constructs Yosys rejects, so write portable, standard Verilog-2005/2012:

* Do **not** use Verilog `function` constructs. Express the read mux as a
  combinational `always @*` block with a `case`.
* Do **not** apply a part-select or bit-index to a concatenation or other
  expression — e.g. `{hi, lo}[7:0]` is **illegal** in synthesizable Verilog.
  Slice declared `reg`/`wire` names directly.
* The 4 `SCRATCH` bytes must be **4 discrete registers with an explicit
  `case` decode**, *not* a `reg [7:0] scratch [0:3]` array indexed by a
  variable. Yosys expands a variable-indexed array via its `mem2reg` pass,
  which leaves undriven `$mem2reg_rd$` / `$mem2reg_wr$` wires; LibreLane's
  `Checker.YosysSynthChecks` counts each such wire bit as an error and
  **aborts the flow**. Write it as:

  ```verilog
  reg [7:0] scratch0, scratch1, scratch2, scratch3;

  // read: one combinational mux
  always @* begin
      case (addr[1:0])
          2'd0:    scratch_rd = scratch0;
          2'd1:    scratch_rd = scratch1;
          2'd2:    scratch_rd = scratch2;
          default: scratch_rd = scratch3;
      endcase
  end

  // write: one decoded enable per register
  always @(posedge clk or negedge rst_n) begin
      if (!rst_n) begin
          scratch0 <= 8'h00; scratch1 <= 8'h00;
          scratch2 <= 8'h00; scratch3 <= 8'h00;
      end else if (scratch_we) begin
          case (scratch_addr)
              2'd0: scratch0 <= scratch_wdata;
              2'd1: scratch1 <= scratch_wdata;
              2'd2: scratch2 <= scratch_wdata;
              default: scratch3 <= scratch_wdata;
          endcase
      end
  end
  ```

  Do **not** instantiate a memory macro or infer a RAM primitive either.
* Every net must have **exactly one driver**, and every declared signal must
  be driven on all paths. A signal that is read but never assigned (Verilator
  `UNDRIVEN_NET`) fails the elaborate gate — when restructuring logic, check
  that every intermediate you introduce is actually driven.
* The RTL is checked with `yosys -p "read_verilog -sv; hierarchy -check -top;
  proc; check"` and must report **`Found and reported 0 problems`**. This is
  the same check LibreLane runs; passing Verilator is necessary but not
  sufficient.

## Interface — why it looks like this

The `D10_D` padframe slot fixes the available pads. This design maps the SPI
bus onto them one-for-one, so the top-level wrapper is a pure rename:

| `D10_D` pad     | pad cell               | this module | role                    |
|-----------------|------------------------|-------------|-------------------------|
| `clk`           | `gf180mcu_fd_io__in_s` | `clk`       | system clock            |
| `rst_n`         | `gf180mcu_fd_io__in_c` | `rst_n`     | async active-low reset  |
| `load_en`       | `gf180mcu_fd_io__in_c` | `sclk`      | SPI clock (**sampled**) |
| `shift_out_en`  | `gf180mcu_fd_io__in_c` | `cs_n`      | chip select, active low |
| `din`           | `gf180mcu_fd_io__in_c` | `mosi`      | master-out slave-in     |
| `dout`          | `gf180mcu_fd_io__bi_t` | `miso`      | master-in slave-out     |
| `done`          | `gf180mcu_fd_io__bi_t` | `done`      | transaction complete    |

## Ports

* `clk` — input, 1 bit. Free-running system clock. **The only clock in the
  design.**
* `rst_n` — input, 1 bit, asynchronous active-low reset, released
  synchronously to `clk`.
* `sclk` — input, 1 bit. SPI serial clock **from the master**.
* `cs_n` — input, 1 bit. SPI chip select, active low.
* `mosi` — input, 1 bit. SPI serial data in, MSB-first.
* `miso` — output, 1 bit. SPI serial data out, MSB-first. Drives `0` when
  `cs_n` is high (a plain output, not a tri-state — the pad cell handles
  output enable at the wrapper level).
* `done` — output, 1 bit. Asserted when a complete, well-formed 16-bit
  transaction has finished; see **`done` semantics** below.

## Critical: `sclk` is sampled, never used as a clock

`sclk`, `cs_n`, and `mosi` arrive on **ordinary input pads**, not clock pads,
and are asynchronous to `clk`. The design is therefore **fully synchronous to
`clk` alone**:

* Synchronise `sclk`, `cs_n`, and `mosi` into the `clk` domain with **2-stage
  flip-flop synchronisers**.
* Detect `sclk` rising and falling edges by comparing the synchronised value
  against its previous registered value (a third flop). Do the same for
  `cs_n` assertion/deassertion.
* **Never** write `always @(posedge sclk)`, never declare `sclk` as a clock,
  and never gate `clk` with anything. There must be exactly one clock domain.

**Synchroniser hazard — read this before writing the FSM.** On the clock cycle
where an edge is detected, the synchronised *level* still holds its **old**
value: `cs_fall` is `cs_sync[2:1] == 2'b10`, which means `cs_sync[2]` is still
`1` on that very cycle. So a block shaped like

```verilog
if (cs_fall)  active <= 1'b1;   // frame begins
...
if (cs_sync[2]) active <= 1'b0; // still 1 on this cycle -> cancels the above
```

silently never starts a frame: the later non-blocking assignment wins and
`active` never goes high. Do **not** gate on an edge and on the corresponding
level in the same `always` block. Derive one signal (e.g. `frame_active`) from
the edges alone, and gate everything else on that.

**Frequency constraint:** the master drives `sclk` at no more than `clk / 8`.

## SPI protocol

**Mode 0** (CPOL=0, CPHA=0), MSB-first:

* `sclk` idles low.
* `mosi` is sampled by the slave on the **rising** edge of `sclk`.
* `miso` is updated by the slave on the **falling** edge of `sclk`, so it is
  stable when the master samples it on the next rising edge.

A transaction is exactly **16 `sclk` rising edges** while `cs_n` is low:

```
  byte 0 (command):  cmd[7:0] = { RW, addr[6:0] }
                     RW = 1 -> write, RW = 0 -> read
  byte 1 (data):     write -> payload the master sends on mosi
                     read  -> slave returns the register value on miso
```

* During byte 0, `miso` drives `0`.
* On a **write**, the register is updated at the end of byte 1 (on the 16th
  rising edge). `miso` drives `0` throughout byte 1.
* On a **read**, the slave drives the addressed register onto `miso`
  MSB-first during byte 1, starting on the falling edge that follows the 8th
  rising edge. Whatever the master sends on `mosi` during byte 1 is ignored.

**Framing:** `cs_n` going high **resets the bit counter and byte phase**. A
transaction that receives fewer than 16 `sclk` rising edges before `cs_n`
deasserts is **aborted**: no register is written, and `done` is not asserted.
A transaction that receives more than 16 rising edges before `cs_n` deasserts
is likewise invalid — ignore the extra edges and do not assert `done`.

## `done` semantics

`done` is a level, not a pulse. It is driven **entirely from the `sclk`
rising-edge count** — do *not* correlate it with a `cs_n` edge:

* Cleared to 0 by reset.
* Cleared to 0 when `cs_n` asserts (goes low) to begin a new transaction.
* **Set to 1 on the 16th `sclk` rising edge of a frame** — the same edge that
  commits a write.
* Cleared to 0 again on any *further* rising edge in the same frame (a 17th
  or later edge makes the frame over-long and therefore invalid).
* Otherwise held at its value, so it is still 1 when the master releases
  `cs_n` and reads the pin.

This gives the required behaviour without needing to correlate the bit
counter with a chip-select edge: a short frame never reaches 16 edges, so
`done` stays 0; an over-long frame clears it again; a well-formed frame
leaves it high for the master to observe.

`done` must reach 1 for every well-formed transaction. An implementation
whose `done` never asserts is a failure regardless of other behaviour.

## Register map

Addresses are `cmd[6:0]`. Reads of unmapped addresses return `0x00`; writes
to unmapped or read-only addresses are ignored (and still assert `done`).

| addr        | name      | access | reset | description                        |
|-------------|-----------|--------|-------|------------------------------------|
| `0x00`      | `ID`      | RO     | `0x5A`| Fixed device identifier.            |
| `0x01`      | `VERSION` | RO     | `0x01`| Fixed version.                      |
| `0x02`      | `CTRL`    | RW     | `0x00`| General-purpose R/W control byte.   |
| `0x10..0x13`| `SCRATCH` | RW     | `0x00`| 4 general-purpose R/W bytes.        |

`CTRL` has no side effects — it is a plain readable/writable byte.

## Reset

Asynchronous active-low reset, released synchronously to `clk`. On reset:
bit counter and byte phase clear, `done` clears to **0** (never 1), `miso` drives 0,
`CTRL` clears to `0x00`, and all 4 `SCRATCH` bytes clear to `0x00`.

## Test vectors (acceptance criteria)

Command byte encoding: "read `0x10`" is `cmd = 0x10`; "write `0x10`" is
`cmd = 0x90` (RW bit set).

| # | transaction                 | expected                          |
|---|-----------------------------|-----------------------------------|
| 1 | read `0x00`                 | `miso` returns `0x5A`             |
| 2 | read `0x01`                 | `miso` returns `0x01`             |
| 3 | write `0x10` = `0xA5`       | `done` asserts                    |
| 4 | read `0x10`                 | `miso` returns `0xA5`             |
| 5 | write `0x13` = `0x3C`       | `done` asserts                    |
| 6 | read `0x13`                 | `miso` returns `0x3C`             |
| 7 | read `0x10`                 | `miso` returns `0xA5` (unchanged) |
| 8 | write `0x02` = `0x77`       | `done` asserts                    |
| 9 | read `0x02`                 | `miso` returns `0x77`             |
|10 | write `0x11` = `0x0F`       | `done` asserts                    |
|11 | read `0x11`                 | `miso` returns `0x0F`             |
|12 | read `0x7E`                 | `miso` returns `0x00`             |
|13 | read `0x13`                 | `miso` returns `0x3C` (unchanged) |

### Framing

| # | scenario                                                    | expected                          |
|---|-------------------------------------------------------------|-----------------------------------|
| 1 | 8 `sclk` edges then `cs_n` high (short frame)               | no write occurs, `done` stays 0   |
| 2 | short frame, then a full valid 16-edge frame                | second frame works normally       |
| 3 | `cs_n` held high, `sclk` toggles                            | nothing happens, `done` unchanged |

## Correctness checklist (common failure modes)

* `done` must actually assert — verify it reaches 1 after a full 16-edge
  frame, not merely that a testbench ran without mismatches. Drive it from
  the rising-edge count alone; correlating it with a `cs_n` edge is the
  single most common way this design fails.
* Exactly **one** clock domain. `sclk` is sampled through a 2-FF
  synchroniser and edge-detected; it is never in a sensitivity list.
* `mosi` is sampled on **rising** `sclk`, `miso` updated on **falling**
  `sclk`. Getting this backwards yields an off-by-one-bit readback.
* Both bytes are **MSB-first**; the command byte precedes the data byte.
* The RW bit is `cmd[7]`, with `1` = write.
* Register writes commit at the **end** of byte 1, not per-bit.
* A read must present the register value starting from the falling edge
  after the 8th rising edge — not one bit late.
* `cs_n` deassertion resets the bit counter; a short frame must not commit a
  partial write.

## Constraints

* Target clock period: 10 ns.
* Target utilization: 20%.
* PDK: gf180mcuD.
