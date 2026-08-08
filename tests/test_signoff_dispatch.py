"""F21.3-E — _dispatch_signoff_failure tests.

The dispatcher decides what to do when SIGNOFF closes a gate.

Branches we pin:
* No agent wired → HUMAN gate (today's path).
* Agent wired + budget=0 → HUMAN gate.
* DRC-only / LVS-only / security-only failure → bypasses agent → HUMAN.
* READ_VERILOG-shape STA failure (gate closed but no negative slack)
  → bypasses agent → HUMAN.
* Timing failure with negative slack + agent wired + budget>0 → classify
  called, route appended to ``state.physical_repair_routes``, PHYSICAL
  re-entered.
* Agent picks ESCALATE_HUMAN → HUMAN gate; route NOT appended.
* PHYSICAL_REPAIR_ROUTED audit event emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from chip_agent.agents.signoff_stage import SignoffStageOutcome
from chip_agent.design_state import (
    ArtifactRef,
    DesignState,
    DRCReport,
    LayoutArtifact,
    LVSReport,
    MultiCornerSTAReport,
    NetlistArtifact,
    PhysicalRepairRoute,
    PhysicalRepairRouteKind,
    Provenance,
    SecurityReport,
    Stage,
    StageState,
    StageStatus,
    TimingReport,
    Violation,
)
from chip_agent.graph.stage_context import StageContext
from chip_agent.graph.state_graph import (
    HUMAN_REVIEW_NODE,
    _NODE_NAMES,
    _dispatch_signoff_failure,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.obs.tracing import NoopTracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.librelane import PhysicalConfig


HMAC_KEY = b"f21-3-test-hmac"


# --------------------------------------------------------------------------- #
# Stub agent — records classify() calls + returns a canned route.
# --------------------------------------------------------------------------- #
@dataclass
class _StubRepairAgent:
    canned: PhysicalRepairRoute
    calls: list[dict[str, Any]] = field(default_factory=list)

    def classify(
        self,
        timing: MultiCornerSTAReport | TimingReport,
        *,
        current_config: PhysicalConfig,
        history: list[PhysicalRepairRoute],
        attempts_so_far: int,
        max_attempts: int,
    ) -> PhysicalRepairRoute:
        self.calls.append({
            "timing": timing,
            "current_config": current_config,
            "history": history,
            "attempts_so_far": attempts_so_far,
            "max_attempts": max_attempts,
        })
        return self.canned


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


@pytest.fixture
def audit(tmp_path: Path):  # type: ignore[no-untyped-def]
    a = SqliteAuditLog(db_path=tmp_path / "audit.sqlite", hmac_key=HMAC_KEY)
    yield a
    a.close()


def _layout(store: SqliteArtifactStore) -> LayoutArtifact:
    blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout", design_id="d0", module_id="counter",
        def_file=blob, stage_reached="routed",
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return art


def _bad_multi_corner_timing(*, design_id: str = "d0") -> MultiCornerSTAReport:
    from chip_agent.design_state import CornerTiming
    return MultiCornerSTAReport(
        artifact_id="d0.counter.multi_sta", design_id=design_id, module_id="counter",
        corners=[
            CornerTiming(corner="tt", wns_ns=0.5, tns_ns=0.0),
            CornerTiming(corner="ss", wns_ns=-0.2, tns_ns=-0.4, setup_violations=2),
            CornerTiming(corner="ff", wns_ns=1.1, tns_ns=0.0),
        ],
        passed=False,
        violations=[Violation(
            code="STA.SETUP_VIOLATION", severity="error",
            message="ss corner", location="corner=ss",
        )],
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _ok_other_reports() -> tuple[DRCReport, LVSReport, SecurityReport]:
    return (
        DRCReport(
            artifact_id="d0.counter.drc", design_id="d0", module_id="counter",
            passed=True, violation_count=0,
            provenance=Provenance(produced_by=Stage.SIGNOFF),
        ),
        LVSReport(
            artifact_id="d0.counter.lvs", design_id="d0", module_id="counter",
            passed=True, matched=True, mismatch_count=0,
            provenance=Provenance(produced_by=Stage.SIGNOFF),
        ),
        SecurityReport(
            artifact_id="d0.counter.security", design_id="d0", module_id="counter",
            passed=True, checks_run=[],
            provenance=Provenance(produced_by=Stage.SIGNOFF),
        ),
    )


def _outcome_with_multi_corner_failure(
    store: SqliteArtifactStore,
) -> SignoffStageOutcome:
    timing = _bad_multi_corner_timing()
    drc, lvs, sec = _ok_other_reports()
    timing_ref = store.put(timing)
    drc_ref = store.put(drc)
    lvs_ref = store.put(lvs)
    sec_ref = store.put(sec)
    return SignoffStageOutcome(
        passed=False, timing=None, drc=drc, lvs=lvs, security=sec,
        timing_ref=None, drc_ref=drc_ref, lvs_ref=lvs_ref, security_ref=sec_ref,
        multi_corner_timing=timing,
        multi_corner_timing_ref=timing_ref,
    )


def _outcome_with_drc_only_failure(
    store: SqliteArtifactStore,
) -> SignoffStageOutcome:
    """Timing leg passes, DRC closes the gate. F21.3 must NOT fire."""
    from chip_agent.design_state import CornerTiming
    good_timing = MultiCornerSTAReport(
        artifact_id="d0.counter.multi_sta", design_id="d0", module_id="counter",
        corners=[CornerTiming(corner="tt", wns_ns=0.5, tns_ns=0.0)],
        passed=True,
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )
    bad_drc = DRCReport(
        artifact_id="d0.counter.drc", design_id="d0", module_id="counter",
        passed=False, violation_count=5,
        violations=[Violation(code="DRC.RULE", severity="error", message="DRC")],
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )
    _, ok_lvs, ok_sec = _ok_other_reports()
    return SignoffStageOutcome(
        passed=False, timing=None, drc=bad_drc, lvs=ok_lvs, security=ok_sec,
        timing_ref=None,
        drc_ref=store.put(bad_drc),
        lvs_ref=store.put(ok_lvs),
        security_ref=store.put(ok_sec),
        multi_corner_timing=good_timing,
        multi_corner_timing_ref=store.put(good_timing),
    )


def _outcome_with_read_verilog_failure(
    store: SqliteArtifactStore,
) -> SignoffStageOutcome:
    """Timing closed because OpenSTA couldn't parse the netlist (no slack
    parsed). F21.3 must NOT fire — it can't fix a structural error."""
    from chip_agent.design_state import CornerTiming
    bad_timing = MultiCornerSTAReport(
        artifact_id="d0.counter.multi_sta", design_id="d0", module_id="counter",
        corners=[CornerTiming(corner="tt", wns_ns=None, tns_ns=None)],
        passed=False,
        violations=[Violation(
            code="STA.READ_VERILOG_ERROR", severity="error",
            message="parse error", location="corner=tt:design.nl.v:18",
        )],
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )
    ok_drc, ok_lvs, ok_sec = _ok_other_reports()
    return SignoffStageOutcome(
        passed=False, timing=None, drc=ok_drc, lvs=ok_lvs, security=ok_sec,
        timing_ref=None,
        drc_ref=store.put(ok_drc), lvs_ref=store.put(ok_lvs),
        security_ref=store.put(ok_sec),
        multi_corner_timing=bad_timing,
        multi_corner_timing_ref=store.put(bad_timing),
    )


