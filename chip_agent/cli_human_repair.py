"""Console transcript provider for interactive human repair (F23.5, Option A).

Supplies :attr:`StageContext.human_transcript_for` for the blocking
``chat``/``run`` REPL path: when the RTL loop escalates to a HUMAN turn
(:func:`chip_agent.graph.state_graph._try_interactive_human_turn`), this
prints the :class:`FailureDiagnosis` to the operator's console and blocks
reading their guidance from stdin. The returned text is then distilled
into a typed ``HumanHint`` and re-seeds a bounded retry — the console
never touches the gate.

Returning ``None`` (the operator presses Enter on an empty line, or EOF)
declines the turn, so the graph falls back to the human gate exactly as
before. Safe for non-interactive contexts: callers only wire this in when
an interactive terminal is intended.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO

from chip_agent.design_state import DesignState, FailureDiagnosis

__all__ = ["TranscriptProvider", "console_transcript_provider"]

TranscriptProvider = Callable[[DesignState, str, FailureDiagnosis], str | None]


def console_transcript_provider(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> TranscriptProvider:
    """Build a blocking stdin/stdout transcript provider.

    ``stdin``/``stdout`` default to the process streams; tests inject
    ``io.StringIO`` to drive the prompt without a terminal.
    """
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout

    def _provider(
        _state: DesignState, module_id: str, diagnosis: FailureDiagnosis,
    ) -> str | None:
        out_stream.write(_render_prompt(module_id, diagnosis))
        out_stream.flush()
        lines: list[str] = []
        while True:
            raw = in_stream.readline()
            if raw == "":  # EOF
                break
            line = raw.rstrip("\n")
            if line.strip() == "":  # blank line terminates the block
                break
            lines.append(line)
        transcript = "\n".join(lines).strip()
        if not transcript:
            out_stream.write("  (no guidance given — escalating to the human gate)\n")
            out_stream.flush()
            return None
        return transcript

    return _provider


def _render_prompt(module_id: str, diagnosis: FailureDiagnosis) -> str:
    """Format the diagnosis + input prompt shown to the operator."""
    lines = [
        "",
        "=" * 72,
        f"  RTL repair is stuck on module {module_id!r} — your help is needed.",
        "=" * 72,
        f"  Summary:        {diagnosis.nl_summary.strip() or '<none>'}",
        f"  Failing signal: {diagnosis.failing_signal or '<unknown>'}",
        f"  Cycle:          {diagnosis.cycle if diagnosis.cycle is not None else '<unknown>'}",
        f"  Expected:       {diagnosis.expected or '<unknown>'}",
        f"  Actual:         {diagnosis.actual or '<unknown>'}",
    ]
    if diagnosis.suspected_cause:
        lines.append(f"  Suspected:      {diagnosis.suspected_cause}")
    lines += [
        "-" * 72,
        "  Describe the fix (what's wrong / what to try). It will be distilled",
        "  into a hint that seeds one more bounded, gated repair attempt.",
        "  End with a blank line; press Enter on an empty line to skip.",
        "> ",
    ]
    return "\n".join(lines)
