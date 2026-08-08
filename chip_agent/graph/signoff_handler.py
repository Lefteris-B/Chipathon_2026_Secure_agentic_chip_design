"""SIGNOFF stage gate (F9.1).

Signoff is a verification stage — it produces four reports (STA, DRC,
LVS, security) but **no head artifact**. The handler appends every
report ref to ``ss.results`` so the gate predicate + downstream feedback
edges have the full picture, sets ``status = PASSED`` on the conjunction
(matching CLAUDE.md's "SIGNOFF gate is a conjunction over STA + DRC +
LVS + security ``gate_ok``"), and on failure routes ``last_failure`` to
the first failing leg's ref so the audit log + cross-stage feedback have
something attribution-shaped to point at.

Unlike RTL / SYNTH / PHYSICAL there is no ``promote_to_head`` call —
the next stage (GDSII) reads the existing PHYSICAL head; signoff's job
is purely to gate the advance.
"""

from __future__ import annotations

from chip_agent.agents.signoff_stage import SignoffStageOutcome
from chip_agent.design_state import (
    ArtifactRef,
    DesignState,
    Stage,
    StageState,
    StageStatus,
)
from chip_agent.graph.blackboard import get_or_create_stage_state
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = ["apply_signoff_outcome"]


def apply_signoff_outcome(
    design: DesignState,
    outcome: SignoffStageOutcome,
    *,
    store: SqliteArtifactStore,
) -> StageState:
    """Apply ``outcome`` to ``design.stages[SIGNOFF]``.

    F21.2: appends either ``timing_ref`` (single-corner path) OR
    ``multi_corner_timing_ref`` (multi-corner path) to ``ss.results`` —
    exactly one is non-None per run. DRC / LVS / security refs always
    append. The ``multi_corner_fallback`` flag is surfaced on the
    outcome for the caller (state graph) to emit a
    ``MULTI_CORNER_FALLBACK`` audit event; the handler itself only
    needs to know which timing ref to record.
    """
    ss = get_or_create_stage_state(design, Stage.SIGNOFF)
    # F21.2: exactly one of timing_ref / multi_corner_timing_ref is non-None.
    timing_ref = outcome.timing_ref or outcome.multi_corner_timing_ref
    refs = (timing_ref, outcome.drc_ref, outcome.lvs_ref, outcome.security_ref)
    for ref in refs:
        if ref is not None and ref not in ss.results:
            ss.results.append(ref)

    if outcome.passed:
        ss.status = StageStatus.PASSED
        ss.last_failure = None
        return ss

    ss.last_failure = _first_failing_ref(outcome)
    # No driver-side escalation for signoff (failures point upstream via
    # F5.4's apply_cross_stage_feedback in a later feature). Mark FAILED
    # and let decide_transition route.
    ss.status = StageStatus.FAILED
    return ss


def _first_failing_ref(outcome: SignoffStageOutcome) -> ArtifactRef:
    """Pick the ref attribution-shaped readers point at first.

    F21.2: reads whichever timing ref actually fired; falls through to
    DRC / LVS / security in the original order otherwise.
    """
    if outcome.multi_corner_timing is not None:
        if (
            not outcome.multi_corner_timing.gate_ok
            and outcome.multi_corner_timing_ref is not None
        ):
            return outcome.multi_corner_timing_ref
    elif outcome.timing is not None:
        if not outcome.timing.gate_ok and outcome.timing_ref is not None:
            return outcome.timing_ref
    if not outcome.drc.gate_ok:
        return outcome.drc_ref
    if not outcome.lvs.gate_ok:
        return outcome.lvs_ref
    return outcome.security_ref  # security gate is the only one left
