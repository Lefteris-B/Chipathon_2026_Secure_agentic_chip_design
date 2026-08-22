> **UPDATE (embedded):** The power connectors are now **embedded inside**
> `gds/D10_D.gds` — placed and welded to the PDN in the macro flow (net-aware
> macro grid via + LVS connect-by-label). The macro is self-contained and
> LVS-clean; these standalone cells are kept for reference / re-integration.

# D10_D power connectors (VSS/VDD Metal2 bridge)

Standalone power-connector cells for integration-time placement, following the
pattern used by ChipFoundry/efabless caravel (`vccd1_connection` /
`vssd1_connection` are shipped the same way).

## Why

The padframe I/O cells (`gf180mcu_fd_io__dvss` at slot W18, `dvdd` at W19)
present VSS/VDD on **Metal2** at the west edge of the `D10_D` macro (the `VSS`
/`VDD` pins in `D10_D.def`, 6-rect columns at x≈0). The hardened `D10_D` macro
distributes power on a **Metal5** grid. These connectors bridge the two: a
Metal2 landing (matching the template pin columns) stacked up to Metal5 via
Via2/Via3/Via4 arrays.

## Cells

| Cell | Net | Size | Place at (macro frame, µm) |
|---|---|---|---|
| `vss_conn` | VSS | 10 × 72.28 µm | (0.0, 46.36) N |
| `vdd_conn` | VDD | 10 × 72.28 µm | (0.0, 146.36) N |

Views: `gds/`, `lef/` (CLASS BLOCK, power PIN on Metal2+Metal5, OBS on
Metal3/Metal4), `verilog/` (empty `(* blackbox *)` module with the power port).

Via stack (silicon-proven layers/pitches, extracted from
`caravel-gf180mcu`'s `caravel_power_routing`):
Metal2 → Via2 (0.26 µm, 0.62 pitch) → Metal3 → Via3 (0.71 pitch) → Metal4 →
Via4 (0.71 pitch) → Metal5. Standalone **magic DRC: 0 violations**.

## Placement (LibreLane)

Instantiate each once and place at the coordinates above so the Metal2 landing
abuts the padframe VSS/VDD Metal2 pins and the Metal5 top overlaps the power
grid/ring:

```json
"MACROS": {
  "vss_conn": { "gds": ["connectors/gds/vss_conn.gds"],
                "lef": ["connectors/lef/vss_conn.lef"],
                "instances": { "vss_conn_0": { "location": [0.0, 46.36], "orientation": "N" } } },
  "vdd_conn": { "gds": ["connectors/gds/vdd_conn.gds"],
                "lef": ["connectors/lef/vdd_conn.lef"],
                "instances": { "vdd_conn_0": { "location": [0.0, 146.36], "orientation": "N" } } }
}
```

## Status / caveat

The connector cells themselves are DRC-clean and dimensioned to the template
pins. Placing them **inside** the `D10_D` macro flow hit LibreLane's macro-PDN
model (it builds an empty per-macro grid for a passive bridge, `PDN-0233`), so
they are shipped standalone for placement at chip integration where the
padframe Metal2 power and the macro Metal5 grid actually meet. **Power
connectivity must be LVS-verified at the integration level** once placed.

Build script + full flow knobs: NoeSI repo
`scripts/repro/connectors/build_conn.py`.
