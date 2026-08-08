# PRESENT-80 — verified RTL → GDSII (gf180mcuD)

Bit-serial PRESENT-80 block cipher (encryption-only, iterative), driven from
`specs/present80.md` through RTL → synthesis → place & route → signoff → GDSII
on the pinned IIC-OSIC-TOOLS image (`hpretl/iic-osic-tools:chipathon26`,
LibreLane v3.0.2, gf180mcuD PDK).

## Interface — 7 physical pins (pin-constrained)

`clk`, `rst_n` (async, active-low), `load_en`, `din`, `shift_out_en`, `dout`, `done`.

- **Load (144 cyc):** shift `{key[79:0], plaintext[63:0]}` in MSB-first on `din`.
- **Encrypt (~33 cyc):** 31 rounds (addRoundKey → sBoxLayer → pLayer) + final
  addRoundKey, one round/clock; auto-starts on the 144th load bit.
- **Unload (64 cyc):** ciphertext out MSB-first on `dout` while `done & shift_out_en`.

## Functional verification — PASS (4/4 standard vectors)

Full 240-cycle simulation (`tb_present80.v`, iverilog) against the ISO/IEC
29192-2 test vectors:

| key                  | plaintext          | ciphertext (got == expected) |
|----------------------|--------------------|------------------------------|
| `00…00`              | `00…00`            | `5579C1387B228445` ✅ |
| `FF…FF`              | `00…00`            | `E72C46C0F5945049` ✅ |
| `00…00`              | `FF…FF`            | `A112FFC72F68417B` ✅ |
| `FF…FF`              | `FF…FF`            | `3333DCD3213210D2` ✅ |

Reproduce:
```
docker run --rm --network none -v "$PWD:/work" -w /work \
  hpretl/iic-osic-tools:chipathon26 --skip bash -lc \
  'iverilog -g2012 -o sim present80.v tb_present80.v && vvp sim'
```

## Physical signoff (gdsii/metrics.json)

| Check            | Result |
|------------------|--------|
| Routing DRC      | **0 errors** ✅ |
| LVS              | **0 errors** ✅ |
| Cells / die      | 5098 std cells, 326.15 × 344.07 µm (~0.11 mm²), 30% util |
| Est. power       | ~0.135 W |
| Setup @ 10 ns    | typ **+3.6 ns**, fast **+8.0 ns** — slow corner **−1.82 ns** ⚠️ |

**Timing caveat (accepted):** DRC/LVS-clean and functionally correct, but the
10 ns target does **not** close at the worst slow corner (ss, 125°C, 4.5V) —
that critical path is ~11.8 ns. Closes at typical/fast. Worst-case Fmax ≈ 85 MHz.
Relaxing `CLOCK_PERIOD` to ~12 ns in `gdsii/librelane_config.json` and re-running
would close all corners (throughput is unaffected — the interface is bit-serial).

## RTL note — synthesizable style

Written function-free on purpose: the S-box is a packed constant LUT
(`localparam [63:0] SBOX = 64'h21748FE3DA09B65C`, indexed `SBOX[4*x +: 4]`) and
sBoxLayer/pLayer are `generate` loops of single-driver `assign`s. An earlier
function-based version simulated identically but aborted LibreLane synthesis at
`Checker.YosysSynthChecks` — LibreLane runs Yosys `check` *before* `opt`, so
Verilog-function-local temporaries get flagged "used but has no driver". Verify
synthesizability with `yosys -p "read_verilog present80.v; hierarchy -top
present80; proc; check"` (no `opt`) → expect 0 "no driver".

## Files

- `present80.v` — verified, synthesizable RTL (single flat module)
- `tb_present80.v` — self-checking 4-vector testbench
- `gdsii/present80.gds` — Magic stream-out (1.98 MB); `present80.klayout.gds` — KLayout (1.37 MB)
- `gdsii/present80.netlist.v` — final routed netlist
- `gdsii/metrics.json` — full LibreLane signoff metrics
- `gdsii/sta_nom_tt.max.rpt` — typical-corner setup STA report
- `gdsii/librelane_config.json` — flow config (gf180mcuD, 10 ns, 30% util)