def _ctx(
    store: SqliteArtifactStore,
    *,
    agent: _StubRepairAgent | None = None,
    audit_log: SqliteAuditLog | None = None,
) -> StageContext:
    return StageContext(
        store=store,
        tracer=NoopTracer(),
        audit_log=audit_log,
        physical_config=PhysicalConfig(
            design_name="counter", top_module="counter",
            clock_period_ns=10.0,
        ),
        physical_repair_router=agent,
    )


def _state() -> DesignState:
    return DesignState(design_id="d0", name="counter-demo")


# --------------------------------------------------------------------------- #
# Branch coverage.
# --------------------------------------------------------------------------- #
def test_no_agent_wired_routes_to_human(store: SqliteArtifactStore) -> None:
    """Pre-F21.3 path — no agent → today's HUMAN escalation."""
    ctx = _ctx(store, agent=None)
    state = _state()
    outcome = _outcome_with_multi_corner_failure(store)

    cmd = _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    assert isinstance(cmd, Command)
    assert cmd.goto == HUMAN_REVIEW_NODE
    assert state.physical_repair_routes == []  # history untouched


def test_budget_exhausted_routes_to_human(store: SqliteArtifactStore) -> None:
    """Defensive: even when the agent is wired, budget=0 → HUMAN with no
    classify() call."""
    agent = _StubRepairAgent(canned=PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
    ))
    ctx = _ctx(store, agent=agent)
    state = _state()
    state.global_feedback_budget = 0
    outcome = _outcome_with_multi_corner_failure(store)

    cmd = _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    assert isinstance(cmd, Command)
    assert cmd.goto == HUMAN_REVIEW_NODE
    assert agent.calls == []  # agent NOT invoked


