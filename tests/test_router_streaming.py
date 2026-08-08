"""F11.2 acceptance: LiteLLMRouter.stream routes via the same policy as generate.

The AC: ``router.stream(TaskType.RTL_REPAIR, ...)`` resolves to the inner
loop's model + temperature (just like :meth:`generate`); the terminal
:class:`StreamChunk` carries a :class:`ModelInvocation` that pins the
resolved provider / model / temperature.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from chip_agent.design_state import (
    EscalationLevel,
    FailureDiagnosis,
    Provenance,
    Stage,
    TaskType,
)
from chip_agent.routing.gateway import (
    CompletionResult,
    CompletionStreamChunk,
    LiteLLMGateway,
)
from chip_agent.routing.router import LiteLLMRouter
from chip_agent.settings import LoopBinding, ModelEntry, RoutingSettings, TaskBinding


def _routing() -> RoutingSettings:
    return RoutingSettings(
        registry={
            "local-coder": ModelEntry(
                provider="ollama", model="qwen2.5-coder:7b",
                endpoint="http://localhost:11434",
            ),
            "frontier": ModelEntry(provider="anthropic", model="claude-opus-4-7"),
        },
        loops={
            "inner": LoopBinding(model="local-coder", temperature=0.4, n=3),
            "outer": LoopBinding(model="frontier", temperature=0.2, n=1),
        },
        tasks={
            "rtl_gen": TaskBinding(model="local-coder", temperature=0.8, n=4),
            "diagnose": TaskBinding(model="frontier", temperature=0.2, n=1),
            "plan": TaskBinding(model="frontier", temperature=0.1, n=1),
        },
    )


@dataclass
class _StreamingStubBackend:
    """Yields a canned list of :class:`CompletionStreamChunk`s; records each call."""

    chunks: list[CompletionStreamChunk]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> CompletionResult:
        raise NotImplementedError

    def stream(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        self.calls.append({
            "model": model, "messages": list(messages),
            "temperature": temperature, "seed": seed, "api_base": api_base,
        })
        yield from self.chunks


def _router(
    chunks: list[CompletionStreamChunk] | None = None,
    *,
    routing: RoutingSettings | None = None,
) -> tuple[LiteLLMRouter, _StreamingStubBackend]:
    backend = _StreamingStubBackend(chunks=chunks or [
        CompletionStreamChunk(delta="ok",
                              finish_reason="stop",
                              prompt_tokens=10, completion_tokens=5,
                              cost_usd=0.0),
    ])
    gw = LiteLLMGateway(backend=backend)
    return LiteLLMRouter(routing=routing or _routing(), gateway=gw), backend


# --------------------------------------------------------------------------- #
# AC: stream routes through the same inner loop binding as generate.
# --------------------------------------------------------------------------- #
def test_router_stream_routes_through_loop_binding() -> None:
    """RTL_REPAIR (syntactic) -> inner loop -> local-coder, temp=0.4."""
    router, backend = _router()
    out = list(router.stream(TaskType.RTL_REPAIR, context={"prompt": "fix lint"}))
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["model"] == "ollama/qwen2.5-coder:7b"
    assert call["api_base"] == "http://localhost:11434"
    assert call["temperature"] == 0.4
    # Provenance on the terminal chunk matches the resolved entry.
    final = out[-1]
    assert final.invocation is not None
    assert final.invocation.provider == "ollama"
    assert final.invocation.model == "qwen2.5-coder:7b"
    assert final.invocation.temperature == 0.4


def test_router_stream_routes_through_task_binding() -> None:
    """RTL_GEN -> task binding -> local-coder, temp=0.8 (from tasks.rtl_gen)."""
    router, backend = _router()
    list(router.stream(TaskType.RTL_GEN, context={"prompt": "module counter;"}))
    assert backend.calls[0]["temperature"] == 0.8


def test_router_stream_diagnose_routes_frontier() -> None:
    router, backend = _router()
    out = list(router.stream(TaskType.DIAGNOSE, context={"prompt": "what failed?"}))
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"
    assert backend.calls[0]["api_base"] is None
    assert backend.calls[0]["temperature"] == 0.2
    final = out[-1]
    assert final.invocation is not None
    assert final.invocation.provider == "anthropic"


# --------------------------------------------------------------------------- #
# AC: semantic repair (FailureDiagnosis provided) goes outer-loop (frontier).
# --------------------------------------------------------------------------- #
def test_router_stream_semantic_repair_goes_outer_loop() -> None:
    router, backend = _router()
    fd = FailureDiagnosis(
        artifact_id="d0.counter.diag",
        design_id="d0",
        nl_summary="ack low at cycle 5; expected high",
        provenance=Provenance(produced_by=Stage.RTL),
    )
    list(router.stream(
        TaskType.RTL_REPAIR,
        context={"prompt": "fix this"},
        failure=fd,
    ))
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"
    assert backend.calls[0]["temperature"] == 0.2


def test_router_stream_explicit_escalation_to_outer() -> None:
    router, backend = _router()
    list(router.stream(
        TaskType.RTL_REPAIR,
        context={"prompt": "fix"},
        escalation=EscalationLevel.OUTER,
    ))
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"


# --------------------------------------------------------------------------- #
# Plumbing: system + seed flow through to the backend identically to generate().
# --------------------------------------------------------------------------- #
def test_router_stream_passes_system_and_seed() -> None:
    router, backend = _router()
    list(router.stream(
        TaskType.RTL_GEN,
        context={"prompt": "u", "system": "s", "seed": 99},
    ))
    msgs = backend.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "s"}
    assert msgs[1] == {"role": "user", "content": "u"}
    assert backend.calls[0]["seed"] == 99


# --------------------------------------------------------------------------- #
# Multi-chunk stream yields multiple deltas + one terminal invocation.
# --------------------------------------------------------------------------- #
def test_router_stream_yields_each_backend_delta() -> None:
    router, _backend = _router(chunks=[
        CompletionStreamChunk(delta="A "),
        CompletionStreamChunk(delta="B "),
        CompletionStreamChunk(delta="C",
                              finish_reason="stop",
                              prompt_tokens=12, completion_tokens=3,
                              cost_usd=0.0),
    ])
    out = list(router.stream(TaskType.RTL_GEN, context={"prompt": "p"}))
    assert [c.delta for c in out] == ["A ", "B ", "C"]
    assert out[0].invocation is None
    assert out[1].invocation is None
    final = out[-1]
    assert final.finish_reason == "stop"
    assert final.invocation is not None
    assert final.invocation.prompt_tokens == 12
    assert final.invocation.completion_tokens == 3


# --------------------------------------------------------------------------- #
# Round-trip: stream's aggregated text matches what generate() would return
# for the same conceptual response. (Sanity: both paths share routing.)
# --------------------------------------------------------------------------- #
@dataclass
class _DualBackend:
    """Same canned response surfaced by both ``complete`` and ``stream``."""

    text: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> CompletionResult:
        self.calls.append({"mode": "complete", "model": model,
                           "temperature": temperature})
        return CompletionResult(self.text, 12, 6, 0.0)

    def stream(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        self.calls.append({"mode": "stream", "model": model,
                           "temperature": temperature})
        # Pretend the provider chunked the text into thirds.
        n = len(self.text)
        third = max(1, n // 3)
        yield CompletionStreamChunk(delta=self.text[:third])
        yield CompletionStreamChunk(delta=self.text[third:third * 2])
        yield CompletionStreamChunk(
            delta=self.text[third * 2:],
            finish_reason="stop",
            prompt_tokens=12, completion_tokens=6, cost_usd=0.0,
        )


def test_stream_and_generate_resolve_same_model() -> None:
    backend = _DualBackend(text="hello world")
    gw = LiteLLMGateway(backend=backend)
    router = LiteLLMRouter(routing=_routing(), gateway=gw)

    gen = router.generate(TaskType.PLAN, context={"prompt": "plan it"})
    streamed = "".join(
        c.delta for c in router.stream(TaskType.PLAN, context={"prompt": "plan it"})
    )

    assert gen.chosen == streamed == "hello world"
    assert backend.calls[0]["mode"] == "complete"
    assert backend.calls[1]["mode"] == "stream"
    # Same routing decision both times.
    assert backend.calls[0]["model"] == backend.calls[1]["model"]
    assert backend.calls[0]["temperature"] == backend.calls[1]["temperature"]


# --------------------------------------------------------------------------- #
# Regression: existing generate() path unchanged.
# --------------------------------------------------------------------------- #
def test_generate_still_works_when_backend_supports_streaming() -> None:
    """Adding `stream` to a backend doesn't break `generate`."""
    backend = _DualBackend(text="hi")
    gw = LiteLLMGateway(backend=backend)
    router = LiteLLMRouter(routing=_routing(), gateway=gw)
    result = router.generate(TaskType.PLAN, context={"prompt": "p"})
    assert result.chosen == "hi"
    assert result.invocation.provider == "anthropic"


def test_router_stream_n_greater_than_one_irrelevant_for_streams() -> None:
    """Even if a binding declares ``n>1``, the stream produces a single sequence.

    Multi-candidate sampling has no meaning for a token-by-token stream:
    interactive output is one stream, not N. The router must still call the
    backend exactly once and yield exactly the chunks it gave back.
    """
    router, backend = _router(chunks=[
        CompletionStreamChunk(delta="a"),
        CompletionStreamChunk(delta="b",
                              finish_reason="stop",
                              prompt_tokens=4, completion_tokens=2,
                              cost_usd=0.0),
    ])
    # RTL_GEN's task binding declares n=4, but stream stays single-stream.
    out = list(router.stream(TaskType.RTL_GEN, context={"prompt": "p"}))
    assert len(backend.calls) == 1
    assert [c.delta for c in out] == ["a", "b"]


# --------------------------------------------------------------------------- #
# Pytest collection sanity: avoids "test file with no tests" miss.
# --------------------------------------------------------------------------- #
def test_module_level_pytest_import_is_in_scope() -> None:
    assert pytest is not None
