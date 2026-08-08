"""Shared helpers for tool-output parsing.

The IIC-OSIC-TOOLS image's entrypoint prints a multi-line bootstrap
banner before exec'ing the user command, even when ``--skip`` is passed
to short-circuit the VNC setup. Lines like:

    [INFO] USER_ID: 1000, GROUP_ID: 1000
    [INFO] Final PATH variable: /foss/tools/bin:/foss/tools/sak:...
    [INFO] Final PYTHONPATH variable: /usr/lib/python312.zip:...

leak into every tool's stdout. Tool parsers (verible, verilator, yosys,
netgen, magic, opensta, gdsii) fall back to recording any unparsable
line as a ``LINT.UNKNOWN`` / ``UNRECOGNISED`` violation when the run
returns non-zero, so banner lines get reported as compiler / lint
errors and the RTL repair loop can never converge.

:func:`strip_entrypoint_banner` is the single seam every parser calls
before splitting stdout/stderr into lines.
"""

from __future__ import annotations

import re

__all__ = ["strip_entrypoint_banner"]


_BANNER_LINE_RE = re.compile(
    r"^\[(INFO|WARN|WARNING|ERROR|DEBUG)\]\s.*$",
)


def strip_entrypoint_banner(text: str) -> str:
    """Drop IIC-OSIC-TOOLS entrypoint banner lines from ``text``.

    Removes any line that begins with ``[<LEVEL>] `` where ``<LEVEL>`` is
    one of ``INFO``, ``WARN``, ``WARNING``, ``ERROR``, ``DEBUG`` — the set
    the entrypoint actually emits. Returns the remaining lines joined
    with ``\\n``.

    Empty input returns an empty string. Trailing newlines are preserved
    by joining the surviving lines with ``\\n`` and adding a final ``\\n``
    if the input had one — parsers downstream split on ``splitlines()``
    anyway, but keeping the shape stable avoids surprises when callers
    feed the result back into a logger or test snapshot.
    """
    if not text:
        return text
    kept = [
        line for line in text.splitlines()
        if not _BANNER_LINE_RE.match(line)
    ]
    out = "\n".join(kept)
    if text.endswith("\n") and out:
        out += "\n"
    return out
