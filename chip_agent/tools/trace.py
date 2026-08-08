"""Failing-assertion trace -> typed :class:`FailureDiagnosis` (F2.4).

This is the deterministic, **model-free** half of the outer-loop diagnosis
(see ``docs/control_graph.md`` and ``docs/tool_contracts.md``). The handler
later layers a router-produced ``suspected_cause`` on top before re-prompting
the semantic-repair agent.

F20.6 extends ``build_failure_diagnosis`` to accept an optional
``TestbenchArtifact`` + store seam; when both are provided the function
fetches the testbench source + VCD waveform and runs the diagnosis-
enrichment helpers (failing-test source slice + cycle window +
signal snapshot) before composing the artifact. Callers that already
have the enriched strings pre-extracted can pass them directly via
the keyword arguments — useful for tests and for callers that don't
want the store dependency.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from chip_agent.design_state import (
    BlobRef,
    FailureDiagnosis,
    Provenance,
    RTLArtifact,
    SimulationResult,
    Stage,
    TestbenchArtifact,
    TraceFacts,
)
from chip_agent.tools.diagnosis_enrichment import (
    extract_failing_test_source,
    parse_vcd_window,
)

if TYPE_CHECKING:  # avoid runtime import cycle with the store package
    pass

__all__ = [
    "build_failure_diagnosis",
    "failing_test_name_from_sim",
    "make_trace_facts",
    "parse_failing_assertion",
    "render_nl_summary",
]


class _BlobReader(Protocol):
    """Duck-typed slice of SqliteArtifactStore for enrichment lookup.

    Annotated structurally so callers can pass either the real store
    or a test fake, without trace.py importing SqliteArtifactStore."""

    def get_blob(self, ref: BlobRef) -> bytes: ...


# Patterns ordered most-specific first; first match wins.
_PATTERNS: list[re.Pattern[str]] = [
    # F19.8 differential cocotb harness — canonical token shape:
    #   "DIFF|cycle=4|signal=q|expected=5|actual=4"
    # This pattern wins over the older fuzzy patterns so the
    # differential TB's structured output is parsed deterministically
    # rather than via heuristic-matching against the same string.
    re.compile(
        r"DIFF\|cycle=(?P<cycle>\d+)"
        r"\|signal=(?P<signal>[\w.]+)"
        r"\|expected=(?P<expected>-?\d+)"
        r"\|actual=(?P<actual>-?\d+)",
    ),
    # "ack low at cycle 5, expected high"
    re.compile(
        r"(?P<signal>[\w.]+)\s+(?P<actual>\w+)\s+at\s+cycle\s+"
        r"(?P<cycle>\d+)\s*,\s*expected\s+(?P<expected>\w+)",
        re.IGNORECASE,
    ),
    # "at cycle 5, dut.q == 3 expected 4"
    re.compile(
        r"at\s+cycle\s+(?P<cycle>\d+)\s*,\s*(?P<signal>[\w.]+)\s*==\s*"
        r"(?P<actual>\S+)\s+expected\s+(?P<expected>\S+)",
        re.IGNORECASE,
    ),
    # "Expected dut.q to be high at cycle 5, got low"
    re.compile(
        r"expected\s+(?P<signal>[\w.]+)\s+to\s+be\s+(?P<expected>\w+)\s+"
        r"at\s+cycle\s+(?P<cycle>\d+)\s*,\s*got\s+(?P<actual>\w+)",
        re.IGNORECASE,
    ),
    # "dut.q expected 4 actual 3 at cycle 12"
    re.compile(
        r"(?P<signal>[\w.]+)\s+expected\s+(?P<expected>\S+)\s+actual\s+"
        r"(?P<actual>\S+)\s+at\s+cycle\s+(?P<cycle>\d+)",
        re.IGNORECASE,
    ),
]


def parse_failing_assertion(text: str) -> TraceFacts:
    """Pure heuristic: extract ``(failing_signal, cycle, expected, actual)`` from text.

    Returns a :class:`TraceFacts` with whatever could be extracted; fields stay
    ``None`` when the message doesn't yield them. Callers should treat this as
    a best-effort enrichment, not a guarantee — for shapes the patterns miss,
    the outer-loop NL summary still preserves the raw assertion.
    """
    if not text:
        return TraceFacts()
    for pat in _PATTERNS:
        m = pat.search(text)
        if m is None:
            continue
        return TraceFacts(
            failing_signal=m.group("signal"),
            cycle=int(m.group("cycle")),
            expected=m.group("expected"),
            actual=m.group("actual"),
        )
    return TraceFacts()


def make_trace_facts(sim: SimulationResult) -> TraceFacts:
    """Pick the first failing assertion in ``sim`` and parse it."""
    if not sim.failing_assertions:
        return TraceFacts()
    return parse_failing_assertion(sim.failing_assertions[0])


def render_nl_summary(facts: TraceFacts, *, raw_assertion: str | None = None) -> str:
    """One-line NL description of a trace failure — for the outer loop's prompt.

    The summary names the signal/cycle/expected-vs-actual when known and
    otherwise falls back to the raw assertion text. This is the *deterministic*
    summary; the router-produced ``suspected_cause`` rides alongside.
    """
    sig = facts.failing_signal
    if (
        sig is not None
        and facts.cycle is not None
        and facts.expected is not None
        and facts.actual is not None
    ):
        return (
            f"Signal `{sig}` was `{facts.actual}` at cycle {facts.cycle}; "
            f"expected `{facts.expected}`."
        )
    if raw_assertion:
        return f"Assertion failed: {raw_assertion}"
    return "Simulation reported a failure with no parseable details."


def failing_test_name_from_sim(sim: SimulationResult) -> str:
    """Extract the cocotb test name from ``sim.failing_assertions[0]``.

    The cocotb_sim parser emits ``failing_assertions`` entries of the
    form ``"<classname>::<name>"`` or ``"<classname>::<name>: <body>"``
    (and just ``"<name>"`` when classname is empty). Returns an empty
    string if no failing assertion is present or the format is
    unparseable. Never raises — diagnosis enrichment is best-effort.
    """
    if not sim.failing_assertions:
        return ""
    head = sim.failing_assertions[0]
    # Strip optional `: <body>` first (the body may itself contain
    # colons; split once at the first ": " sentinel).
    head_no_body = head.split(": ", 1)[0]
    if "::" in head_no_body:
        return head_no_body.rsplit("::", 1)[-1].strip()
    return head_no_body.strip()


def build_failure_diagnosis(
    sim: SimulationResult,
    rtl: RTLArtifact,
    *,
    suspected_cause: str | None = None,
    target_stage: Stage | None = None,
    testbench: TestbenchArtifact | None = None,
    store: _BlobReader | None = None,
    test_source: str = "",
    window_vcd_summary: str = "",
    active_signals_at_failure_cycle: dict[str, str] | None = None,
) -> FailureDiagnosis:
    """Compose a :class:`FailureDiagnosis` from the sim result.

    The ``suspected_cause`` field is left for the router (the outer-loop model)
    to fill in; the deterministic fields and ``nl_summary`` are populated here.

    F20.6: when ``testbench`` and ``store`` are both provided AND the
    caller did not already supply ``test_source`` / ``window_vcd_summary``
    / ``active_signals_at_failure_cycle``, the function fetches the
    testbench source blob and (if present) the simulation waveform blob
    and runs the diagnosis-enrichment helpers. Either kwarg shape is
    accepted so callers without a store can inject the enriched
    strings directly — used by unit tests and by the cross-stage
    feedback path.
    """
    facts = make_trace_facts(sim)
    raw = sim.failing_assertions[0] if sim.failing_assertions else None
    summary = render_nl_summary(facts, raw_assertion=raw)
    module = rtl.module_id or rtl.top_module

    enriched_test_source = test_source
    enriched_window = window_vcd_summary
    enriched_snapshot: dict[str, str] = dict(active_signals_at_failure_cycle or {})

    if store is not None:
        if testbench is not None and not enriched_test_source:
            test_name = failing_test_name_from_sim(sim)
            if test_name:
                tb_bytes = store.get_blob(testbench.source)
                enriched_test_source = extract_failing_test_source(
                    tb_bytes, test_name,
                )
        if (
            sim.waveform is not None
            and facts.cycle is not None
            and not enriched_window
            and not enriched_snapshot
        ):
            vcd_bytes = store.get_blob(sim.waveform)
            enriched_window, enriched_snapshot = parse_vcd_window(
                vcd_bytes, facts.cycle,
            )

    return FailureDiagnosis(
        artifact_id=f"{rtl.design_id}.{module}.diagnosis",
        design_id=rtl.design_id,
        module_id=rtl.module_id,
        failing_signal=facts.failing_signal,
        cycle=facts.cycle,
        expected=facts.expected,
        actual=facts.actual,
        suspected_cause=suspected_cause,
        nl_summary=summary,
        target_stage=target_stage,
        target_module=rtl.module_id,
        test_source=enriched_test_source,
        window_vcd_summary=enriched_window,
        active_signals_at_failure_cycle=enriched_snapshot,
        provenance=Provenance(
            produced_by=Stage.RTL,
            agent="rtl_specialist",
            inputs=[sim.ref(), rtl.ref()],
        ),
    )