def test_drc_only_failure_bypasses_agent(store: SqliteArtifactStore) -> None:
    """Timing leg passes; DRC closes the gate. F21.3 must NOT try to fix
    that with a placement knob."""
    agent = _StubRepairAgent(canned=PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
    ))
    ctx = _ctx(store, agent=agent)
    state = _state()
    outcome = _outcome_with_drc_only_failure(store)

    cmd = _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    assert cmd.goto == HUMAN_REVIEW_NODE
    assert agent.calls == []


def test_read_verilog_failure_bypasses_agent(store: SqliteArtifactStore) -> None:
    """Structural STA error (no parsed slack) → can't be fixed by a knob.
    Bypass to HUMAN."""
    agent = _StubRepairAgent(canned=PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
    ))
    ctx = _ctx(store, agent=agent)
    state = _state()
    outcome = _outcome_with_read_verilog_failure(store)

    cmd = _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    assert cmd.goto == HUMAN_REVIEW_NODE
    assert agent.calls == []


def test_timing_failure_classify_called_route_appended(
    store: SqliteArtifactStore, audit: SqliteAuditLog,
) -> None:
    """Negative-slack failure + agent wired + budget>0 → classify called;
    the picked route lands on state.physical_repair_routes; PHYSICAL
    re-entered."""
    canned = PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
        reason="ss corner WNS negative",
    )
    agent = _StubRepairAgent(canned=canned)
    ctx = _ctx(store, agent=agent, audit_log=audit)
    state = _state()
    initial_budget = state.global_feedback_budget
    outcome = _outcome_with_multi_corner_failure(store)

    cmd = _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    assert cmd.goto == _NODE_NAMES[Stage.PHYSICAL]
    assert len(agent.calls) == 1
    # The agent saw the multi-corner timing report, not the single-corner one.
    assert isinstance(agent.calls[0]["timing"], MultiCornerSTAReport)
    # Route appended; budget decremented.
    assert state.physical_repair_routes == [canned]
    assert state.global_feedback_budget == initial_budget - 1
    # Audit event emitted.
    events = audit.events(state.design_id)
    repair_events = [e for e in events if e.event_type == EventType.PHYSICAL_REPAIR_ROUTED]
    assert len(repair_events) == 1
    assert repair_events[0].payload["kind"] == "lower_density"
    assert repair_events[0].payload["attempt"] == 1


