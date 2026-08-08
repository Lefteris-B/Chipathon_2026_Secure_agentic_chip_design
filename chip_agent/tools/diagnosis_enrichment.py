"""F20.6 — diagnosis enrichment helpers.

Pure functions over bytes; no model calls, no store access. Two
extractors:

* :func:`extract_failing_test_source` slices one ``@cocotb.test()``-
  decorated function body out of a testbench source blob.
* :func:`parse_vcd_window` reads a VCD via :mod:`pyvcd` and renders a
  ``±radius`` clock-cycle text summary plus a signal-snapshot dict at
  the failure cycle.

Both helpers return empty results on any parse failure (graceful
fallback). The outer-loop repair prompt simply omits the corresponding
section when the field is empty, so a broken or missing VCD does not
crash the diagnosis pipeline.
"""

from __future__ import annotations

import io
import re
from typing import Any, TypedDict, cast

from vcd.reader import TokenKind, tokenize

__all__ = ["extract_failing_test_source", "parse_vcd_window"]


_CLOCK_NAMES = frozenset({"clk", "clock"})


class _Snapshot(TypedDict):
    cycle: int
    values: dict[str, str]


def extract_failing_test_source(
    testbench_bytes: bytes,
    test_name: str,
) -> str:
    """Slice one ``@cocotb.test()``-decorated function body from the
    testbench source.

    Anchored on the ``@cocotb.test(...)`` decorator immediately
    followed by ``def <test_name>(...)``. Walks subsequent lines while
    they are indented (i.e. inside the function body); stops at the
    first line whose indentation returns to module level (a fresh
    ``def``, ``class``, ``@decorator``, or any unindented statement).

    Returns an empty string on:
    - decoding failure (non-UTF-8 bytes)
    - missing test name in the source
    - empty test name

    The function never raises — diagnosis enrichment is best-effort.
    """
    if not test_name:
        return ""
    try:
        source = testbench_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ""

    # Match the decorator + def. Allow async def, parameterised
    # decorator (@cocotb.test(...)), and arbitrary whitespace.
    pattern = re.compile(
        r"^[ \t]*@cocotb\.test\s*\([^)]*\)\s*\n"
        r"[ \t]*(?:async\s+)?def[ \t]+"
        + re.escape(test_name)
        + r"\s*\(",
        re.MULTILINE,
    )
    m = pattern.search(source)
    if m is None:
        return ""

    # Capture from the decorator through the function body. The body
    # ends at the next module-level statement (no leading whitespace
    # except blank lines).
    lines = source[m.start():].splitlines()
    if not lines:
        return ""

    captured: list[str] = [lines[0]]  # decorator line
    if len(lines) > 1:
        captured.append(lines[1])  # def line

    # Walk body lines; capture indented lines + blank lines. Stop at
    # the first non-blank line whose first character is non-whitespace.
    for line in lines[2:]:
        if not line.strip():
            captured.append(line)
            continue
        if line[0] in (" ", "\t"):
            captured.append(line)
            continue
        break

    # Trim trailing blank lines.
    while captured and not captured[-1].strip():
        captured.pop()

    return "\n".join(captured) + "\n"


def parse_vcd_window(
    vcd_bytes: bytes,
    failure_cycle: int,
    *,
    radius: int = 3,
) -> tuple[str, dict[str, str]]:
    """Render a ``±radius`` clock-cycle text summary around a failure
    cycle plus a signal snapshot dict at the failure cycle.

    A "cycle" is a rising edge of the clock signal (the first signal
    named ``clk`` or ``clock`` discovered in the VCD's declarations).
    The summary lines are formatted as::

        Cycle 12: clk=0, rst_n=1, en=1, q=0x2a
        Cycle 13 (FAILURE): clk=1, rst_n=1, en=1, q=0x2b

    Multi-bit values render as ``0x<hex>``; single-bit values as ``0``
    / ``1`` / ``x`` / ``z``. The snapshot dict carries the same values
    keyed by signal name.

    Returns ``("", {})`` on:
    - garbage / non-VCD input
    - VCD with no clock signal discovered
    - failure_cycle never reached during the run

    Never raises.
    """
    try:
        signals, changes = _read_vcd(vcd_bytes)
    except Exception:
        return "", {}

    clock_id = _find_clock_id(signals)
    if clock_id is None:
        return "", {}

    # Reconstruct per-cycle snapshots by walking the change stream.
    # Each rising edge on the clock signal advances the cycle counter.
    snapshots = _walk_cycles(signals, changes, clock_id=clock_id)
    if not snapshots:
        return "", {}

    lo = max(0, failure_cycle - radius)
    hi = failure_cycle + radius
    window: list[_Snapshot] = [s for s in snapshots if lo <= s["cycle"] <= hi]
    if not window:
        return "", {}

    failure_snapshot = next(
        (s for s in window if s["cycle"] == failure_cycle), None,
    )
    snapshot_dict: dict[str, str] = (
        dict(failure_snapshot["values"]) if failure_snapshot else {}
    )

    summary_lines: list[str] = []
    for s in window:
        marker = " (FAILURE)" if s["cycle"] == failure_cycle else ""
        body = ", ".join(
            f"{name}={value}" for name, value in sorted(s["values"].items())
        )
        summary_lines.append(f"Cycle {s['cycle']}{marker}: {body}")
    return "\n".join(summary_lines), snapshot_dict


