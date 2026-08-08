"""F3.1 acceptance: LiteLLMGateway over a stub backend.

The AC: both an Ollama-shaped and a frontier-shaped registry entry are
callable through one ``.call`` interface, and every call produces a
ModelInvocation carrying tokens / cost / seed / temperature.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from chip_agent.routing.gateway import (
    CompletionResult,
    CompletionStreamChunk,
    GatewayError,
    LiteLLMGateway,
)
from chip_agent.settings import ModelEntry


@dataclass
class StubBackend:
    """Captures the call args; returns a canned result (or raises if so seeded)."""

    result: CompletionResult | Exception = field(
        default_factory=lambda: CompletionResult("ok", 12, 5, 0.0)
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        seed: int | None,
        api_base: str | None,
    ) -> CompletionResult:
        self.calls.append({
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "seed": seed,
            "api_base": api_base,
        })
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        seed: int | None,
        api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# AC: both backends callable through one interface
# --------------------------------------------------------------------------- #
def test_ollama_entry_routes_with_api_base() -> None:
    backend = StubBackend(result=CompletionResult("ok", 12, 5, 0.0))
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(
        provider="ollama",
        model="qwen2.5-coder:7b",
        endpoint="http://localhost:11434",
    )
    text, inv = gw.call(entry, prompt="hi", temperature=0.4, seed=42)
    assert text == "ok"
    call = backend.calls[0]
    assert call["model"] == "ollama/qwen2.5-coder:7b"
    assert call["api_base"] == "http://localhost:11434"
    assert call["temperature"] == 0.4
    assert call["seed"] == 42
    # AC: invocation records tokens / cost / seed / temperature.
    assert inv.provider == "ollama"
    assert inv.model == "qwen2.5-coder:7b"
    assert inv.temperature == 0.4
    assert inv.seed == 42
    assert inv.prompt_tokens == 12
    assert inv.completion_tokens == 5
    assert inv.cost_usd == 0.0


def test_anthropic_entry_omits_api_base() -> None:
    backend = StubBackend(result=CompletionResult("hello", 100, 50, 0.0015))
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="claude-opus-4-7")
    text, inv = gw.call(entry, prompt="hi")
    assert text == "hello"
    call = backend.calls[0]
    assert call["model"] == "anthropic/claude-opus-4-7"
    assert call["api_base"] is None
    assert inv.cost_usd == 0.0015
    assert inv.provider == "anthropic"


# --------------------------------------------------------------------------- #
# Message construction
# --------------------------------------------------------------------------- #
def test_system_prompt_is_prepended() -> None:
    backend = StubBackend()
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="claude-opus-4-7")
    gw.call(entry, prompt="user-text", system="you are helpful")
    messages = backend.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": "you are helpful"}
    assert messages[1] == {"role": "user", "content": "user-text"}


def test_no_system_prompt_means_just_user() -> None:
    backend = StubBackend()
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="claude-opus-4-7")
    gw.call(entry, prompt="hi")
    messages = backend.calls[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_defaults_temperature_and_seed() -> None:
    backend = StubBackend()
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="m")
    text, inv = gw.call(entry, prompt="p")
    assert text == "ok"
    assert inv.temperature == 0.2
    assert inv.seed is None
    # When seed is None we should NOT have sent a seed key to the backend
    # (so providers that reject `seed=None` don't choke). The stub captures
    # whatever the gateway sent.
    assert backend.calls[0]["seed"] is None


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_backend_error_propagates_as_gateway_error() -> None:
    backend = StubBackend(result=GatewayError("oops"))
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="x", model="y")
    with pytest.raises(GatewayError):
        gw.call(entry, prompt="p")


# --------------------------------------------------------------------------- #
# Missing usage fields don't crash — ModelInvocation tolerates None.
# --------------------------------------------------------------------------- #
def test_missing_usage_fields_yield_none_in_invocation() -> None:
    backend = StubBackend(
        result=CompletionResult("ok", prompt_tokens=None,
                                completion_tokens=None, cost_usd=None)
    )
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="m")
    _, inv = gw.call(entry, prompt="p")
    assert inv.prompt_tokens is None
    assert inv.completion_tokens is None
    assert inv.cost_usd is None


# --------------------------------------------------------------------------- #
# Settings integration smoke
# --------------------------------------------------------------------------- #
def test_routes_a_settings_registry_entry() -> None:
    """The settings registry feeds the gateway directly — no glue needed."""
    from chip_agent.settings import RoutingSettings
    routing = RoutingSettings(
        registry={
            "local-coder": ModelEntry(
                provider="ollama",
                model="qwen2.5-coder:7b",
                endpoint="http://localhost:11434",
            ),
            "frontier": ModelEntry(provider="anthropic", model="claude-opus-4-7"),
        },
    )
    backend = StubBackend()
    gw = LiteLLMGateway(backend=backend)
    gw.call(routing.registry["local-coder"], prompt="x")
    gw.call(routing.registry["frontier"], prompt="y")
    assert backend.calls[0]["model"] == "ollama/qwen2.5-coder:7b"
    assert backend.calls[0]["api_base"] == "http://localhost:11434"
    assert backend.calls[1]["model"] == "anthropic/claude-opus-4-7"
    assert backend.calls[1]["api_base"] is None


# --------------------------------------------------------------------------- #
# F22.2-B: every gateway call opens one MODEL span on the injected tracer.
# These tests don't drive the JSONL — they verify the tracer hook fires
# with the right attributes, leaving the JSONL contract to
# tests/test_jsonl_tracer.py.
# --------------------------------------------------------------------------- #
def test_call_opens_one_model_span_with_full_attributes() -> None:
    """Each ``gw.call`` opens exactly one ``MODEL`` span carrying
    provider, model_id, temperature, seed, prompt, system,
    completion, plus the existing tokens/cost setters."""
    from chip_agent.obs.tracing import InMemoryTracer, SpanKind
    tracer = InMemoryTracer()
    backend = StubBackend(result=CompletionResult("response text", 100, 50, 0.012))
    gw = LiteLLMGateway(backend=backend, tracer=tracer)
    entry = ModelEntry(provider="anthropic", model="claude-opus-4-7")
    with tracer.run("d"):
        gw.call(
            entry, prompt="describe the module",
            system="You are an RTL author.", temperature=0.7, seed=42,
        )
    model_spans = [s for s in tracer.spans if s.kind is SpanKind.MODEL]
    assert len(model_spans) == 1
    span = model_spans[0]
    assert span.name == "model:claude-opus-4-7"
    assert span.attributes["provider"] == "anthropic"
    assert span.attributes["model_id"] == "claude-opus-4-7"
    assert span.attributes["temperature"] == 0.7
    assert span.attributes["seed"] == 42
    assert span.attributes["prompt"] == "describe the module"
    assert span.attributes["system"] == "You are an RTL author."
    assert span.attributes["completion"] == "response text"
    assert span.prompt_tokens == 100
    assert span.completion_tokens == 50
    assert span.cost_usd == 0.012


def test_call_with_no_system_prompt_records_empty_system_attribute() -> None:
    from chip_agent.obs.tracing import InMemoryTracer, SpanKind
    tracer = InMemoryTracer()
    gw = LiteLLMGateway(backend=StubBackend(), tracer=tracer)
    entry = ModelEntry(provider="ollama", model="qwen")
    with tracer.run("d"):
        gw.call(entry, prompt="hi")
    span = next(s for s in tracer.spans if s.kind is SpanKind.MODEL)
    assert span.attributes["system"] == ""


def test_call_missing_token_counts_skips_token_setters() -> None:
    """A backend that returns ``prompt_tokens=None`` should leave the
    span's token counters as ``None`` rather than crashing the setter."""
    from chip_agent.obs.tracing import InMemoryTracer, SpanKind
    tracer = InMemoryTracer()
    backend = StubBackend(
        result=CompletionResult("ok", None, None, None),
    )
    gw = LiteLLMGateway(backend=backend, tracer=tracer)
    entry = ModelEntry(provider="anthropic", model="claude")
    with tracer.run("d"):
        gw.call(entry, prompt="x")
    span = next(s for s in tracer.spans if s.kind is SpanKind.MODEL)
    assert span.prompt_tokens is None
    assert span.completion_tokens is None
    assert span.cost_usd is None


