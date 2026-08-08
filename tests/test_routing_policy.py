"""F3.2 acceptance: routing policy is a pure function of the blackboard."""

from __future__ import annotations

import pytest

from chip_agent.design_state import EscalationLevel, TaskType
from chip_agent.routing.policy import (
    FailureType,
    ResolutionError,
    loop_for,
    params_for,
    resolve_model,
)
from chip_agent.settings import LoopBinding, ModelEntry, RoutingSettings, TaskBinding


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _routing(
    *,
    inner: str = "local-coder",
    outer: str = "frontier",
    rtl_gen: str = "local-coder",
    diagnose: str = "frontier",
    plan: str = "frontier",
) -> RoutingSettings:
    return RoutingSettings(
        registry={
            "local-coder": ModelEntry(
                provider="ollama", model="qwen2.5-coder:7b",
                endpoint="http://localhost:11434",
            ),
            "frontier": ModelEntry(provider="anthropic", model="claude-opus-4-7"),
            "local-big": ModelEntry(
                provider="ollama", model="qwen2.5-coder:32b",
                endpoint="http://localhost:11434",
            ),
        },
        loops={
            "inner": LoopBinding(model=inner, temperature=0.4, n=3),
            "outer": LoopBinding(model=outer, temperature=0.2, n=1),
        },
        tasks={
            "rtl_gen": TaskBinding(model=rtl_gen, temperature=0.8, n=8),
            "diagnose": TaskBinding(model=diagnose, temperature=0.2, n=1),
            "plan": TaskBinding(model=plan, temperature=0.2, n=1),
        },
    )


# --------------------------------------------------------------------------- #
# loop_for
# --------------------------------------------------------------------------- #
def test_loop_for_classifies_failures() -> None:
    assert loop_for(FailureType.NONE) is EscalationLevel.INNER
    assert loop_for(FailureType.SYNTACTIC) is EscalationLevel.INNER
    assert loop_for(FailureType.SEMANTIC) is EscalationLevel.OUTER
    assert loop_for(FailureType.PHYSICAL) is EscalationLevel.OUTER
    assert loop_for(FailureType.TIMING) is EscalationLevel.OUTER


# --------------------------------------------------------------------------- #
# AC: RTL_GEN routes local; DIAGNOSE routes frontier.
# --------------------------------------------------------------------------- #
def test_rtl_gen_routes_to_local_coder() -> None:
    routing = _routing()
    entry = resolve_model(TaskType.RTL_GEN, routing=routing)
    assert entry.provider == "ollama"
    assert entry.model == "qwen2.5-coder:7b"


def test_diagnose_routes_to_frontier() -> None:
    routing = _routing()
    entry = resolve_model(TaskType.DIAGNOSE, routing=routing)
    assert entry.provider == "anthropic"
    assert entry.model == "claude-opus-4-7"


def test_plan_routes_to_frontier() -> None:
    routing = _routing()
    entry = resolve_model(TaskType.PLAN, routing=routing)
    assert entry.provider == "anthropic"


def test_contract_extraction_routes_to_bound_model() -> None:
    """F19.3: TaskType.CONTRACT_EXTRACTION must resolve via the routing
    config's task binding. Without the YAML/policy wire the agent would
    crash with ResolutionError at first call."""
    routing = _routing()
    routing.tasks["contract_extraction"] = TaskBinding(
        model="frontier", temperature=0.0, n=1,
    )
    entry = resolve_model(TaskType.CONTRACT_EXTRACTION, routing=routing)
    assert entry.provider == "anthropic"
    assert entry.model == "claude-opus-4-7"


def test_oracle_gen_routes_to_bound_model() -> None:
    """F19.4: TaskType.ORACLE_GEN must resolve via the routing config.
    Without it the OracleGenAgent crashes with ResolutionError on first
    router.generate() call."""
    routing = _routing()
    routing.tasks["oracle_gen"] = TaskBinding(
        model="frontier", temperature=0.2, n=1,
    )
    entry = resolve_model(TaskType.ORACLE_GEN, routing=routing)
    assert entry.provider == "anthropic"
    assert entry.model == "claude-opus-4-7"


