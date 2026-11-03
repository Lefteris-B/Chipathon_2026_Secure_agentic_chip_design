# Reproducing `gds/D10_D.gds`

Everything needed to regenerate the submitted `D10_D` macro from source,
using plain LibreLane in the pinned IIC-OSIC-TOOLS image. No agent, no model,
and no network. The agentic flow that originally produced this RTL is
documented in the top-level [`Readme.md`](../Readme.md). This directory is the
deterministic physical spine on its own.

## Environment

| | |
|---|---|
| Image | `hpretl/iic-osic-tools:chipathon26` |
| Image ID | `sha256:b3a754709b6d71bf11ce9357c07d2d63b6a4562c2a36b20c1dde33dd0079a9b4` |
| LibreLane | 3.0.2 (bundled) |
| PDK / cells | `gf180mcuD` / `gf180mcu_fd_sc_mcu7t5v0` |
| Sandbox | `docker run --network=none --cpus=4 --memory=32g` |

## Inputs

| File | What | sha256 (prefix) |
|---|---|---|
| [`D10_D/D10_D.v`](D10_D/D10_D.v) | Top RTL: `D10_D` pad tie-off wrapper + `spi_slave_regs` core + `vss_conn`/`vdd_conn` blackbox stubs. Byte-identical to the RTL staged for the shipped run | `19691594ca14b5ca` |
| [`D10_D/D10_D.def`](D10_D/D10_D.def) | Organizer padframe DEF template (`FP_DEF_TEMPLATE`): 550×550 µm die, 37 FIXED pins | `a4cf00debfc63652` |
| [`D10_D/no_macro_pdn.tcl`](D10_D/no_macro_pdn.tcl) | Custom `PDN_CFG` (see [PDN](#pdn)) | `1abe1a840605bd46` |
| [`D10_D/config.json`](D10_D/config.json) | LibreLane config | — |
| [`../connectors/`](../connectors/) | `vss_conn`/`vdd_conn` power-bridge cells (GDS + LEF) and their build scripts | — |

## Run

```bash
repro/D10_D/run.sh            # ~2-3 min on 4 cores
```

Outputs land in `repro/D10_D/runs/repro/final/` (`gds/`, `pnl/`, `def/`,
`metrics.json`). The script prints the signoff metrics at the end.

### Result of re-running from this directory (2026-11-03)

| Metric | Shipped `gds/D10_D.gds` | Re-run |
|---|---|---|
| Power-grid unconnected shapes (VDD / VSS) | 0 / 0 | 0 / 0 |
| IR drop worst / avg (nom tt, 5 V) | not run | 0.40 mV / 0.07 mV |
| Magic DRC | 0 | 0 |
| LVS errors | 0 | 0 |
| Antenna violating nets | 0 | 0 |
| Setup / hold / slew / cap violations | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| Setup WS / hold WS (worst corner) | +2.80 / +0.19 ns | +2.76 / +0.19 ns |
| Instances | 7347 | 7337 |

**What matches exactly:** the DEF `PINS` and `SPECIALNETS` sections (the
entire power grid, connector vias included) are byte-identical to the shipped
`runs/D10_D_regs/exports/D10_D.def`. The connector cells inside the shipped GDS
XOR to zero against `connectors/gds/`.

**What does not:** about 45 standard cells differ in drive strength or
orientation (e.g. `nand4_2` vs `nand4_4`), with the fill and routing changing
around them. That accounts for the 0.04 ns setup-slack difference. The flow is
deterministic from these inputs (two re-runs gave byte-identical DEFs), so the
shipped run differed from this config in some setting not captured in its
resolved config. The difference is in timing-repair sizing only. Power grid,
pins, and every signoff check agree.

KLayout DRC is skipped by LibreLane for gf180mcuD (`KLAYOUT_DRC_RUNSET`
unset), so layout DRC coverage is Magic only, in both runs.

## PDN

### Problem

The padframe delivers VDD/VSS to the macro as **Metal2** pin columns on the
west edge (template pins `VSS` at slot W18, `VDD` at W19). The core power grid
is **Metal4 vertical / Metal5 horizontal** straps over Metal1 rails. Nothing
in a default LibreLane flow connects the Metal2 pins to that grid. An earlier
submission had the pins and the core grid as separate islands (PSM-0069). LVS
still passed, because the DEF pin label merged both islands, so LVS is
**not** a check of PDN continuity. `check_power_grid` is.

(Side note on the organizer-reported "≈1 M power grid violations" for that
earlier GDS: LibreLane 3.0.2's `get_psm_error_count()` counts the characters
of the violation string instead of entries. The real count was 13,165.)

### Fix

1. **Bridge cells.** `vss_conn` and `vdd_conn` are 30 × 72.28 µm cells that
   repeat the template's 6-rectangle Metal2 pin column, extended 30 µm
   east, with a Via2 array up to an identical Metal3 plate (4,136 vias per cell).
   They are placed at (0, 46.36) and (0, 146.36) µm so the Metal2 abuts the
   pad pins. The 30 µm width is chosen so that both straps of the first core
   Metal4 pair (VDD at x = 23.04 µm, VSS at x = 26.34 µm) cross the plate.
2. **`no_macro_pdn.tcl`** (LibreLane 3.0.2's `pdn_cfg.tcl` with these changes; `diff` it against the image's copy to see exactly what changed):
   - `add_global_connection` ties `vss_conn_0/VSS` and `vdd_conn_0/VDD` to the
     special power nets. The RTL instances are deliberately left unconnected:
     tying them in RTL creates a separate regular net that never merges
     (LVS `VSS_uq0` island).
   - Each bridge gets a macro grid that **emits no stripes** and declares
     `-obstructions "Metal2 Metal3"` only. So the core Metal4 straps are
     *not* cut around the bridge and run straight over it.
   - `add_pdn_connect -layers "Metal3 Metal4"` drops the Via3 stack where a
     same-net strap crosses the plate.
   - The default catch-all macro grid (`-default`, Metal4↔Metal5 connect) is
     removed. Metal3 is never used as a PDN layer, because a crossing
     opposite-net Metal3 stripe would short the bridge plates.
3. **Flow knobs** (`config.json`): `MACROS` places the bridges,
   `SYNTH_KEEP_HIERARCHY_MODULES` keeps the empty blackbox instances through
   synthesis, and `LVS_CONNECT_BY_LABEL` lets extraction merge the bridge into
   the labeled power net. `RUN_IRDROP_REPORT=1` and `ERROR_ON_PDN_VIOLATIONS=1`
   make a disconnected grid fail the run instead of passing quietly.

Approaches that did **not** work, and why: re-basing a macro-grid stripe onto
the core offset leaves a 0.28 µm gap at each end, because pdngen cuts the core
strap around the macro and trims the macro stripe to the macro bbox. A 20 µm
bridge does not reach the first strap at 23.04 µm, because the core grid offset
is measured from the core-area origin (6.72 µm), not the die origin.

### Verify the PDN

After `run.sh`:

```bash
R=repro/D10_D/runs/repro
grep PSM-0040 $R/*-openroad-generatepdn/openroad-generatepdn.log
#   [INFO PSM-0040] All shapes on net VDD are connected.
#   [INFO PSM-0040] All shapes on net VSS are connected.
cat $R/*-openroad-generatepdn/V*-grid-errors.rpt     # both empty
cat $R/*-openroad-irdropreport/irdrop.rpt            # IR-drop summary (net-*.csv: per-node)
```

### Rebuild the bridge cells

```bash
cd connectors/build
docker run --rm --network=none -v "$PWD":/w -w /w hpretl/iic-osic-tools:chipathon26 --skip \
  bash -lc 'klayout -b -r build_conn.py &&
            magic -dnull -noconsole -rcfile $PDK_ROOT/$PDK/libs.tech/magic/$PDK.magicrc add_bbox.tcl &&
            magic -dnull -noconsole -rcfile $PDK_ROOT/$PDK/libs.tech/magic/$PDK.magicrc drc_conn.tcl'
# -> vss_conn.gds / vdd_conn.gds, "DRC_RESULT ...: 0" for both
```

| Script | Does |
|---|---|
| `build_conn.py` | KLayout: draws the Metal2 + Via2 + Metal3 bridge geometry |
| `add_bbox.tcl` | Magic: adds the `FIXED_BBOX` property LibreLane's macro bbox step needs |
| `drc_conn.tcl` | Magic: standalone gf180mcuD DRC on each cell |

Rebuilt cells are geometrically identical to `connectors/gds/` (KLayout XOR,
all layers: 0 differences). The bytes differ only in GDS timestamps.
