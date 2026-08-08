"""Frozen-app entry point.

pyproject.toml exposes `chip-agent = "chip_agent.cli:main"` as a console script,
but PyInstaller needs a real script file to analyse. This is that file.
"""

from __future__ import annotations

import multiprocessing
import sys


def _run() -> int:
    from chip_agent.cli import main

    return main()


if __name__ == "__main__":
    # Required so frozen builds don't re-launch the whole app in worker
    # processes on Windows / macOS spawn-based start methods.
    multiprocessing.freeze_support()
    sys.exit(_run())
