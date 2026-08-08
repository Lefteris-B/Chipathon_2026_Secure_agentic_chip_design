"""TUI panes.

Each pane is a self-contained Textual widget that reads from a slice
of the chip-agent public API:

* :class:`ChatPane` (F14.1) — streams via ``router.stream``.
* :class:`PipelinePane` (F14.2) — polls the LangGraph checkpoint.
* :class:`AuditPane` (F14.4) — polls ``SqliteAuditLog.events``.
* :class:`ExportsPane` (F14.5) — walks ``<run-dir>/exports/<design_id>/``.
* :class:`SignoffDashboardPane` (F14.6) — reads signoff verification artifacts.
"""
