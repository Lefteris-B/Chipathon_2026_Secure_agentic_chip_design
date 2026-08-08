"""Synthesis stage driver (F6.1).

Thin wrapper around :class:`YosysSynthService` that produces a typed
:class:`SynthStageOutcome` for the control graph's gate. The driver does
**not** run an LLM repair loop: synth's "inner loop fixes from synth
warnings" is two distinct work items — (a) script/config tuning, which is
deterministic and lives here, and (b) cross-stage feedback into RTL,
which is :mod:`chip_agent.graph.feedback`'s F5.4 surface. Both are
budget-bounded by the control graph; the driver itself stays pure of
orchestration.

The outcome carries everything the gate handler needs to apply: the new
:class:`NetlistArtifact` ref (for ``promote_to_head``), the
:class:`SynthesisReport` ref (for ``record_failure``), and a hint for
the escalation ladder (``escalate_to``).
"""

from __future__ import annotations

from dataclasses import dataclass

from chip_agent.design_state import (
    ArtifactRef,
    EscalationLevel,
    NetlistArtifact,
    RTLArtifact,
    SynthesisReport,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.yosys import YosysSynthService

__all__ = [
    "SynthStageDriver",
    "SynthStageError",
    "SynthStageOutcome",
]


class SynthStageError(ValueError):
    """The driver was misconfigured (e.g. design_id mismatch)."""


@dataclass(frozen=True)
class SynthStageOutcome:
    """Result of one ``drive`` call.

    ``passed`` is True iff the synth report's gate closed (no errors, no
    inferred latches, no blackboxes). ``escalate_to`` is ``None`` on
    success, or a hint for the control graph: backend failures default to
    ``HUMAN`` after the OUTER budget, but the gate may instead route to
    :data:`Transition.FEEDBACK_UPSTREAM` (RTL) if the heuristic in F5.4
    fires.
    """

    passed: bool
    escalate_to: EscalationLevel | None
    netlist: NetlistArtifact
    netlist_ref: ArtifactRef
    report: SynthesisReport
    report_ref: ArtifactRef


class SynthStageDriver:
    """Drive one synth pass on a module's RTL head."""

    def __init__(
        self,
        *,
        service: YosysSynthService,
        store: SqliteArtifactStore,
        design_id: str,
    ) -> None:
        if not design_id:
            raise SynthStageError("design_id must be non-empty")
        self.service = service
        self.store = store
        self.design_id = design_id

    def drive(
        self,
        rtl: RTLArtifact,
        *,
        std_cell_lib: str = "gf180mcu_fd_sc_mcu7t5v0",
        time_limit_s: int | None = None,
    ) -> SynthStageOutcome:
        """Run synth on ``rtl`` and return a typed outcome."""
        if rtl.design_id != self.design_id:
            raise SynthStageError(
                f"rtl.design_id {rtl.design_id!r} != driver.design_id "
                f"{self.design_id!r}"
            )

        netlist, report = self.service.synthesize(
            rtl, std_cell_lib=std_cell_lib, time_limit_s=time_limit_s,
        )
        netlist_ref = self.store.put(netlist)
        report_ref = self.store.put(report)

        # The driver only knows about a single attempt. Whether to RETRY,
        # ESCALATE, or FEEDBACK_UPSTREAM is the control graph's call; the
        # outcome merely flags HUMAN when the gate didn't close so callers
        # without budget tracking get a sane default.
        escalate_to = None if report.gate_ok else EscalationLevel.HUMAN
        return SynthStageOutcome(
            passed=report.gate_ok,
            escalate_to=escalate_to,
            netlist=netlist,
            netlist_ref=netlist_ref,
            report=report,
            report_ref=report_ref,
        )