def test_assertion_extract_routes_to_bound_model() -> None:
    """F19.5 stage A: AssertionGenAgent dispatches to ASSERTION_EXTRACT
    on its first call. An unbound task type would crash the agent
    before it ever produced a SignalMap."""
    routing = _routing()
    routing.tasks["assertion_extract"] = TaskBinding(
        model="frontier", temperature=0.0, n=1,
    )
    entry = resolve_model(TaskType.ASSERTION_EXTRACT, routing=routing)
    assert entry.provider == "anthropic"
    assert entry.model == "claude-opus-4-7"


def test_assertion_map_routes_to_bound_model() -> None:
    """F19.5 stage B: SignalMap -> SignalDefinitions via ASSERTION_MAP.
    Independently bindable so the cheap structural-mapping task can
    route to a smaller model than the codegen-heavy translate stage."""
    routing = _routing()
    routing.tasks["assertion_map"] = TaskBinding(
        model="frontier", temperature=0.0, n=1,
    )
    entry = resolve_model(TaskType.ASSERTION_MAP, routing=routing)
    assert entry.provider == "anthropic"
    assert entry.model == "claude-opus-4-7"


def test_assertion_translate_routes_to_bound_model() -> None:
    """F19.5 stage C: SignalDefinitions -> Python source via
    ASSERTION_TRANSLATE. The codegen step typically wants frontier
    while extract/map can run on a smaller model."""
    routing = _routing()
    routing.tasks["assertion_translate"] = TaskBinding(
        model="frontier", temperature=0.2, n=1,
    )
    entry = resolve_model(TaskType.ASSERTION_TRANSLATE, routing=routing)
    assert entry.provider == "anthropic"
    assert entry.model == "claude-opus-4-7"


# --------------------------------------------------------------------------- #
# Repair tasks walk the loop bindings.
# --------------------------------------------------------------------------- #
def test_repair_with_syntactic_failure_uses_inner_loop() -> None:
    routing = _routing()
    entry = resolve_model(
        TaskType.RTL_REPAIR,
        failure_type=FailureType.SYNTACTIC,
        routing=routing,
    )
    assert entry.model == "qwen2.5-coder:7b"  # loops.inner = local-coder


def test_repair_with_semantic_failure_uses_outer_loop() -> None:
    routing = _routing()
    entry = resolve_model(
        TaskType.RTL_REPAIR,
        failure_type=FailureType.SEMANTIC,
        routing=routing,
    )
    assert entry.model == "claude-opus-4-7"  # loops.outer = frontier


def test_repair_with_no_failure_defaults_to_inner_loop() -> None:
    # A repair task with no explicit failure_type defaults to the inner loop.
    routing = _routing()
    entry = resolve_model(TaskType.RTL_REPAIR, routing=routing)
    assert entry.model == "qwen2.5-coder:7b"


# --------------------------------------------------------------------------- #
# Escalation bumps inner -> outer regardless of failure type.
# --------------------------------------------------------------------------- #
def test_escalation_bumps_inner_to_outer() -> None:
    routing = _routing()
    entry = resolve_model(
        TaskType.RTL_REPAIR,
        failure_type=FailureType.SYNTACTIC,
        escalation=EscalationLevel.OUTER,
        routing=routing,
    )
    assert entry.model == "claude-opus-4-7"  # escalation forced outer loop


def test_human_escalation_raises() -> None:
    routing = _routing()
    with pytest.raises(ResolutionError) as ei:
        resolve_model(
            TaskType.RTL_REPAIR, escalation=EscalationLevel.HUMAN, routing=routing,
        )
    assert "HUMAN" in str(ei.value)


