"""Textual TUI for chip-agent (F14.1 scaffolding).

The TUI is a thin client over the existing ``chip_agent.cli`` entry
points. It exposes one window with a chat pane today; later F-features
mount the pipeline-progress, audit-log, exports-tree, and signoff
dashboard panes alongside it.

The package is structured so each pane + worker stays small and
independently testable via ``textual.App.run_test()``.
"""

from __future__ import annotations

from chip_agent.tui.app import ChipAgentApp

__all__ = ["ChipAgentApp"]
