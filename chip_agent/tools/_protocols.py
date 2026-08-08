"""Shared Protocols the tool services depend on.

The sandbox seam is duck-typed (rather than the concrete :class:`SandboxRunner`)
so tests can inject a stub without dragging Docker into every parser test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from chip_agent.design_state import ToolRun

__all__ = ["SandboxLike"]


class SandboxLike(Protocol):
    """The subset of :class:`SandboxRunner` the tool services use."""

    def run(
        self,
        cmd: list[str],
        mount: Path | str,
        *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun: ...
