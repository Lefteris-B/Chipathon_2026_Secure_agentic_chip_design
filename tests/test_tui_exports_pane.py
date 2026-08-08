"""F14.5 acceptance: exports tree + JSON/Verilog/GDS preview pane.

Pins:

* Mounting the pane against a populated exports tree shows the files
  in the :class:`DirectoryTree`.
* The pane survives an empty-or-missing exports root (no run yet) —
  mkdir is eager + idempotent.
* Selecting a ``.json`` file loads its body into a syntax-highlighted
  preview (a :class:`rich.syntax.Syntax`).
* Selecting a ``.v`` file gets the verilog lexer.
* Selecting a ``.gds`` file shows the size, the HEADER-bytes magic
  badge, and a hex dump — same shape F11.7 used to assert a real
  layout came out of LibreLane.
* Selecting a non-existent file falls back to a "(file not found)"
  string instead of crashing.

Tests mount :class:`ExportsPane` in a minimal host :class:`App` via
Textual's :meth:`App.run_test` harness and drive
``pane.apply_file_preview(path)`` directly — that's the same public
seam the tree's ``FileSelected`` message uses internally, so we
exercise the rendering logic without simulating mouse clicks. The
file tree is populated by writing bytes into ``tmp_path`` — no real
``cmd_run`` needed (the F11.6 export shape is documented + simple).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rich.syntax import Syntax
from textual.app import App, ComposeResult
from textual.widgets import DirectoryTree, Static

from chip_agent.tui.panes.exports import (
    ExportsPane,
    _build_preview,
    _render_gds_preview,
)

DESIGN_ID = "exports-pane-test"


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Fixture helper — populate an exports/<id>/ tree the way F11.6 does.
# --------------------------------------------------------------------------- #
def _populate_exports(root: Path) -> dict[str, Path]:
    """Drop the F11.6 file shape into ``root`` and return path handles."""
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    spec_md = root / "spec.md"
    spec_md.write_text("# Counter spec\n\n8-bit wrap counter.\n")
    paths["spec.md"] = spec_md

    rtl_dir = root / "rtl"
    rtl_dir.mkdir(exist_ok=True)
    rtl_v = rtl_dir / "counter.v"
    rtl_v.write_text(
        "module counter (input clk, input rst_n, output [7:0] q);\n"
        "endmodule\n",
    )
    paths["rtl/counter.v"] = rtl_v

    rtl_json = rtl_dir / "counter.rtl.json"
    rtl_json.write_text('{"artifact_id": "counter.rtl", "version": 1}\n')
    paths["rtl/counter.rtl.json"] = rtl_json

    physical_dir = root / "physical"
    physical_dir.mkdir(exist_ok=True)
    def_file = physical_dir / "counter.def"
    def_file.write_text("VERSION 5.8 ;\nDESIGN counter ;\nEND DESIGN\n")
    paths["physical/counter.def"] = def_file

    # A real (mini) GDSII body — HEADER record + a few bytes of payload.
    gds_dir = root / "gds"
    gds_dir.mkdir(exist_ok=True)
    gds = gds_dir / "counter.gds"
    gds.write_bytes(b"\x00\x06\x00\x02" + b"\xab" * 32)  # 36 bytes
    paths["gds/counter.gds"] = gds

    return paths


# --------------------------------------------------------------------------- #
# Host harness for the pane.
# --------------------------------------------------------------------------- #
def _drive(
    drive: Callable[[ExportsPane, object], Awaitable[dict[str, Any]]],
    *,
    exports_root: Path,
    refresh_ms: int = 1_000_000,
) -> dict[str, Any]:
    """Mount an ExportsPane in a minimal host App and run ``drive``.

    ``refresh_ms`` defaults to "essentially never" so tests trigger
    reloads explicitly — keeps assertions deterministic instead of
    racing the interval timer.
    """

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield ExportsPane(
                exports_root=exports_root, refresh_ms=refresh_ms,
            )

    async def _go() -> dict[str, Any]:
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            pane = host.query_one(ExportsPane)
            return await drive(pane, pilot)

    return _arun(_go())


def _preview_renderable(pane: ExportsPane) -> Any:
    return pane.query_one("#exports-preview", Static).renderable


# --------------------------------------------------------------------------- #
# Pure-function tests: _build_preview + _render_gds_preview.
# --------------------------------------------------------------------------- #
def test_build_preview_json_uses_json_lexer(tmp_path: Path) -> None:
    body = '{"k": 1}\n'
    p = tmp_path / "x.json"
    p.write_text(body)
    rend = _build_preview(p)
    assert isinstance(rend, Syntax)
    assert rend.code == body
    assert rend._lexer == "json"


def test_build_preview_verilog_uses_verilog_lexer(tmp_path: Path) -> None:
    body = "module m; endmodule\n"
    p = tmp_path / "x.v"
    p.write_text(body)
    rend = _build_preview(p)
    assert isinstance(rend, Syntax)
    assert rend.code == body
    assert rend._lexer == "verilog"

    p_sv = tmp_path / "y.sv"
    p_sv.write_text(body)
    assert _build_preview(p_sv)._lexer == "verilog"  # type: ignore[union-attr]


def test_build_preview_def_uses_plain_text_lexer(tmp_path: Path) -> None:
    body = "VERSION 5.8 ;\n"
    p = tmp_path / "x.def"
    p.write_text(body)
    rend = _build_preview(p)
    assert isinstance(rend, Syntax)
    assert rend._lexer == "text"


def test_build_preview_unknown_extension_returns_plain_string(
    tmp_path: Path,
) -> None:
    """No lexer mapping → preview gets the raw body string (Static accepts str)."""
    body = "no lexer for me\n"
    p = tmp_path / "x.foo"
    p.write_text(body)
    assert _build_preview(p) == body


def test_build_preview_missing_file_returns_fallback_string(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nope.json"
    rend = _build_preview(missing)
    assert isinstance(rend, str)
    assert "nope.json" in rend
    assert "not found" in rend


def test_render_gds_preview_shows_size_header_badge_and_hex(
    tmp_path: Path,
) -> None:
    """Valid GDSII magic → "HEADER record OK" + size + hex dump."""
    p = tmp_path / "tiny.gds"
    body = b"\x00\x06\x00\x02" + b"\xff\xee" + b"\x00" * 30  # 36 bytes
    p.write_bytes(body)
    preview = _render_gds_preview(p)
    assert "GDSII: 36 bytes" in preview
    assert "HEADER record OK" in preview
    # Hex dump shows the magic bytes in the first hex group.
    assert "00 06 00 02" in preview
    assert "ff ee" in preview


def test_render_gds_preview_flags_missing_header_for_garbage_bytes(
    tmp_path: Path,
) -> None:
    p = tmp_path / "bogus.gds"
    p.write_bytes(b"junk" + b"\x00" * 60)
    preview = _render_gds_preview(p)
    assert "MISSING HEADER record" in preview


# --------------------------------------------------------------------------- #
# Mounted-pane tests.
# --------------------------------------------------------------------------- #
def test_exports_pane_mounts_against_missing_root_directory(
    tmp_path: Path,
) -> None:
    """No run yet → exports root doesn't exist → pane should mkdir it
    eagerly so DirectoryTree doesn't crash on mount."""
    missing_root = tmp_path / "nowhere" / "exports" / DESIGN_ID
    assert not missing_root.exists()

    async def drive(pane: ExportsPane, pilot: object) -> dict[str, Any]:
        await pilot.pause()  # type: ignore[attr-defined]
        # Tree mounted without crashing + the directory exists now.
        tree = pane.query_one("#exports-tree", DirectoryTree)
        return {"root_exists": missing_root.exists(), "has_tree": tree is not None}

    captured = _drive(drive, exports_root=missing_root)
    assert captured["root_exists"]
    assert captured["has_tree"]


