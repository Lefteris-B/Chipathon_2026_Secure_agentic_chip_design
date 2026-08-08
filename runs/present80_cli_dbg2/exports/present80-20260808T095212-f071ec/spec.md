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
combinational logic and `always` blocks within this one module. All
internal sequencing (round counter, state-machine phase, load/unload bit
counters) is an unspecified implementation detail and must not be exposed
as a port.

## Interface (pin-constrained, bit-serial)

This design is **pin-constrained**: the full 80-bit key, 64-bit plaintext,
and 64-bit ciphertext are streamed **one bit per clock** over single-bit
ports rather than presented on parallel buses. This keeps the design to
**7 physical I/O pins** (each bus bit would otherwise become its own die
pin — a parallel interface would need ~212). The internal
`{key_reg[79:0], state[63:0]}` register pair doubles as the input shift
chain during load, so the added logic is just a shift path plus small
bit counters.

## Ports

* `clk` — input, 1 bit, primary clock.
* `rst_n` — input, 1 bit, asynchronous active-low reset.
* `load_en` — input, 1 bit. While high, one bit is shifted in from `din`
  per rising clock edge.
* `din` — input, 1 bit, serial data in.
* `shift_out_en` — input, 1 bit. While high (and `done` asserted), the
  ciphertext is shifted out one bit per rising clock edge on `dout`.
* `dout` — output, 1 bit, serial data out.
* `done` — output, 1 bit. Asserted once the ciphertext is fully computed
  and held high while the result is available to shift out; clears on
  reset or when a new load begins.

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

The core has three phases, sequenced by internal counters:

1. **Load (144 cycles).** While `load_en` is high, shift `din` MSB-first
   into the internal register chain: the first 80 bits are the key
   (`key[79]` first … `key[0]` last), the next 64 bits are the plaintext
   (`plaintext[63]` first … `plaintext[0]` last). These 144 bits fill
   `{key_reg[79:0], state[63:0]}`.
2. **Encrypt (~32 cycles).** Encryption **auto-starts** on the cycle the
   144th input bit is shifted in. Run the 31 rounds at one round per clock
   (a round counter sequences them), then perform the final addRoundKey.
3. **Unload.** When encryption completes, assert `done` and hold it high.
   While `done` is high and `shift_out_en` is high, shift the 64-bit
   ciphertext out MSB-first on `dout` (`ciphertext[63]` first), one bit
   per clock. `done` clears on reset or when a fresh load begins.

Total latency is deterministic (~144 + ~32 + 64 ≈ 240 cycles); exact cycle
counts are an implementation detail as long as `done` marks a valid,
fully-computed ciphertext before any bit is shifted out.

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
