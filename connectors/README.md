# D10_D power connectors (VSS/VDD Metal2 → core-grid bridge)

The two passive cells that connect the padframe's Metal2 VSS/VDD pins to the
`D10_D` core power grid. They are **embedded in `gds/D10_D.gds`** (instances
`vss_conn_0`, `vdd_conn_0`). These are the exact views used by the flow, kept
here so the macro can be reproduced ([`../repro/README.md`](../repro/README.md#pdn)).

## Cells

| Cell | Net | Size | Placed at (macro frame, µm) |
|---|---|---|---|
| `vss_conn` | VSS | 30 × 72.28 µm | (0.0, 46.36) N |
| `vdd_conn` | VDD | 30 × 72.28 µm | (0.0, 146.36) N |

Geometry: the template's 6-rectangle Metal2 pin column, extended 30 µm east,
then a Via2 array (0.26 µm, 0.62 µm pitch, 4,136 vias per cell) up to an
identical Metal3 plate. The cells stop at Metal3. Inside the flow, the core
Metal4 straps at x = 23.04 µm (VDD) and 26.34 µm (VSS) run over the plate, and
`no_macro_pdn.tcl` drops the Metal3→Metal4 vias. Standalone Magic DRC: 0.

## Files

| Path | |
|---|---|
| `gds/` | Layout (with the `FIXED_BBOX` property LibreLane needs) |
| `lef/` | Abstract: `CLASS BLOCK`, power pin on Metal2 + Metal3 |
| `verilog/` | `(* blackbox *)` module stubs (also inlined in `repro/D10_D/D10_D.v`) |
| `build/` | `build_conn.py` (KLayout geometry), `add_bbox.tcl` (Magic bbox property), `drc_conn.tcl` (Magic DRC). Rebuild steps in [`../repro/README.md`](../repro/README.md#rebuild-the-bridge-cells) |