# --------------------------------------------------------------------------- #
# F12.5 — EXHAUSTED routes RTL_REPAIR to whichever model is bound to
# ``routing.tasks.plan`` (by convention the frontier).
# --------------------------------------------------------------------------- #
def test_exhausted_escalation_routes_rtl_repair_to_plan_model() -> None:
    """A repair task at EXHAUSTED uses the plan task's binding — the
    F12.5 contract reuses the existing 'frontier for semantic reasoning'
    binding rather than introducing a new registry slot."""
    routing = _routing()  # default: plan -> "frontier"
    entry = resolve_model(
        TaskType.RTL_REPAIR,
        escalation=EscalationLevel.EXHAUSTED,
        routing=routing,
    )
    assert entry.provider == "anthropic"
    assert entry.model == "claude-opus-4-7"


def test_exhausted_escalation_overrides_inner_loop_binding() -> None:
    """Even when failure_type=SYNTACTIC (which normally routes inner),
    EXHAUSTED jumps straight to the plan-task binding."""
    routing = _routing()
    entry = resolve_model(
        TaskType.RTL_REPAIR,
        failure_type=FailureType.SYNTACTIC,
        escalation=EscalationLevel.EXHAUSTED,
        routing=routing,
    )
    assert entry.model == "claude-opus-4-7"


def test_exhausted_escalation_uses_plan_task_params() -> None:
    """params_for at EXHAUSTED returns the plan task's binding (its
    temperature + n)."""
    routing = _routing()
    binding = params_for(
        TaskType.RTL_REPAIR,
        escalation=EscalationLevel.EXHAUSTED,
        routing=routing,
    )
    assert binding.temperature == 0.2
    assert binding.n == 1


def test_exhausted_escalation_without_plan_binding_raises() -> None:
    """If routing has no plan binding, EXHAUSTED can't resolve — explicit
    error so the operator knows what to fix."""
    routing = _routing()
    del routing.tasks["plan"]
    with pytest.raises(ResolutionError) as ei:
        resolve_model(
            TaskType.RTL_REPAIR,
            escalation=EscalationLevel.EXHAUSTED,
            routing=routing,
        )
    assert "plan" in str(ei.value)


# --------------------------------------------------------------------------- #
# AC: policy is config-overridable.
# --------------------------------------------------------------------------- #
def test_rebinding_inner_loop_reroutes_syntactic_repair() -> None:
    # Same scenario, different config: inner loop now routes to a different
    # registry entry. The AC: policy is config-overridable.
    routing = _routing(inner="local-big")
    entry = resolve_model(
        TaskType.RTL_REPAIR,
        failure_type=FailureType.SYNTACTIC,
        routing=routing,
    )
    assert entry.model == "qwen2.5-coder:32b"


def test_rebinding_rtl_gen_reroutes_generation() -> None:
    routing = _routing(rtl_gen="frontier")
    entry = resolve_model(TaskType.RTL_GEN, routing=routing)
    assert entry.model == "claude-opus-4-7"


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #
def test_unbound_task_raises() -> None:
    routing = _routing()
    # No binding for TaskType.SCORE in this routing.
    with pytest.raises(ResolutionError) as ei:
        resolve_model(TaskType.SCORE, routing=routing)
    assert "score" in str(ei.value).lower()


def test_dangling_registry_reference_caught_by_settings() -> None:
    # The Settings validator already catches loop/task bindings that point at
    # a missing registry entry; verify resolve_model still raises cleanly if
    # someone bypasses validation and edits the model dict directly.
    routing = _routing()
    routing.loops["inner"].model = "ghost"  # nuke the validated binding
    with pytest.raises(ResolutionError) as ei:
        resolve_model(
            TaskType.RTL_REPAIR,
            failure_type=FailureType.SYNTACTIC,
            routing=routing,
        )
    assert "ghost" in str(ei.value)


