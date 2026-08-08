# PRESENT-80 lightweight block cipher (encryption, iterative)

A gf180mcu proof-of-concept: the PRESENT-80 block cipher in its
canonical encryption-only form, implemented as an **iterative** datapath
that reuses one round of logic across 31 rounds (one round per clock).

PRESENT is a 64-bit block cipher with an 80-bit key and 31 rounds,
standardised in ISO/IEC 29192-2. This design targets the smallest,
simplest **correct** implementation suitable for a first end-to-end flow.
Correctness against the published test vectors below is the acceptance
criterion — a design that never asserts `done`, or that returns the wrong
ciphertext, is a failure even if it elaborates cleanly.

## Module

* Name: `present80`
* Top-level module ID: `present80`

Implement the entire cipher as a **single flat Verilog module** named
`present80`. Do **not** decompose it into sub-modules (no separate S-box,
pLayer, key-schedule, or FSM modules) — inline every layer as
combinational logic and `always` blocks within this one module. All
internal sequencing (round counter, state-machine phase, load/unload bit
counters) is internal and must not be exposed as a port.

**Coding-style requirement (for clean synthesis):** the RTL must be
**synthesizable by Yosys**, not merely simulatable — Verilator accepts some
constructs Yosys rejects, so write portable, standard Verilog-2005/2012:

* Do **not** use Verilog `function` constructs for the S-box, pLayer, or key
  schedule. Express the S-box as an indexed constant (e.g. a packed
  `localparam [63:0]` sliced with `SBOX[4*x +: 4]`) and express sBoxLayer /
  pLayer with `generate` loops of continuous `assign`s, so every net has
  exactly one driver. (Function-local temporaries trip the synthesis linter's
  "used but has no driver" check.)
* Do **not** apply a part-select or bit-index to a concatenation or other
  expression — e.g. `{key_reg, state}[142:0]` is **illegal** in synthesizable
  Verilog. To shift the 144-bit load chain left by one, slice the named
  registers directly: `{key_reg, state} <= {key_reg[78:0], state[63:0], din};`.
  Only index a declared `reg`/`wire`, never `{...}[...]`.

## Interface (pin-constrained, bit-serial)

This design is **pin-constrained**: the full 80-bit key, 64-bit plaintext,
and 64-bit ciphertext are streamed **one bit per clock** over single-bit
ports rather than presented on parallel buses. This keeps the design to
**7 physical I/O pins** (a parallel interface would need ~212). The internal
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
* `dout` — output, 1 bit, serial data out. Drives `state[63]` (the current
  MSB of the ciphertext) while unloading; `0` otherwise.
* `done` — output, 1 bit. Asserted once the ciphertext is fully computed
  and held high while the result is available to shift out; clears on
  reset or when a new load begins.

## Reset

Asynchronous active-low reset. When `rst_n` is low, the core returns to
idle: `done` clears to 0 and any in-progress encryption is abandoned.
Reset is released synchronously to `clk`.

## Algorithm — reference pseudocode

Encryption maintains a 64-bit `state` and an 80-bit `key_reg`. `state` is
loaded from `plaintext`, `key_reg` from `key`. Then:

```
# 31 full rounds, then a final key mix
for i in 1 .. 31:
    state = state XOR key_reg[79:16]      # addRoundKey (top 64 bits of key_reg)
    state = sBoxLayer(state)              # 16 parallel 4-bit S-box lookups
    state = pLayer(state)                 # fixed 64-bit bit permutation
    key_reg = keyScheduleUpdate(key_reg, i)   # advance key AFTER using it
state = state XOR key_reg[79:16]          # FINAL addRoundKey with K_32
ciphertext = state
```

Two mistakes make the ciphertext wrong — avoid both:
* **The final addRoundKey is mandatory.** After the 31st round you XOR once
  more with `key_reg[79:16]` *as updated by the 31st `keyScheduleUpdate`*.
* **The key is updated after each round is used**, so round `i` uses the
  key register's current value and `keyScheduleUpdate(·, i)` produces the
  key for round `i+1` (and, after `i=31`, the final K_32).

### sBoxLayer

Apply the 4-bit S-box independently to each of the 16 nibbles of `state`:
`out[4*n +: 4] = S[ state[4*n +: 4] ]` for `n` in 0..15.

S-box (input nibble `x` → `S[x]`, hex):

| x    | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | A | B | C | D | E | F |
|------|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S[x] | C | 5 | 6 | B | 9 | 0 | A | D | 3 | E | F | 8 | 4 | 7 | 1 | 2 |

