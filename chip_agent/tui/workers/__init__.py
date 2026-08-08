"""Background worker functions for the TUI.

Every blocking call (``router.stream``, ``cmd_run``, ``audit.events``,
filesystem scans) runs inside a worker thread. Workers communicate
back to the main TUI thread via ``App.call_from_thread`` posting
``chip_agent.tui.messages.*`` Messages.
"""
