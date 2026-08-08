# 3-state traffic-light FSM

A gf180mcu demo design used by the F12.6 spec-shape generality test: a
three-state Moore traffic-light finite state machine with asynchronous
active-low reset and an external tick signal that drives transitions.

## Module

* Name: `fsm_traffic_light`
* Top-level module ID: `fsm_traffic_light`

## Ports

* `clk` — input, 1 bit, primary clock.
* `rst_n` — input, 1 bit, asynchronous active-low reset.
* `tick` — input, 1 bit, advance signal (every assertion advances one state).
* `state` — output, 2 bits, current encoded state.

## Reset

Asynchronous active-low reset. When `rst_n` is low, `state` clears to `2'b00`
(`RED`) and the FSM holds in that state until reset is released.

## State encoding

A binary-encoded three-state Moore machine:

* `RED    = 2'b00`
* `GREEN  = 2'b01`
* `YELLOW = 2'b10`

The fourth code point (`2'b11`) is unreachable and falls back to `RED` on
the next clock if ever entered (defensive coding for robustness).

## Behaviour

On the rising edge of `clk`, while `rst_n` is high:

* If `tick` is low, `state` holds.
* If `tick` is high, the FSM advances one state: `RED -> GREEN -> YELLOW -> RED`.

The output `state` is the registered state value (Moore — output is a pure
function of the present state, registered to be glitch-free).

## Constraints

* Target clock period: 10 ns.
* Target utilization: 30%.
* PDK: gf180mcuD.