Packed for a constant lookup: `SBOX = 64'h21748FE3DA09B65C` (so
`SBOX[4*x +: 4] == S[x]`).

### pLayer

Bit `i` of the input moves to position `P(i)`:
`P(i) = (i * 16) mod 63` for `i` in 0..62, and `P(63) = 63`.
Equivalently `out[P(i)] = in[i]`.

### keyScheduleUpdate(key_reg, i)

1. Rotate the 80-bit register left by 61 bits:
   `key_reg = (key_reg << 61) | (key_reg >> 19)`  (mask to 80 bits).
2. Pass the new top nibble through the S-box:
   `key_reg[79:76] = S[key_reg[79:76]]`.
3. XOR the 5-bit round counter `i` (1..31) into bits `[19:15]`:
   `key_reg[19:15] = key_reg[19:15] XOR i[4:0]`.

## Behaviour — concrete cycle schedule (FSM)

Use a small phase FSM plus three counters (`load_cnt`, `round`, `unload_cnt`).
Exact counter widths are up to the implementer, but the sequence below is
**normative** — in particular `done` MUST assert after a bounded, finite
number of cycles for every input.

1. **LOAD (exactly 144 accepted bits).** While `load_en` is high, on each
   rising edge shift `din` MSB-first into the 144-bit chain
   `{key_reg[79:0], state[63:0]}` (new bit enters at `state[0]`; the chain
   shifts left by one):
   `{key_reg, state} <= {key_reg[78:0], state[63:0], din}`.
   The first 80 shifted-in bits are the key (`key[79]` first … `key[0]`
   last); the next 64 are the plaintext (`plaintext[63]` first …
   `plaintext[0]` last). Count accepted bits in `load_cnt`. `done` is 0
   throughout LOAD.
2. **ENCRYPT (32 cycles).** Encryption **auto-starts** on the edge that
   accepts the 144th bit (set `round <= 1` and enter ENCRYPT). On each
   ENCRYPT edge, for `round = 1 .. 31`: compute the round transform
   (addRoundKey → sBoxLayer → pLayer) into `state` **and** advance
   `key_reg = keyScheduleUpdate(key_reg, round)` on the same edge, then
   increment `round`. After the round-31 edge, take one more edge to apply
   the **final addRoundKey** (`state <= state XOR key_reg[79:16]`), assert
   `done <= 1`, and enter DONE.
3. **DONE / UNLOAD.** Hold `done` high. `dout = shift_out_en ? state[63] : 0`.
   While `shift_out_en` is high, on each rising edge shift `state` left by
   one (`state <= {state[62:0], 1'b0}`) so the 64 ciphertext bits leave
   MSB-first (`ciphertext[63]` first). `done` clears on reset, or when a
   fresh load begins (`load_en` high in DONE restarts LOAD with `done<=0`).

Total latency is deterministic: 144 (load) + ~32 (encrypt) + 64 (unload)
≈ 240 cycles. The critical acceptance property is that `done` reaches 1 and
`dout` then emits the correct ciphertext MSB-first — a testbench that waits
for `done` (up to a few hundred cycles) and compares the shifted-out 64
bits to the vectors below must pass.

## Test vectors (acceptance criteria)

Standard PRESENT-80 vectors (all values hex, MSB-first). The implementation
must reproduce all four:

| key                        | plaintext          | ciphertext         |
|----------------------------|--------------------|--------------------|
| `00000000000000000000`     | `0000000000000000` | `5579C1387B228445` |
| `FFFFFFFFFFFFFFFFFFFF`      | `0000000000000000` | `E72C46C0F5945049` |
| `00000000000000000000`     | `FFFFFFFFFFFFFFFF`  | `A112FFC72F68417B` |
| `FFFFFFFFFFFFFFFFFFFF`      | `FFFFFFFFFFFFFFFF`  | `3333DCD3213210D2` |

## Correctness checklist (common failure modes)

* `done` must actually assert — an FSM that never leaves LOAD/ENCRYPT (wrong
  load-count gating or a missing auto-start) is the most common failure.
* The **final addRoundKey** after round 31 must be present.
* Load and unload are both **MSB-first**; the key loads before the plaintext.
* `keyScheduleUpdate` XORs the round number `i` (not `i-1`) into `[19:15]`,
  rotates left by 61, and S-boxes the *post-rotate* top nibble.
* Loading is gated by `load_en`; do not shift on cycles where `load_en` is 0.

## Constraints

* Target clock period: 10 ns.
* Target utilization: 20%.
* PDK: gf180mcuD.