def test_exports_pane_lists_files_after_export_pass(tmp_path: Path) -> None:
    """Populated exports tree → DirectoryTree's reload picks up the
    files. We don't simulate mouse clicks — just assert the tree
    widget's root node knows about the directory (the rest is
    Textual's job)."""
    exports_root = tmp_path / "exports" / DESIGN_ID
    _populate_exports(exports_root)

    async def drive(pane: ExportsPane, pilot: object) -> dict[str, Any]:
        tree = pane.query_one("#exports-tree", DirectoryTree)
        # Force one reload + pump events so the tree finishes its
        # background expansion (Textual schedules subtree loads).
        tree.reload()
        for _ in range(5):
            await pilot.pause()  # type: ignore[attr-defined]
        return {"root_path": str(tree.root.data.path)}  # type: ignore[union-attr]

    captured = _drive(drive, exports_root=exports_root)
    assert captured["root_path"] == str(exports_root)


def test_clicking_json_file_renders_syntax_highlighted_preview(
    tmp_path: Path,
) -> None:
    """``apply_file_preview`` on a .json file lands a Syntax(code=…)
    renderable in the preview Static."""
    exports_root = tmp_path / "exports" / DESIGN_ID
    paths = _populate_exports(exports_root)
    target = paths["rtl/counter.rtl.json"]

    async def drive(pane: ExportsPane, pilot: object) -> dict[str, Any]:
        pane.apply_file_preview(target)
        await pilot.pause()  # type: ignore[attr-defined]
        rend = _preview_renderable(pane)
        return {
            "is_syntax": isinstance(rend, Syntax),
            "code": rend.code if isinstance(rend, Syntax) else str(rend),
            "lexer": rend._lexer if isinstance(rend, Syntax) else None,
        }

    captured = _drive(drive, exports_root=exports_root)
    assert captured["is_syntax"]
    assert captured["lexer"] == "json"
    assert "artifact_id" in captured["code"]
    assert "counter.rtl" in captured["code"]