# --------------------------------------------------------------------------- #
# params_for surfaces temperature / n the bindings declare.
# --------------------------------------------------------------------------- #
def test_params_for_returns_task_binding_for_generation() -> None:
    routing = _routing()
    binding = params_for(TaskType.RTL_GEN, routing=routing)
    assert binding.temperature == 0.8
    assert binding.n == 8


def test_params_for_returns_loop_binding_for_repair() -> None:
    routing = _routing()
    binding = params_for(
        TaskType.RTL_REPAIR,
        failure_type=FailureType.SYNTACTIC,
        routing=routing,
    )
    assert binding.temperature == 0.4
    assert binding.n == 3


# --------------------------------------------------------------------------- #
# F11.5 — pin the CLAUDE.md hybrid policy: local for high-volume / inner;
# frontier for planning / semantic diagnosis / outer.
# --------------------------------------------------------------------------- #
def _hybrid_routing() -> RoutingSettings:
    """A routing config shaped like ``configs/demo-ollama.yaml``."""
    return RoutingSettings(
        registry={
            "frontier": ModelEntry(
                provider="anthropic", model="claude-sonnet-4-6",
            ),
            "local-coder": ModelEntry(
                provider="ollama", model="qwen3-coder:latest",
                endpoint="http://localhost:11434",
            ),
        },
        loops={
            "inner": LoopBinding(model="local-coder", temperature=0.4, n=1),
            "outer": LoopBinding(model="frontier", temperature=0.0, n=1),
        },
        tasks={
            "spec_intake": TaskBinding(model="frontier", temperature=0.0, n=1),
            "plan": TaskBinding(model="frontier", temperature=0.0, n=1),
            "rtl_gen": TaskBinding(model="local-coder", temperature=0.4, n=1),
            "rtl_repair": TaskBinding(model="local-coder", temperature=0.4, n=1),
            "tb_gen": TaskBinding(model="local-coder", temperature=0.2, n=1),
            "diagnose": TaskBinding(model="frontier", temperature=0.0, n=1),
        },
        fallback={"local-coder": "frontier"},
    )


@pytest.mark.parametrize(
    "task,expected_provider",
    [
        # High-volume code generation -> local coder.
        (TaskType.RTL_GEN, "ollama"),
        (TaskType.TB_GEN, "ollama"),
        # Planning / NL reasoning / semantic diagnosis -> frontier.
        (TaskType.SPEC_INTAKE, "anthropic"),
        (TaskType.PLAN, "anthropic"),
        (TaskType.DIAGNOSE, "anthropic"),
    ],
)
def test_hybrid_policy_pins_task_to_expected_backend(
    task: TaskType, expected_provider: str,
) -> None:
    """Each generation TaskType lands on the CLAUDE.md-prescribed backend."""
    routing = _hybrid_routing()
    entry = resolve_model(task, routing=routing)
    assert entry.provider == expected_provider, (
        f"task {task.value!r} expected {expected_provider!r}, "
        f"got {entry.provider!r}"
    )


def test_hybrid_policy_rtl_repair_inner_routes_local() -> None:
    """Syntactic RTL_REPAIR walks the inner loop -> local-coder."""
    routing = _hybrid_routing()
    entry = resolve_model(
        TaskType.RTL_REPAIR,
        failure_type=FailureType.SYNTACTIC,
        routing=routing,
    )
    assert entry.provider == "ollama"


def test_hybrid_policy_rtl_repair_outer_routes_frontier() -> None:
    """Semantic RTL_REPAIR walks the outer loop -> frontier."""
    routing = _hybrid_routing()
    entry = resolve_model(
        TaskType.RTL_REPAIR,
        failure_type=FailureType.SEMANTIC,
        routing=routing,
    )
    assert entry.provider == "anthropic"


def test_hybrid_policy_includes_fallback_pointing_local_to_frontier() -> None:
    """Hybrid configs publish a ``local-coder -> frontier`` fallback so an
    unreachable Ollama daemon doesn't tank the run."""
    routing = _hybrid_routing()
    assert routing.fallback.get("local-coder") == "frontier"
