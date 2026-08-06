# PRESENT-80 lightweight block cipher (encryption, iterative)

A gf180mcu proof-of-concept: the PRESENT-80 block cipher in its
canonical encryption-only form, implemented as an **iterative** datapath
that reuses one round of logic across 31 rounds (one round per clock).

PRESENT is a 64-bit block cipher with an 80-bit key and 31 rounds,
standardised in ISO/IEC 29192-2. This design targets the smallest,
simplest correct implementation suitable for a first end-to-end flow.

## Module

* Name: `present80`
* Top-level module ID: `present80`

Implement the entire cipher as a **single flat Verilog module** named
`present80`. Do **not** decompose it into sub-modules (no separate S-box,
pLayer, key-schedule, or FSM modules) — inline every layer as
combinational logic and `always` blocks within this one module. The
module's only observable outputs are `ciphertext` and `done`; all internal
sequencing (round counter, state machine phase) is an unspecified
implementation detail and must not be exposed as a port.

## Ports

* `clk` — input, 1 bit, primary clock.
* `rst_n` — input, 1 bit, asynchronous active-low reset.
* `start` — input, 1 bit. A single-cycle pulse latches `plaintext`/`key`
  and begins an encryption.
* `plaintext` — input, 64 bits, the block to encrypt.
* `key` — input, 80 bits, the encryption key.
* `ciphertext` — output, 64 bits, the encrypted block. Valid when `done` is high.
* `done` — output, 1 bit. Asserted for one cycle when `ciphertext` is valid.

## Reset

Asynchronous active-low reset. When `rst_n` is low, the core returns to
idle: `done` clears to 0 and any in-progress encryption is abandoned.
Reset is released synchronously to `clk`.

## Algorithm

Encryption maintains a 64-bit `state` and an 80-bit `key_reg`. On `start`,
`state` is loaded from `plaintext` and `key_reg` from `key`. Each round
`i` (i = 1 .. 31) performs, in order:

1. **addRoundKey** — `state = state XOR roundKey`, where `roundKey` is the
   leftmost (most significant) 64 bits of `key_reg`, i.e. `key_reg[79:16]`.
2. **sBoxLayer** — apply the 4-bit S-box independently to each of the 16
   nibbles of `state`.
3. **pLayer** — the bit permutation: bit at position `i` of the input moves
   to position `P(i)`, where `P(i) = (i * 16) mod 63` for `i` in 0..62 and
   `P(63) = 63`.

After the 31st round, one final **addRoundKey** XORs `state` with the
32nd round key (`key_reg[79:16]` after the 31st key update). The result is
`ciphertext`.

### S-box (4-bit, hex)

Input nibble `x` (0..F) maps to `S[x]`:

| x    | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | A | B | C | D | E | F |
|------|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S[x] | C | 5 | 6 | B | 9 | 0 | A | D | 3 | E | F | 8 | 4 | 7 | 1 | 2 |

### Key schedule

The round key for round `i` is `key_reg[79:16]` (the 64 MSBs). After each
round's key is used, `key_reg` is updated for the next round:

1. Rotate the 80-bit register left by 61 bits:
   `key_reg = (key_reg << 61) | (key_reg >> 19)`.
2. Pass the top nibble through the S-box:
   `key_reg[79:76] = S[key_reg[79:76]]`.
3. XOR the 5-bit round counter into bits `[19:15]`:
   `key_reg[19:15] = key_reg[19:15] XOR round_counter`, where
   `round_counter` is the round number `i` (1..31), 5 bits wide.

## Behaviour

Idle until `start` pulses. On `start`, latch inputs and run 31 rounds at
one round per clock (a small round counter sequences them), then perform
the final addRoundKey. When the block is complete, drive `ciphertext` and
pulse `done` high for one cycle, then return to idle. Encryption latency
is deterministic (order of ~32 cycles); exact cycle count is an
implementation detail as long as `done` marks valid output.

## Test vectors

Standard PRESENT-80 vectors (all values hex, MSB-first):

| key                        | plaintext          | ciphertext         |
|----------------------------|--------------------|--------------------|
| `00000000000000000000`     | `0000000000000000` | `5579C1387B228445` |
| `FFFFFFFFFFFFFFFFFFFF`      | `0000000000000000` | `E72C46C0F5945049` |
| `00000000000000000000`     | `FFFFFFFFFFFFFFFF`  | `A112FFC72F68417B` |
| `FFFFFFFFFFFFFFFFFFFF`      | `FFFFFFFFFFFFFFFF`  | `3333DCD3213210D2` |

## Constraints

* Target clock period: 10 ns.
* Target utilization: 30%.
* PDK: gf180mcuD.
