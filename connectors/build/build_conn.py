"""Build gf180 power-connector cells (vdd_conn / vss_conn) for D10_D.

Stack (silicon-proven layers/pitches from caravel_power_routing):
  Metal2(36) -Via2(38)- Metal3(42) -Via3(40)- Metal4(46) -Via4(41)- Metal5(81)
Via size 0.26 um; pitch Via2=0.62, Via3=0.71, Via4=0.71 um.

Each cell's Metal2 replicates the template's 6-rect column (relative to the
cell origin) but EXTENDED east to WIDTH um so a via array fits; the west edge
(x=0) stays for pad abutment. Placed later at (0, y_min): VSS (0,46.36),
VDD (0,146.36).
"""
import pya

DBU = 0.001
WIDTH = 30.0          # eastward metal extent (um): must reach PAST the first
                      # core-grid M4 strap PAIR (VDD x=23.04, VSS x=26.34um) so the
                      # macro PDN grid strap is collinear with the core strap
VIA = 0.26            # via square size (um)
INSET = 0.42          # via array inset from metal edge (encl + margin)
PITCH = {"via2": 0.62, "via3": 0.71, "via4": 0.71}
LYR = {"Metal2": (36, 0), "Via2": (38, 0), "Metal3": (42, 0),
       "Via3": (40, 0), "Metal4": (46, 0), "Via4": (41, 0), "Metal5": (81, 0)}

# template Metal2 rects (um), x[0,1], per net -> we take (y1,y2) bands
BANDS = {
    "vss_conn": [(109.14, 118.64), (95.99, 106.24), (84.14, 94.39),
                 (70.61, 80.86), (58.76, 69.01), (46.36, 55.86)],
    "vdd_conn": [(209.14, 218.64), (195.99, 206.24), (184.14, 194.39),
                 (170.61, 180.86), (158.76, 169.01), (146.36, 155.86)],
}


def via_array(cell, layer, x1, y1, x2, y2, pitch):
    """Fill [x1,x2]x[y1,y2] with VIA-size squares at `pitch`, inset from edges."""
    ax1, ay1 = x1 + INSET, y1 + INSET
    ax2, ay2 = x2 - INSET, y2 - INSET
    n = 0
    y = ay1
    while y + VIA <= ay2 + 1e-6:
        x = ax1
        while x + VIA <= ax2 + 1e-6:
            cell.shapes(layer).insert(
                pya.DBox(x, y, x + VIA, y + VIA))
            x += pitch; n += 1
        y += pitch
    return n


def build(ly, name):
    cell = ly.create_cell(name)
    L = {k: ly.layer(*v) for k, v in LYR.items()}
    ymin = min(b[0] for b in BANDS[name])
    total_vias = 0
    for (y1, y2) in BANDS[name]:
        ry1, ry2 = y1 - ymin, y2 - ymin          # relative to cell origin
        box = pya.DBox(0.0, ry1, WIDTH, ry2)
        # Stack ONLY up to Metal3 (no core power straps live on M2/M3, so the
        # bridge can't short the paired VDD/VSS grid). The net-aware macro PDN
        # grid vias this Metal3 pin up to the Metal4/5 straps that cross it.
        for m in ("Metal2", "Metal3"):
            cell.shapes(L[m]).insert(box)
        total_vias += via_array(cell, L["Via2"], 0, ry1, WIDTH, ry2, PITCH["via2"])
    return cell, total_vias


for name in ("vss_conn", "vdd_conn"):
    ly = pya.Layout(); ly.dbu = DBU
    cell, nv = build(ly, name)
    bbox = cell.dbbox()
    ly.write(f"{name}.gds")
    print(f"{name}: {bbox.width():.2f} x {bbox.height():.2f} um, {nv} vias -> {name}.gds")
