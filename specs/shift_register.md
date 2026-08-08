# 4-bit synchronous shift register

A gf180mcu demo design used by the F10.5 spec-shape generality test: a
4-bit MSB-first serial-in/parallel-out shift register with asynchronous
active-low reset.

## Module

* Name: `shift_register`
* Top-level module ID: `shift_register`

## Ports

* `clk` — input, 1 bit, primary clock.
* `rst_n` — input, 1 bit, asynchronous active-low reset.
* `serial_in` — input, 1 bit, new bit shifted in on every clock.
* `q` — output, 4 bits, current shift-register contents.

## Reset

Asynchronous active-low reset. When `rst_n` is low, `q` clears to 0.

## Behaviour

On the rising edge of `clk`, while `rst_n` is high, the register shifts
left by one position — `q[3:1]` takes the previous `q[2:0]` values and
`q[0]` takes `serial_in`. When `rst_n` is low, `q` holds at 0.

## Constraints

* Target clock period: 10 ns.
* Target utilization: 30%.
* PDK: gf180mcuD.