def test_clicking_verilog_file_renders_verilog_lexer_preview(
    tmp_path: Path,
) -> None:
    exports_root = tmp_path / "exports" / DESIGN_ID
    paths = _populate_exports(exports_root)
    target = paths["rtl/counter.v"]

    async def drive(pane: ExportsPane, pilot: object) -> dict[str, Any]:
        pane.apply_file_preview(target)
        await pilot.pause()  # type: ignore[attr-defined]
        rend = _preview_renderable(pane)
        return {
            "is_syntax": isinstance(rend, Syntax),
            "lexer": rend._lexer if isinstance(rend, Syntax) else None,
            "code": rend.code if isinstance(rend, Syntax) else "",
        }

    captured = _drive(drive, exports_root=exports_root)
    assert captured["is_syntax"]
    assert captured["lexer"] == "verilog"
    assert "module counter" in captured["code"]


def test_clicking_gds_file_renders_hex_header(tmp_path: Path) -> None:
    """``.gds`` files → plain-string preview with the HEADER badge +
    a hex dump of the first 64 bytes."""
    exports_root = tmp_path / "exports" / DESIGN_ID
    paths = _populate_exports(exports_root)
    target = paths["gds/counter.gds"]

    async def drive(pane: ExportsPane, pilot: object) -> dict[str, Any]:
        pane.apply_file_preview(target)
        await pilot.pause()  # type: ignore[attr-defined]
        rend = _preview_renderable(pane)
        return {
            "is_str": isinstance(rend, str),
            "text": str(rend),
        }

    captured = _drive(drive, exports_root=exports_root)
    assert captured["is_str"]
    assert "GDSII" in captured["text"]
    assert "HEADER record OK" in captured["text"]
    # Hex of the magic bytes is the first thing in the dump.
    assert "00 06 00 02" in captured["text"]
