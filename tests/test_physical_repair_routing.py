"""F21.3 — PhysicalRepairRoutingAgent tests.

Mirrors tests/test_reflection_routing.py: canned-JSON happy paths for
each route; budget-exhausted short-circuit; defensive fallbacks
(unparseable JSON / missing key / unknown kind) → ESCALATE_HUMAN; task
dispatch + prompt inlining for both multi-corner and single-corner
timing inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from chip_agent.agents.physical_repair_routing import (
    PhysicalRepairRoutingAgent,
    PhysicalRepairRoutingError,
)
from chip_agent.design_state import (
    CornerTiming,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    MultiCornerSTAReport,
    PhysicalRepairRoute,
    PhysicalRepairRouteKind,
    Provenance,
    Stage,
    TaskType,
    TimingReport,
    Violation,
)
from chip_agent.tools.librelane import PhysicalConfig


# --------------------------------------------------------------------------- #
# StubRouter — copy of the F19.9 pattern.
# --------------------------------------------------------------------------- #
@dataclass
class StubRouter:
    chosen: str
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic",
            model="claude-opus-4-7",
            temperature=0.0,
            seed=None,
            prompt_tokens=180,
            completion_tokens=48,
            cost_usd=0.0017,
        ),
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        self.calls.append({
            "task": task,
            "context": dict(context),
            "escalation": escalation,
            "n": n,
        })
        return GenerationResult(
            candidates=[self.chosen],
            chosen=self.chosen,
            invocation=self.invocation,
        )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _multi_corner_report(
    *,
    ss_wns: float = -0.2,
    tt_wns: float = 0.5,
    ff_wns: float = 1.1,
    violations: list[Violation] | None = None,
) -> MultiCornerSTAReport:
    return MultiCornerSTAReport(
        artifact_id="d0.counter.multi_sta",
        design_id="d0",
        module_id="counter",
        corners=[
            CornerTiming(corner="tt", wns_ns=tt_wns, tns_ns=0.0),
            CornerTiming(corner="ss", wns_ns=ss_wns, tns_ns=-0.4,
                         setup_violations=2),
            CornerTiming(corner="ff", wns_ns=ff_wns, tns_ns=0.0),
        ],
        passed=ss_wns >= 0,
        violations=violations or [],
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _single_corner_report(*, wns_ns: float = -0.85) -> TimingReport:
    return TimingReport(
        artifact_id="d0.counter.timing",
        design_id="d0",
        module_id="counter",
        passed=False,
        wns_ns=wns_ns, tns_ns=-3.2,
        setup_violations=1, hold_violations=0,
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _agent(router: StubRouter) -> PhysicalRepairRoutingAgent:
    return PhysicalRepairRoutingAgent(router=router, design_id="d0")


def _config() -> PhysicalConfig:
    return PhysicalConfig(
        design_name="counter", top_module="counter",
        clock_period_ns=5.0, target_utilization=0.5,
    )


# --------------------------------------------------------------------------- #
# Constructor / config-error tests
# --------------------------------------------------------------------------- #
def test_agent_rejects_empty_design_id() -> None:
    with pytest.raises(PhysicalRepairRoutingError):
        PhysicalRepairRoutingAgent(
            router=StubRouter(chosen="{}"), design_id="",
        )


# --------------------------------------------------------------------------- #
# Happy-path classification — one test per route.
# --------------------------------------------------------------------------- #
def test_classify_picks_lower_density_when_congestion_shaped() -> None:
    router = StubRouter(chosen=(
        '{"route": "lower_density", '
        '"reason": "ss corner WNS negative, suggests routing congestion"}'
    ))
    agent = _agent(router)
    route = agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.LOWER_DENSITY
    assert "routing congestion" in route.reason


def test_classify_picks_increase_delay_optimization() -> None:
    router = StubRouter(chosen=(
        '{"route": "increase_delay_optimization", '
        '"reason": "deep logic cone on critical path"}'
    ))
    agent = _agent(router)
    route = agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=1, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.INCREASE_DELAY_OPTIMIZATION


def test_classify_picks_relax_clock_period() -> None:
    router = StubRouter(chosen=(
        '{"route": "relax_clock_period", '
        '"reason": "tried both knob fixes; loosen target frequency"}'
    ))
    agent = _agent(router)
    route = agent.classify(
        _single_corner_report(),
        current_config=_config(),
        history=[
            PhysicalRepairRoute(
                kind=PhysicalRepairRouteKind.LOWER_DENSITY,
                reason="first try",
            ),
            PhysicalRepairRoute(
                kind=PhysicalRepairRouteKind.INCREASE_DELAY_OPTIMIZATION,
                reason="second try",
            ),
        ],
        attempts_so_far=2, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD


def test_classify_picks_escalate_human_on_model_decision() -> None:
    router = StubRouter(chosen=(
        '{"route": "escalate_human", '
        '"reason": "structural failure, not slack-driven"}'
    ))
    agent = _agent(router)
    route = agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.ESCALATE_HUMAN


# --------------------------------------------------------------------------- #
# Defensive fallbacks.
# --------------------------------------------------------------------------- #
def test_classify_short_circuits_when_budget_exhausted() -> None:
    """attempts_so_far >= max_attempts → no router call, ESCALATE_HUMAN."""
    router = StubRouter(chosen='{"route": "lower_density"}')
    agent = _agent(router)
    route = agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=3, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.ESCALATE_HUMAN
    assert "exhausted" in route.reason
    assert len(router.calls) == 0  # router not invoked


def test_classify_falls_back_on_unparseable_json() -> None:
    router = StubRouter(chosen="this is not JSON at all")
    agent = _agent(router)
    route = agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.ESCALATE_HUMAN
    assert "unparseable" in route.reason


def test_classify_falls_back_on_missing_route_key() -> None:
    router = StubRouter(chosen='{"reason": "no route"}')
    agent = _agent(router)
    route = agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.ESCALATE_HUMAN
    assert "missing 'route'" in route.reason


def test_classify_falls_back_on_unknown_route_value() -> None:
    router = StubRouter(chosen='{"route": "tighten_clock_uncertainty"}')
    agent = _agent(router)
    route = agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.ESCALATE_HUMAN
    assert "unknown route" in route.reason


def test_classify_handles_json_wrapped_in_prose() -> None:
    """Frontier models sometimes prefix prose; the greedy regex fallback
    finds the JSON object anyway."""
    router = StubRouter(chosen=(
        "Looking at the timing report, the failure pattern suggests "
        'routing congestion at the slow corner. {"route": "lower_density", '
        '"reason": "ss corner congestion"}'
    ))
    agent = _agent(router)
    route = agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    assert route.kind is PhysicalRepairRouteKind.LOWER_DENSITY


# --------------------------------------------------------------------------- #
# Task dispatch + prompt inlining.
# --------------------------------------------------------------------------- #
def test_classify_dispatches_to_physical_repair_routing_task() -> None:
    router = StubRouter(chosen='{"route": "lower_density"}')
    agent = _agent(router)
    agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    assert len(router.calls) == 1
    assert router.calls[0]["task"] is TaskType.PHYSICAL_REPAIR_ROUTING


def test_user_prompt_inlines_per_corner_wns_when_multi_corner() -> None:
    router = StubRouter(chosen='{"route": "lower_density"}')
    agent = _agent(router)
    agent.classify(
        _multi_corner_report(ss_wns=-0.42, tt_wns=0.5, ff_wns=1.1),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    prompt = router.calls[0]["context"]["prompt"]
    # Per-corner WNS visible to the model
    assert "tt" in prompt and "ss" in prompt and "ff" in prompt
    assert "-0.42" in prompt  # the actual negative WNS
    # Current config knobs visible
    assert "clock_period_ns" in prompt
    assert "pl_target_density" in prompt
    assert "synth_strategy" in prompt


def test_user_prompt_inlines_single_corner_when_typical_only() -> None:
    """Falls back to single-corner shape uniformly so the model doesn't
    need to handle two prompt layouts."""
    router = StubRouter(chosen='{"route": "relax_clock_period"}')
    agent = _agent(router)
    agent.classify(
        _single_corner_report(wns_ns=-0.85),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    prompt = router.calls[0]["context"]["prompt"]
    assert "-0.85" in prompt
    assert "wns_ns" in prompt


def test_user_prompt_inlines_repair_history_most_recent_first() -> None:
    """The agent reads its own history so it doesn't pick the same
    losing route twice."""
    router = StubRouter(chosen='{"route": "relax_clock_period"}')
    agent = _agent(router)
    history = [
        PhysicalRepairRoute(
            kind=PhysicalRepairRouteKind.LOWER_DENSITY,
            reason="first try; failed at ss",
        ),
        PhysicalRepairRoute(
            kind=PhysicalRepairRouteKind.INCREASE_DELAY_OPTIMIZATION,
            reason="second try; still tight",
        ),
    ]
    agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=history,
        attempts_so_far=2, max_attempts=3,
    )
    prompt = router.calls[0]["context"]["prompt"]
    # Most-recent-first: -1 attempt is the increase_delay one
    pos_minus1 = prompt.index("attempt -1")
    pos_minus2 = prompt.index("attempt -2")
    assert pos_minus1 < pos_minus2
    assert "increase_delay_optimization" in prompt[pos_minus1:pos_minus2]
    assert "lower_density" in prompt[pos_minus2:]


def test_user_prompt_empty_history_message() -> None:
    router = StubRouter(chosen='{"route": "lower_density"}')
    agent = _agent(router)
    agent.classify(
        _multi_corner_report(),
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    prompt = router.calls[0]["context"]["prompt"]
    assert "first repair attempt" in prompt


def test_user_prompt_inlines_violations_when_present() -> None:
    router = StubRouter(chosen='{"route": "escalate_human"}')
    agent = _agent(router)
    report = _multi_corner_report(violations=[
        Violation(
            code="STA.READ_VERILOG_ERROR",
            severity="error",
            message="OpenSTA could not parse design.nl.v at line 18",
            location="corner=ss:design.nl.v:18",
            detail={"corner": "ss"},
        ),
    ])
    agent.classify(
        report,
        current_config=_config(),
        history=[],
        attempts_so_far=0, max_attempts=3,
    )
    prompt = router.calls[0]["context"]["prompt"]
    assert "STA.READ_VERILOG_ERROR" in prompt


def test_system_prompt_names_all_four_routes() -> None:
    """Defensive: the model sees all four exact route strings in the
    system prompt; a future renaming PR can't drift them apart."""
    sp = PhysicalRepairRoutingAgent.SYSTEM_PROMPT
    assert "lower_density" in sp
    assert "increase_delay_optimization" in sp
    assert "relax_clock_period" in sp
    assert "escalate_human" in sp
