# 4-bit combinational ALU

A gf180mcu demo design used by the F12.6 spec-shape generality test: a
purely combinational 4-bit ALU with four operations selected by a
2-bit opcode. Single-cycle latency, no registers.

## Module

* Name: `alu_4bit`
* Top-level module ID: `alu_4bit`

## Ports

* `a` — input, 4 bits, first operand.
* `b` — input, 4 bits, second operand.
* `op` — input, 2 bits, operation selector.
* `result` — output, 4 bits, computed result (LSB-aligned).
* `cout` — output, 1 bit, carry-out (only meaningful for `op == 2'b00` add).

## Operations

The `op` selector picks one of four combinational operations:

* `op == 2'b00` (ADD): `{cout, result} = a + b`. `cout` is the carry-out from
  the 4-bit add.
* `op == 2'b01` (SUB): `result = a - b`. `cout` is `0` for SUB.
* `op == 2'b10` (AND): `result = a & b`. `cout` is `0`.
* `op == 2'b11` (OR):  `result = a | b`. `cout` is `0`.

## Reset / clock

None. The ALU is purely combinational — no `clk`, no reset.

## Behaviour

A single-cycle, glitch-tolerant combinational block. The implementation
typically uses a Verilog `case` statement on `op` and a 5-bit intermediate
sum to capture the carry-out for ADD.

## Constraints

* Target clock period: 10 ns (used by the surrounding flow even though the
  block itself has no clock — STA still needs a virtual reference).
* Target utilization: 30%.
* PDK: gf180mcuD.
