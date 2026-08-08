# UART receiver (gf180mcu demo)

A configurable UART receiver: deserialises an 8N1 frame at 115200 baud,
oversampled 16x for noise tolerance. F19.3 (and downstream M19 features)
use this as the canonical "non-trivial state-machine" spec — it exercises
encoding parameters that the contract extractor must capture as typed
fields.

## Module

* Name: `uart_rx`
* Top-level module ID: `uart_rx`

## Ports

* `clk` — input, 1 bit, oversampling clock (16 * baud).
* `rst_n` — input, 1 bit, synchronous active-low reset.
* `rx` — input, 1 bit, serial data line (idle high).
* `data` — output, 8 bits, last received byte.
* `valid` — output, 1 bit, pulses high for one `clk` when `data` is fresh.
* `framing_error` — output, 1 bit, pulses high when the stop bit is not high.

## Reset

Synchronous active-low reset. Holds the FSM in IDLE and clears `valid` /
`framing_error`.

## Protocol parameters

* Baud rate: 115200 bps.
* Frame: 8 data bits, no parity, 1 stop bit (8N1).
* Oversampling: 16x — the receiver samples in the middle of each
  oversampled cell to align with the bit centre.

## Behaviour

On the falling edge of `rx` (while idle), the FSM advances to the
START_DETECT state, waits 8 oversample ticks (half a bit), and confirms
`rx` is still low before entering DATA. Each data bit is sampled at the
middle of its 16-tick window; bits are shifted in LSB-first. After 8
data bits the FSM enters STOP. If `rx` is high at the middle of the
stop-bit window, `data` is latched and `valid` pulses for one `clk`;
otherwise `framing_error` pulses and `data` is not updated.

## Constraints

* Target clock period: 50 ns (20 MHz oversampling clock for 115200 baud * 16).
* Target utilization: 50%.
* PDK: gf180mcuD.
