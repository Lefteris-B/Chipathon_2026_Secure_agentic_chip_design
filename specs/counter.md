# 8-bit synchronous up-counter

A small gf180mcu demo design: an 8-bit synchronous up-counter with
asynchronous active-low reset and an enable input.

## Module

* Name: `counter`
* Top-level module ID: `counter`

## Ports

* `clk` — input, 1 bit, primary clock.
* `rst_n` — input, 1 bit, asynchronous active-low reset.
* `en` — input, 1 bit, count enable (synchronous).
* `q` — output, 8 bits, current count value.

## Reset

Asynchronous active-low reset. When `rst_n` is low, `q` clears to 0.
Reset is released synchronously to `clk`.

## Behaviour

On the rising edge of `clk`, while `rst_n` is high and `en` is high,
`q` increments by 1 (modulo 256). While `en` is low, `q` holds.

## Constraints

* Target clock period: 10 ns.
* Target utilization: 50%.
* PDK: gf180mcuD.
