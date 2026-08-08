"""F3.2 acceptance: LiteLLMRouter composes policy + gateway."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from chip_agent.design_state import (
    EscalationLevel,
    FailureDiagnosis,
    Provenance,
    Stage,
    TaskType,
)
from chip_agent.routing import FailureType, LiteLLMGateway, LiteLLMRouter
from chip_agent.routing.gateway import CompletionResult, CompletionStreamChunk
from chip_agent.settings import LoopBinding, ModelEntry, RoutingSettings, TaskBinding


def _routing(*, inner: str = "local-coder", outer: str = "frontier") -> RoutingSettings:
    return RoutingSettings(
        registry={
            "local-coder": ModelEntry(
                provider="ollama", model="qwen2.5-coder:7b",
                endpoint="http://localhost:11434",
            ),
            "frontier": ModelEntry(provider="anthropic", model="claude-opus-4-7"),
        },
        loops={
            "inner": LoopBinding(model=inner, temperature=0.4, n=3),
            "outer": LoopBinding(model=outer, temperature=0.2, n=1),
        },
        tasks={
            "rtl_gen": TaskBinding(model="local-coder", temperature=0.8, n=8),
            "diagnose": TaskBinding(model="frontier", temperature=0.2, n=1),
            "plan": TaskBinding(model="frontier", temperature=0.2, n=1),
        },
    )


@dataclass
class StubBackend:
    text: str = "ok"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> CompletionResult:
        self.calls.append({
            "model": model, "messages": list(messages),
            "temperature": temperature, "seed": seed, "api_base": api_base,
        })
        return CompletionResult(self.text, 10, 5, 0.001)

    def stream(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        raise NotImplementedError


def _router(routing: RoutingSettings | None = None) -> tuple[LiteLLMRouter, StubBackend]:
    backend = StubBackend()
    gw = LiteLLMGateway(backend=backend)
    return LiteLLMRouter(routing=routing or _routing(), gateway=gw), backend


# --------------------------------------------------------------------------- #
# AC: RTL_GEN routes local
# --------------------------------------------------------------------------- #
def test_rtl_gen_routes_local_with_task_temperature() -> None:
    # n=1 keeps this test focused on routing; multi-candidate behaviour is
    # exercised in test_router_multicandidate.py.
    router, backend = _router()
    result = router.generate(
        TaskType.RTL_GEN, context={"prompt": "module counter;"}, n=1,
    )
    assert result.chosen == "ok"
    call = backend.calls[0]
    # Routed to local-coder via Ollama.
    assert call["model"] == "ollama/qwen2.5-coder:7b"
    assert call["api_base"] == "http://localhost:11434"
    # Task binding's temperature is used.
    assert call["temperature"] == 0.8
    # Single-candidate path: one call, one candidate.
    assert len(backend.calls) == 1
    assert len(result.candidates) == 1
    # The ModelInvocation flows through to GenerationResult.
    assert result.invocation.provider == "ollama"
    assert result.invocation.temperature == 0.8


# --------------------------------------------------------------------------- #
# AC: semantic diagnosis routes frontier
# --------------------------------------------------------------------------- #
def test_diagnose_routes_frontier() -> None:
    router, backend = _router()
    router.generate(TaskType.DIAGNOSE, context={"prompt": "what failed?"})
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"
    assert backend.calls[0]["api_base"] is None
    assert backend.calls[0]["temperature"] == 0.2


def test_semantic_repair_with_failure_routes_outer_loop() -> None:
    router, backend = _router()
    fd = FailureDiagnosis(
        artifact_id="d0.counter.diag",
        design_id="d0",
        nl_summary="ack low at cycle 5; expected high",
        provenance=Provenance(produced_by=Stage.RTL),
    )
    router.generate(
        TaskType.RTL_REPAIR,
        context={"prompt": "fix this"},
        failure=fd,
    )
    # FailureDiagnosis presence -> SEMANTIC -> outer loop -> frontier.
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"
    assert backend.calls[0]["temperature"] == 0.2


def test_syntactic_repair_no_failure_routes_inner_loop() -> None:
    router, backend = _router()
    router.generate(TaskType.RTL_REPAIR, context={"prompt": "fix lint"})
    assert backend.calls[0]["model"] == "ollama/qwen2.5-coder:7b"
    assert backend.calls[0]["temperature"] == 0.4


# --------------------------------------------------------------------------- #
# Escalation bumps the loop slot
# --------------------------------------------------------------------------- #
def test_escalation_bumps_inner_to_outer() -> None:
    router, backend = _router()
    router.generate(
        TaskType.RTL_REPAIR,
        context={"prompt": "fix"},
        escalation=EscalationLevel.OUTER,
    )
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"


# --------------------------------------------------------------------------- #
# Context plumbing: system + seed
# --------------------------------------------------------------------------- #
def test_context_passes_system_prompt_and_seed() -> None:
    router, backend = _router()
    router.generate(
        TaskType.RTL_GEN,
        context={"prompt": "u", "system": "s", "seed": 42},
    )
    messages = backend.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": "s"}
    assert messages[1] == {"role": "user", "content": "u"}
    assert backend.calls[0]["seed"] == 42


# --------------------------------------------------------------------------- #
# AC: policy is config-overridable. Same call -> different model.
# --------------------------------------------------------------------------- #
def test_rebinding_inner_loop_reroutes() -> None:
    router, backend = _router(_routing(inner="frontier"))
    router.generate(TaskType.RTL_REPAIR, context={"prompt": "x"})
    # Inner loop now points at frontier (the same syntactic repair flips).
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"


# --------------------------------------------------------------------------- #
# Explicit override via context["failure_type"]
# --------------------------------------------------------------------------- #
def test_explicit_failure_type_override() -> None:
    router, backend = _router()
    router.generate(
        TaskType.RTL_REPAIR,
        context={"prompt": "x", "failure_type": FailureType.SEMANTIC},
    )
    # Even though `failure` is None, explicit override sends us to the outer loop.
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"


def test_explicit_failure_type_string_override() -> None:
    router, backend = _router()
    router.generate(
        TaskType.RTL_REPAIR,
        context={"prompt": "x", "failure_type": "semantic"},
    )
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"
