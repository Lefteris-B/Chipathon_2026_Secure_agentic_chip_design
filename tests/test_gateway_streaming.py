"""F11.2 acceptance: LiteLLMGateway streaming over a stub backend.

The AC: a stream yields one chunk per backend delta; the terminal chunk
(``finish_reason != None``) carries a finalised :class:`ModelInvocation`
mirroring what :meth:`LiteLLMGateway.call` returns alongside its text.
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
    StreamChunk,
)
from chip_agent.settings import ModelEntry


@dataclass
class _StreamingStubBackend:
    """Yields a canned list of :class:`CompletionStreamChunk`s on ``stream``."""

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


@dataclass
class _NonStreamingStubBackend:
    """A backend without a ``stream`` method — gateway should raise."""

    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> CompletionResult:
        return CompletionResult("ok")

    def stream(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# AC: every backend delta becomes a gateway StreamChunk.
# --------------------------------------------------------------------------- #
def test_stream_yields_multiple_chunks() -> None:
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="hel"),
        CompletionStreamChunk(delta="lo "),
        CompletionStreamChunk(
            delta="world",
            finish_reason="stop",
            prompt_tokens=12,
            completion_tokens=8,
            cost_usd=0.0,
        ),
    ])
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="ollama", model="qwen2.5-coder:7b",
                       endpoint="http://localhost:11434")
    out = list(gw.stream(entry, prompt="hi"))
    assert len(out) == 3
    assert [c.delta for c in out] == ["hel", "lo ", "world"]
    # Mid-stream chunks have no finish_reason and no invocation.
    assert out[0].finish_reason is None
    assert out[0].invocation is None
    assert out[1].finish_reason is None
    assert out[1].invocation is None


# --------------------------------------------------------------------------- #
# AC: the terminal chunk carries the finalised ModelInvocation.
# --------------------------------------------------------------------------- #
def test_stream_records_invocation_after_final_chunk() -> None:
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="a"),
        CompletionStreamChunk(delta="b"),
        CompletionStreamChunk(
            delta="",
            finish_reason="stop",
            prompt_tokens=42,
            completion_tokens=17,
            cost_usd=0.0042,
        ),
    ])
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="claude-sonnet-4-6")
    out = list(gw.stream(entry, prompt="hi", temperature=0.3, seed=7))
    final = out[-1]
    assert final.finish_reason == "stop"
    assert final.invocation is not None
    inv = final.invocation
    assert inv.provider == "anthropic"
    assert inv.model == "claude-sonnet-4-6"
    assert inv.temperature == 0.3
    assert inv.seed == 7
    assert inv.prompt_tokens == 42
    assert inv.completion_tokens == 17
    assert inv.cost_usd == 0.0042


def test_stream_invocation_tolerates_missing_usage_fields() -> None:
    # Providers like some Ollama versions never emit usage. The terminal
    # invocation should still be present with the token fields as None.
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="hi"),
        CompletionStreamChunk(delta="", finish_reason="stop"),
    ])
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="ollama", model="qwen2.5-coder:7b",
                       endpoint="http://localhost:11434")
    out = list(gw.stream(entry, prompt="hi"))
    assert out[-1].invocation is not None
    assert out[-1].invocation.prompt_tokens is None
    assert out[-1].invocation.completion_tokens is None
    assert out[-1].invocation.cost_usd is None


# --------------------------------------------------------------------------- #
# Message construction mirrors gw.call() so callers can swap freely.
# --------------------------------------------------------------------------- #
def test_stream_message_construction_matches_call() -> None:
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="ok", finish_reason="stop"),
    ])
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="claude-opus-4-7")
    list(gw.stream(entry, prompt="user-text", system="be helpful"))
    msgs = backend.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "be helpful"}
    assert msgs[1] == {"role": "user", "content": "user-text"}
    assert backend.calls[0]["model"] == "anthropic/claude-opus-4-7"
    assert backend.calls[0]["api_base"] is None


def test_stream_passes_api_base_for_ollama_entries() -> None:
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="x", finish_reason="stop"),
    ])
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="ollama", model="qwen2.5-coder:7b",
                       endpoint="http://localhost:11434")
    list(gw.stream(entry, prompt="hi"))
    assert backend.calls[0]["api_base"] == "http://localhost:11434"
    assert backend.calls[0]["model"] == "ollama/qwen2.5-coder:7b"


# --------------------------------------------------------------------------- #
# Aggregated text from a stream matches what a non-streaming call would say.
# --------------------------------------------------------------------------- #
def test_aggregated_stream_text_matches_call_text() -> None:
    # The plan calls out: "non-streaming generate() calls are byte-identical
    # to today" — we verify the inverse here: aggregating a stream over the
    # same conceptual response yields the same final text.
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="Hello, "),
        CompletionStreamChunk(delta="world"),
        CompletionStreamChunk(delta="!", finish_reason="stop"),
    ])
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="m")
    out = list(gw.stream(entry, prompt="say hi"))
    assert "".join(c.delta for c in out) == "Hello, world!"


# --------------------------------------------------------------------------- #
# Result type — produced items are typed StreamChunks, not raw dicts.
# --------------------------------------------------------------------------- #
def test_stream_yields_typed_chunks() -> None:
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="x", finish_reason="stop"),
    ])
    gw = LiteLLMGateway(backend=backend)
    entry = ModelEntry(provider="anthropic", model="m")
    for chunk in gw.stream(entry, prompt="hi"):
        assert isinstance(chunk, StreamChunk)


# --------------------------------------------------------------------------- #
# Non-streaming backend: stream() reports a clear GatewayError.
# --------------------------------------------------------------------------- #
def test_non_streaming_backend_raises_gateway_error() -> None:
    gw = LiteLLMGateway(backend=_NonStreamingStubBackend())
    entry = ModelEntry(provider="anthropic", model="m")
    with pytest.raises(GatewayError, match="does not support stream"):
        list(gw.stream(entry, prompt="hi"))


# --------------------------------------------------------------------------- #
# F22.2-B: streaming opens one MODEL span for the full stream lifetime,
# accumulates deltas into the ``completion`` attribute, sets tokens/cost
# from the terminal chunk's usage block. The span closes when the
# generator is exhausted (or closed early).
# --------------------------------------------------------------------------- #
def test_stream_opens_single_model_span_for_whole_stream() -> None:
    from chip_agent.obs.tracing import InMemoryTracer, SpanKind
    tracer = InMemoryTracer()
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="hel"),
        CompletionStreamChunk(delta="lo "),
        CompletionStreamChunk(
            delta="world", finish_reason="stop",
            prompt_tokens=12, completion_tokens=8, cost_usd=0.0,
        ),
    ])
    gw = LiteLLMGateway(backend=backend, tracer=tracer)
    entry = ModelEntry(provider="anthropic", model="claude")
    with tracer.run("d"):
        list(gw.stream(entry, prompt="hi", temperature=0.5, seed=7))
    model_spans = [s for s in tracer.spans if s.kind is SpanKind.MODEL]
    assert len(model_spans) == 1
    span = model_spans[0]
    assert span.attributes["streamed"] is True
    assert span.attributes["completion"] == "hello world"
    assert span.attributes["prompt"] == "hi"
    assert span.attributes["temperature"] == 0.5
    assert span.attributes["seed"] == 7
    assert span.prompt_tokens == 12
    assert span.completion_tokens == 8
    assert span.cost_usd == 0.0


def test_stream_closed_early_still_closes_span() -> None:
    """If the consumer breaks out of the iteration after the first
    chunk, generator cleanup still runs the span's ``__exit__``."""
    from chip_agent.obs.tracing import InMemoryTracer, SpanKind
    tracer = InMemoryTracer()
    backend = _StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="part1"),
        CompletionStreamChunk(delta="part2"),
        CompletionStreamChunk(delta="end", finish_reason="stop"),
    ])
    gw = LiteLLMGateway(backend=backend, tracer=tracer)
    entry = ModelEntry(provider="anthropic", model="claude")
    with tracer.run("d"):
        gen = gw.stream(entry, prompt="hi")
        first = next(gen)
        assert first.delta == "part1"
        gen.close()  # early termination
    model_spans = [s for s in tracer.spans if s.kind is SpanKind.MODEL]
    assert len(model_spans) == 1
    # Partial completion captured ("part1" — the only delta seen).
    assert model_spans[0].attributes["completion"] == "part1"


def test_stream_default_tracer_is_noop() -> None:
    gw = LiteLLMGateway(backend=_StreamingStubBackend(chunks=[
        CompletionStreamChunk(delta="x", finish_reason="stop"),
    ]))
    entry = ModelEntry(provider="anthropic", model="m")
    chunks = list(gw.stream(entry, prompt="hi"))
    assert len(chunks) == 1
    assert gw._tracer.spans == []  # NoopTracer's always-empty list