def test_timing_failure_clears_physical_and_signoff_stage_state(
    store: SqliteArtifactStore,
) -> None:
    """Defensive: on a successful classify, the PHYSICAL + SIGNOFF stage
    states get cleared so re-entry rebuilds them cleanly."""
    canned = PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
    )
    agent = _StubRepairAgent(canned=canned)
    ctx = _ctx(store, agent=agent)
    state = _state()
    # Pre-populate the stages so we can assert they get cleared.
    state.stages[Stage.PHYSICAL] = StageState(
        stage=Stage.PHYSICAL,
        status=StageStatus.PASSED,
        head=ArtifactRef(
            artifact_id="x", version=1,
            kind=__import__("chip_agent.design_state",
                            fromlist=["ArtifactKind"]).ArtifactKind.LAYOUT,
            content_hash="sha256:" + "0" * 64,
        ),
        results=[],
    )
    state.stages[Stage.SIGNOFF] = StageState(
        stage=Stage.SIGNOFF, status=StageStatus.FAILED, results=[],
    )
    outcome = _outcome_with_multi_corner_failure(store)

    _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    # Both stages cleared.
    assert state.stages[Stage.PHYSICAL].head is None
    assert state.stages[Stage.PHYSICAL].status == StageStatus.PENDING
    assert state.stages[Stage.SIGNOFF].head is None
    assert state.stages[Stage.SIGNOFF].status == StageStatus.PENDING


def test_escalate_human_route_doesnt_append_to_history(
    store: SqliteArtifactStore, audit: SqliteAuditLog,
) -> None:
    """When the agent picks ESCALATE_HUMAN, we go to the human gate AND
    we don't append the route to the history (there's nothing to apply).
    Budget IS decremented (the classify call was made)."""
    agent = _StubRepairAgent(canned=PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.ESCALATE_HUMAN,
        reason="structural failure",
    ))
    ctx = _ctx(store, agent=agent, audit_log=audit)
    state = _state()
    initial_budget = state.global_feedback_budget
    outcome = _outcome_with_multi_corner_failure(store)

    cmd = _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    assert cmd.goto == HUMAN_REVIEW_NODE
    assert state.physical_repair_routes == []
    assert state.global_feedback_budget == initial_budget - 1
    # Audit still records the escalation decision.
    events = audit.events(state.design_id)
    repair_events = [e for e in events if e.event_type == EventType.PHYSICAL_REPAIR_ROUTED]
    assert len(repair_events) == 1
    assert repair_events[0].payload["kind"] == "escalate_human"


def test_agent_sees_reapplied_config_from_history(
    store: SqliteArtifactStore,
) -> None:
    """When the dispatcher fires for the second attempt, the agent's
    ``current_config`` argument reflects the FIRST route already applied.
    Pins the F21.3 invariant: classify() always sees the active config,
    not the original ctx.physical_config."""
    canned = PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.INCREASE_DELAY_OPTIMIZATION,
    )
    agent = _StubRepairAgent(canned=canned)
    ctx = _ctx(store, agent=agent)
    state = _state()
    # Pretend the first attempt already applied LOWER_DENSITY.
    state.physical_repair_routes.append(PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
    ))
    outcome = _outcome_with_multi_corner_failure(store)

    _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    seen_config = agent.calls[0]["current_config"]
    # The agent sees the post-first-route config (pl_target_density set).
    assert seen_config.pl_target_density is not None
    assert seen_config.pl_target_density < 0.5  # below target_utilization


def test_single_corner_timing_failure_routes_through_dispatcher(
    store: SqliteArtifactStore,
) -> None:
    """Single-corner mode (no multi_corner_timing) with negative wns_ns
    → dispatcher fires, agent sees a TimingReport."""
    canned = PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
    )
    agent = _StubRepairAgent(canned=canned)
    ctx = _ctx(store, agent=agent)
    state = _state()
    bad_timing = TimingReport(
        artifact_id="d0.counter.timing", design_id="d0", module_id="counter",
        passed=False, wns_ns=-0.85, tns_ns=-3.2,
        setup_violations=1, hold_violations=0,
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )
    drc, lvs, sec = _ok_other_reports()
    outcome = SignoffStageOutcome(
        passed=False, timing=bad_timing, drc=drc, lvs=lvs, security=sec,
        timing_ref=store.put(bad_timing),
        drc_ref=store.put(drc), lvs_ref=store.put(lvs), security_ref=store.put(sec),
    )

    cmd = _dispatch_signoff_failure(ctx, state, _layout(store), outcome)

    assert cmd.goto == _NODE_NAMES[Stage.PHYSICAL]
    assert isinstance(agent.calls[0]["timing"], TimingReport)