def test_call_backend_error_still_closes_model_span_as_error() -> None:
    """The InMemoryTracer's span context records ``status=error`` when
    the wrapped backend raises. Without this, an exception would leak
    an open span in the tracer stack."""
    from chip_agent.obs.tracing import InMemoryTracer, SpanKind, SpanStatus
    tracer = InMemoryTracer()
    backend = StubBackend(result=GatewayError("backend down"))
    gw = LiteLLMGateway(backend=backend, tracer=tracer)
    entry = ModelEntry(provider="anthropic", model="claude")
    with tracer.run("d"), pytest.raises(GatewayError):
        gw.call(entry, prompt="x")
    span = next(s for s in tracer.spans if s.kind is SpanKind.MODEL)
    assert span.status is SpanStatus.ERROR


def test_default_tracer_is_noop_so_existing_callers_stay_silent() -> None:
    """Backward compat: a ``LiteLLMGateway()`` without an explicit
    tracer behaves identically to pre-F22.2 — no spans recorded
    anywhere visible. The wrapped NoopTracer's ``spans`` list is
    always empty."""
    gw = LiteLLMGateway(backend=StubBackend())
    entry = ModelEntry(provider="ollama", model="qwen")
    # No explicit ``tracer.run`` context — confirms NoopTracer doesn't
    # need one (it never opens real spans).
    text, _ = gw.call(entry, prompt="hi")
    assert text == "ok"
    assert gw._tracer.spans == []  # NoopTracer's always-empty list


def test_model_span_nests_under_enclosing_stage_span() -> None:
    """Parent pointer: a MODEL span opened inside a STAGE context
    points at the STAGE span. This is what build_trace_tree (and the
    future TUI ReasoningTape's stage-grouping) reads."""
    from chip_agent.obs.tracing import InMemoryTracer, SpanKind
    tracer = InMemoryTracer()
    gw = LiteLLMGateway(backend=StubBackend(), tracer=tracer)
    entry = ModelEntry(provider="anthropic", model="claude")
    with tracer.run("d"), tracer.span("rtl", kind=SpanKind.STAGE) as stage:
        gw.call(entry, prompt="x")
    model_span = next(s for s in tracer.spans if s.kind is SpanKind.MODEL)
    assert model_span.parent_id == stage.id
