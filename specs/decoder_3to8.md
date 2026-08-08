# 3-to-8 line decoder with active-low enable

A gf180mcu demo design used by the F12.6 spec-shape generality test: a
purely combinational 3-to-8 line decoder driven by a 3-bit selector and
gated by an active-low enable. Single-cycle, no registers.

## Module

* Name: `decoder_3to8`
* Top-level module ID: `decoder_3to8`

## Ports

* `sel` — input, 3 bits, selector. `sel == k` asserts output bit `k`.
* `en_n` — input, 1 bit, active-low enable. When high, all outputs are zero.
* `y` — output, 8 bits, one-hot decoded output.

## Behaviour

A purely combinational block:

* When `en_n` is high (enable inactive), `y == 8'b0000_0000` regardless of `sel`.
* When `en_n` is low (enable active), exactly one bit of `y` is asserted high
  — the bit at position `sel`. Specifically, `y == (8'b0000_0001 << sel)`.

## Reset / clock

None. The decoder is purely combinational — no `clk`, no reset.

## Implementation note

A typical implementation uses a Verilog `case (sel)` block returning the
one-hot value, ANDed with `~en_n` to gate the output. Equivalent forms
using a shift are also acceptable.

## Constraints

* Target clock period: 10 ns (STA reference clock).
* Target utilization: 30%.
* PDK: gf180mcuD.