# --------------------------------------------------------------------------- #
# Internal VCD helpers
# --------------------------------------------------------------------------- #
def _read_vcd(
    vcd_bytes: bytes,
) -> tuple[dict[str, tuple[str, int]], list[tuple[int, str, str]]]:
    """Parse the VCD into a signal table + an ordered change stream.

    Returns:
      signals: ``id_code -> (signal_name, bit_width)``
      changes: list of ``(timestamp, id_code, value_str)`` in stream order.

    Raises on malformed VCD — callers wrap with try/except.
    """
    signals: dict[str, tuple[str, int]] = {}
    changes: list[tuple[int, str, str]] = []
    current_time = 0

    # pyvcd's Token.data is a tagged union over many record types; mypy
    # cannot narrow on Token.kind, so cast after the explicit kind check.
    for tok in tokenize(io.BytesIO(vcd_bytes)):
        if tok.kind is TokenKind.VAR:
            var = cast(Any, tok.data)
            signals[var.id_code] = (var.reference, var.size)
        elif tok.kind is TokenKind.CHANGE_TIME:
            current_time = int(cast(int, tok.data))
        elif tok.kind is TokenKind.CHANGE_SCALAR:
            scalar = cast(Any, tok.data)
            changes.append((current_time, scalar.id_code, str(scalar.value)))
        elif tok.kind is TokenKind.CHANGE_VECTOR:
            vector = cast(Any, tok.data)
            changes.append((current_time, vector.id_code, _vector_to_str(vector.value)))

    return signals, changes


def _vector_to_str(value: object) -> str:
    """Render a pyvcd VectorChange.value into a stable string repr.

    Multi-bit values come back as ints; we emit ``0x<hex>``. ``x`` / ``z``
    states arrive as strings already (``"x"`` / ``"z"``) — pass them through.
    """
    if isinstance(value, int):
        return f"0x{value:x}"
    return str(value)


def _find_clock_id(signals: dict[str, tuple[str, int]]) -> str | None:
    """Return the id_code of the first 1-bit signal named clk/clock.

    Case-insensitive. Returns None if no such signal exists in the
    VCD declarations (e.g. a combinational module).
    """
    for id_code, (name, width) in signals.items():
        if width == 1 and name.lower() in _CLOCK_NAMES:
            return id_code
    return None


def _walk_cycles(
    signals: dict[str, tuple[str, int]],
    changes: list[tuple[int, str, str]],
    *,
    clock_id: str,
) -> list[_Snapshot]:
    """Walk the change stream, advancing a cycle counter on every
    rising edge of the clock. Emit a snapshot of all signal values at
    each cycle.

    The first snapshot is emitted as cycle 0 once the clock first goes
    high (no implicit cycle before any clock edge — matches the
    convention used by cocotb harnesses).
    """
    name_by_id = {id_code: name for id_code, (name, _) in signals.items()}
    values: dict[str, str] = {name: "x" for name in name_by_id.values()}
    snapshots: list[_Snapshot] = []
    cycle = -1
    prev_clock = "x"

    for _time, id_code, value in changes:
        if id_code in name_by_id:
            values[name_by_id[id_code]] = value
        if id_code == clock_id:
            if prev_clock != "1" and value == "1":
                cycle += 1
                snapshots.append(_Snapshot(cycle=cycle, values=dict(values)))
            prev_clock = value

    return snapshots
