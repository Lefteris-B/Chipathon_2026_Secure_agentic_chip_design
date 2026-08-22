# D10_D — Reproducing the submission

Provenance and exact steps to regenerate `gds/D10_D.gds` +
`verilog/gl/D10_D.pnl.v` (top cell `D10_D`, chipathon 2026 D10 variant D).

## Environment (pinned)

| | |
|---|---|
| EDA image | `hpretl/iic-osic-tools:chipathon26` |
| image digest | `sha256:b3a754709b6d71bf11ce9357c07d2d63b6a4562c2a36b20c1dde33dd0079a9b4` |
| PDK | `gf180mcuD` |
| std cell lib | `gf180mcu_fd_sc_mcu7t5v0` |
| flow driver | LibreLane (bundled in the image), via the NoeSI agentic wrapper |
| NoeSI repo | branch `feat/librelane-fp-def-template`, commit `071bb62` |
| sandbox | `docker run --network none`, 32 GB RAM cap |

Inputs (committed in NoeSI at that commit):
- RTL top: `scripts/repro/D10_D.v` — the 35-signal-port `D10_D` macro wrapping
  the verified PRESENT-80 core (pad-control tie-offs per `gf180mcu_fd_io.v`).
- Padframe contract: `D10/project_defs/D/D10_D.def` (organizer `FP_DEF_TEMPLATE`).

## Regenerate (run from the NoeSI repo)

```bash
uv run python scripts/repro/repro_present80_physical.py \
  --rtl scripts/repro/D10_D.v --top D10_D \
  --fp-def-template D10/project_defs/D/D10_D.def \
  --clock-period-ns 13.0 \
  --max-fanout 4 \
  --setup-slack-margin 4.0 \
  --extra-config MAX_CAPACITANCE_CONSTRAINT=0.4 \
  --extra-config CTS_SINK_CLUSTERING_SIZE=40 \
  --memory-gb 32 \
  --out-dir runs/D10_D_clockfix
```

Deliverables land in `runs/D10_D_clockfix/exports/` (`D10_D.gds`, `D10_D.pnl.v`,
`D10_D.def`, `D10_D.nl.v`); copy the GDS + `.pnl.v` here.

### Why these knobs (ss-corner closure, gf180 5V)
- `FP_DEF_TEMPLATE` → adopts the template die (550×550 µm) + FIXED boundary
  pins; the wrapper also derives `FP_SIZING=absolute`+`DIE_AREA` from it.
- `--max-fanout 4` → splits the high-fanout datapath broadcast nets (FSM
  `phase`, etc.) whose pin capacitance drove ss-corner `max_slew`; the single
  lever that cleared slew.
- `--setup-slack-margin 4.0` → keeps drivers strong so the extra buffer depth
  from fanout=4 still closes ss setup (relaxing the clock is counterproductive —
  the resizer downsizes and slew regresses).
- `MAX_CAPACITANCE_CONSTRAINT=0.4` → clears the clock-root max_cap (0.325 < 0.4).
- Note: `MAX_TRANSITION_CONSTRAINT` is NOT usable here — it OOM-crashes
  `RepairDesignPostGPL` on gf180 5V and does not help (slew is pin-cap, not
  transition, limited).

## Signoff (all 9 gf180 corners)
DRC · antenna · LVS · setup · hold · max_slew · max_cap = **0**.
Remaining `clkbuf_0_clk` max_fanout line is a checker artifact (data fanout
constraint applied to the healthy CTS clock tree; skew 0.035 ns) — non-gating.

35/37 template pins on the exact Metal2 coordinates. (VDD/VSS currently emerge
as a Metal5 PDN ring rather than the template's Metal2 west-edge pins — pending
organizer confirmation on whether M5 power tap is acceptable.)

## Independent cross-checks (on the committed files, fresh from the GDS)
- **LVS**: `magic` extract `gds/D10_D.gds` → SPICE, `netgen` vs
  `verilog/gl/D10_D.pnl.v` → *Circuits match uniquely*.
- **DRC**: `magic` DRC on `gds/D10_D.gds` (gf180mcuD) → 0 violations.
