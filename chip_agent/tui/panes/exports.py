"""Exports tree + file preview pane (F14.5).

Renders the F11.6 ``exports/<design_id>/`` tree as a navigable
:class:`DirectoryTree`, with a syntax-highlighted preview underneath
that updates on file selection. Supports the file shapes the spine
produces:

* ``.json`` — :class:`rich.syntax.Syntax` with the ``json`` lexer.
* ``.v`` / ``.sv`` — Verilog lexer.
* ``.def`` — plain text (DEF is line-based ASCII).
* ``.py`` / ``.md`` — Python / Markdown lexers for the cocotb TB +
  the chat-minted spec body.
* ``.gds`` — binary, so no lexer; render a ``(size, HEADER badge,
  first-64-bytes hex)`` summary instead. The HEADER badge mirrors the
  F11.7 magic-bytes check (``\\x00\\x06\\x00\\x02``).

The tree re-walks once per second on the main thread — no native
filesystem watcher, no extra dependency. Single-design exports trees
are tiny (<30 files) so this is sub-millisecond.

If the exports directory doesn't yet exist (no run has driven the
spine), the pane eagerly creates it so :class:`DirectoryTree`'s mount
doesn't crash. The tree then shows an empty root until the first
``cmd_run`` / ``cmd_resume`` writes files.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from rich.syntax import Syntax
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DirectoryTree, Static

from chip_agent.tui.messages import ExportPreviewRequested

__all__ = ["ExportsPane"]


_IDLE_PREVIEW = "(select a file from the tree to preview)"

# Magic header bytes for a GDSII record — 2-byte length 0x0006 +
# 2-byte record type HEADER=0x0002. Same check the F11.7 acceptance
# tests use to verify a real layout came out of LibreLane.
_GDSII_HEADER_MAGIC = b"\x00\x06\x00\x02"

# Cap a preview to keep the Static widget responsive on huge files
# (e.g. a netlist after a big synth pass). 200 KiB is well past any
# real file in the demo flow; for now it's a soft guard, not a feature.
_PREVIEW_MAX_BYTES = 200 * 1024


class ExportsPane(Vertical):
    """``exports/<id>/`` tree + syntax-highlighted preview.

    Layout (top to bottom):

    * ``#exports-tree`` — :class:`DirectoryTree` rooted at the design's
      exports directory.
    * ``#exports-preview`` — :class:`Static` whose ``update`` is called
      with either a :class:`Syntax` (text files) or a plain string
      (GDS hex dump, "no preview" fallback).
    """

    DEFAULT_CSS = """
    ExportsPane {
        height: 1fr;
        border: solid $primary;
        layout: vertical;
    }

    ExportsPane > #exports-tree {
        height: 40%;
        border-bottom: solid $primary;
        background: $surface;
    }

    ExportsPane > #exports-preview {
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
        background: $surface;
    }
    """

    DEFAULT_REFRESH_MS: ClassVar[int] = 1_000

    def __init__(
        self,
        *,
        exports_root: Path,
        refresh_ms: int = DEFAULT_REFRESH_MS,
    ) -> None:
        super().__init__()
        # Eagerly materialise the root so DirectoryTree doesn't crash on
        # mount when no run has produced exports yet. The directory is
        # cheap, idempotent, and matches what cmd_run / cmd_resume would
        # do anyway via _export_artifacts.
        exports_root.mkdir(parents=True, exist_ok=True)
        self.exports_root = exports_root
        self.refresh_ms = refresh_ms

    def compose(self) -> ComposeResult:
        yield DirectoryTree(str(self.exports_root), id="exports-tree")
        yield Static(_IDLE_PREVIEW, id="exports-preview")

    def on_mount(self) -> None:
        self.set_interval(self.refresh_ms / 1000, self._refresh_tree)

    def _refresh_tree(self) -> None:
        """Re-walk the tree so new files appear as the spine writes them."""
        tree = self.query_one("#exports-tree", DirectoryTree)
        tree.reload()

    # --------------------------------------------------------- message handlers
    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected,
    ) -> None:
        """A tree node was selected — load its body into the preview."""
        self.post_message(ExportPreviewRequested(path=event.path))

    def on_export_preview_requested(
        self, message: ExportPreviewRequested,
    ) -> None:
        self.apply_file_preview(message.path)

    # ------------------------------------------------------------- public API
    def apply_file_preview(self, path: Path) -> None:
        """Update the preview Static directly from ``path``.

        Public so tests can drive a file selection without simulating
        a tree click. Also the entry point F14.6's "see source" links
        from the signoff dashboard will use.
        """
        preview = self.query_one("#exports-preview", Static)
        preview.update(_build_preview(path))


# --------------------------------------------------------------------------- #
# Pure helpers — easy to unit-test.
# --------------------------------------------------------------------------- #
_LEXER_BY_SUFFIX: dict[str, str] = {
    ".json": "json",
    ".v": "verilog",
    ".sv": "verilog",
    ".def": "text",
    ".py": "python",
    ".md": "markdown",
}


def _build_preview(path: Path):  # type: ignore[no-untyped-def]
    """Build a renderable for the preview ``Static`` based on ``path``.

    Returns either a :class:`rich.syntax.Syntax` (for known text
    file types) or a plain ``str`` (for GDS hex dumps, "file too
    large" fallbacks, and missing-file errors). The :class:`Static`
    widget accepts both.
    """
    if not path.exists() or not path.is_file():
        return f"(file not found: {path.name})"

    suffix = path.suffix.lower()
    if suffix == ".gds":
        return _render_gds_preview(path)

    try:
        body = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        return f"(can't decode {path.name} as UTF-8: {e})"

    if len(body) > _PREVIEW_MAX_BYTES:
        return (
            f"({path.name} is {len(body):,} bytes — preview capped at "
            f"{_PREVIEW_MAX_BYTES:,}. Open the file directly to inspect.)"
        )

    lexer = _LEXER_BY_SUFFIX.get(suffix)
    if lexer is None:
        return body
    return Syntax(
        body, lexer, theme="monokai",
        line_numbers=False, word_wrap=False,
    )


def _render_gds_preview(path: Path) -> str:
    """Multi-line summary of a GDSII binary blob.

    Shape:

    ::

        GDSII: 18164 bytes — HEADER record OK
        First 64 bytes:
        00 06 00 02 00 71 00 02 00 ff …

    The HEADER badge mirrors F11.7's magic-bytes assertion. The hex
    dump is the first 64 bytes only — enough to spot a truncated /
    corrupted blob without flooding the preview pane.
    """
    body = path.read_bytes()
    size = len(body)
    header = body[:4]
    valid = header == _GDSII_HEADER_MAGIC
    badge = "HEADER record OK" if valid else "MISSING HEADER record"
    hex_part = " ".join(f"{b:02x}" for b in body[:64])
    return (
        f"GDSII: {size:,} bytes — {badge}\n\n"
        f"First {min(64, size)} bytes:\n{hex_part}"
    )
