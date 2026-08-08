"""PDK-derived tool paths — the single source of truth for where the
signoff/GDSII tool services find the technology files inside the sandbox.

Magic (DRC + GDS emit), OpenSTA and Netgen each need a PDK-specific input:
Magic's ``-T`` tech name, OpenSTA's liberty file, and Netgen's setup ``.tcl``.
Historically these were hardcoded to sky130 in each service. Centralizing the
path *shape* here means switching PDKs is a config change (``DesignConstraints.pdk``
/ ``std_cell_lib``) rather than an edit scattered across four tool modules.

The open_pdks install layout under ``/foss/pdks/<pdk>`` is uniform across
sky130 and gf180mcu, so the same templates serve both. The only family-specific
knob is the timing corner's voltage label (sky130 is 1.8 V, gf180mcu is 5 V),
resolved by :func:`_corner_label`.
"""

from __future__ import annotations

__all__ = [
    "PDK_ROOT",
    "liberty_path",
    "magic_tech",
    "netgen_setup",
]

#: Root under which the IIC-OSIC-TOOLS image installs open_pdks PDKs.
PDK_ROOT = "/foss/pdks"

#: Timing-corner voltage label by PDK family prefix. sky130 is a 1.8 V process,
#: gf180mcu a 5 V process, so the liberty filenames end ``__tt_025C_1v80`` vs
#: ``__tt_025C_5v00``. New PDKs slot in by adding their prefix here.
_CORNER_BY_FAMILY: dict[str, str] = {
    "sky130": "1v80",
    "gf180": "5v00",
}


def _corner_label(pdk: str) -> str:
    """Return the typical-corner voltage label (e.g. ``5v00``) for ``pdk``.

    Matches on family prefix so variant suffixes (``gf180mcuD``, ``sky130A``)
    all resolve. Raises for an unknown family rather than guessing a corner.
    """
    for prefix, label in _CORNER_BY_FAMILY.items():
        if pdk.startswith(prefix):
            return label
    raise ValueError(
        f"unknown PDK family for {pdk!r}; add its corner label to "
        "chip_agent.tools.pdk_paths._CORNER_BY_FAMILY",
    )


def magic_tech(pdk: str) -> str:
    """Return the Magic ``-T`` tech name for ``pdk``.

    open_pdks names the Magic techfile after the PDK variant
    (``sky130A``, ``gf180mcuD``), so the tech name is the PDK name itself.
    """
    return pdk


def liberty_path(pdk: str, std_cell_lib: str) -> str:
    """Return the typical-corner liberty file path for OpenSTA.

    Points at ``<PDK_ROOT>/<pdk>/libs.ref/<std_cell_lib>/lib/
    <std_cell_lib>__tt_025C_<corner>.lib`` — the typical process, 25 °C,
    nominal-voltage corner used for single-corner signoff STA.
    """
    corner = _corner_label(pdk)
    return (
        f"{PDK_ROOT}/{pdk}/libs.ref/{std_cell_lib}/lib/"
        f"{std_cell_lib}__tt_025C_{corner}.lib"
    )


def netgen_setup(pdk: str) -> str:
    """Return the Netgen LVS setup ``.tcl`` path for ``pdk``.

    open_pdks names the setup file after the PDK variant, at
    ``<PDK_ROOT>/<pdk>/libs.tech/netgen/<pdk>_setup.tcl``.
    """
    return f"{PDK_ROOT}/{pdk}/libs.tech/netgen/{pdk}_setup.tcl"
