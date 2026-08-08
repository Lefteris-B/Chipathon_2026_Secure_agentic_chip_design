"""LiteLLM-backed model gateway (F3.1, F11.2).

A single, typed seam between a :class:`~chip_agent.settings.ModelEntry`
(provider + model + optional endpoint) and a completion call. Records a
:class:`~chip_agent.design_state.ModelInvocation` so every model call lands
in artifact provenance with tokens / cost / seed / temperature.

LiteLLM's exact API surface is abstracted behind :class:`CompletionBackend`
so tests stay free of network calls. F11.2 adds a streaming sibling
(:meth:`LiteLLMGateway.stream`) for chat-style flows that need token-level
output; the non-streaming :meth:`LiteLLMGateway.call` path is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from chip_agent.design_state import ModelInvocation
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.settings import ModelEntry

__all__ = [
    "BackendUnreachableError",
    "CompletionBackend",
    "CompletionResult",
    "CompletionStreamChunk",
    "GatewayError",
    "LiteLLMBackend",
    "LiteLLMGateway",
    "StreamChunk",
]


class GatewayError(RuntimeError):
    """The backend failed or returned a malformed response."""


class BackendUnreachableError(GatewayError):
    """The backend was unreachable — DNS / connect-timeout / refused / network.

    Raised distinctly from a generic :class:`GatewayError` so the router
    (F11.5) can decide to retry against a configured fallback model
    instead of propagating the error to the caller.
    """


# Class names of upstream exceptions we treat as "backend unreachable".
# We match by class name (not isinstance) so this stays free of a hard
# litellm import — providers raise their own subclasses but the names
# are stable across SDK versions.
_CONNECTION_EXCEPTION_NAMES: frozenset[str] = frozenset({
    "APIConnectionError",
    "Timeout",
    "ConnectError",
    "ConnectionError",
    "ConnectTimeout",
    "ConnectTimeoutError",
    "ReadTimeout",
    "ReadTimeoutError",
    "URLError",
    "RemoteDisconnected",
})


def _is_connection_error(e: BaseException) -> bool:
    """True if ``e`` (or any cause in its chain) looks like a connection failure.

    Recognises Python-stdlib ``ConnectionError`` / ``TimeoutError`` by type
    plus any provider-specific exception whose class name names a
    connection-flavoured concept (``APIConnectionError``, ``Timeout``, …)
    so we stay free of a hard ``litellm`` import.
    """
    cur: BaseException | None = e
    while cur is not None:
        if isinstance(cur, ConnectionError | TimeoutError):
            return True
        if type(cur).__name__ in _CONNECTION_EXCEPTION_NAMES:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


@dataclass(frozen=True)
class CompletionResult:
    """The pieces of a completion the gateway needs to assemble a ModelInvocation."""

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class CompletionStreamChunk:
    """One delta from a streaming backend call.

    ``finish_reason`` is ``None`` for mid-stream chunks and a string
    (e.g. ``"stop"``, ``"length"``) on the terminal chunk. Token / cost
    fields are typically populated only on the terminal chunk by providers
    that emit a usage block at the tail of the stream; mid-stream chunks
    leave them ``None``.
    """

    delta: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class StreamChunk:
    """Gateway-level streaming chunk surfaced to callers.

    Mid-stream chunks carry just the ``delta`` and ``finish_reason=None``.
    The terminal chunk (``finish_reason != None``) additionally carries
    the assembled :class:`ModelInvocation`, mirroring what
    :meth:`LiteLLMGateway.call` returns alongside its text.
    """

    delta: str
    finish_reason: str | None = None
    invocation: ModelInvocation | None = None


class CompletionBackend(Protocol):
    """The seam: anything that can turn a prompt into a CompletionResult.

    F11.2 adds an optional ``stream`` method. Backends that do not implement
    streaming raise :class:`GatewayError` from ``stream`` (or the gateway
    propagates the ``AttributeError`` as a ``GatewayError``).
    """

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        seed: int | None,
        api_base: str | None,
    ) -> CompletionResult: ...

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        seed: int | None,
        api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]: ...


class LiteLLMBackend:
    """Default :class:`CompletionBackend`: calls ``litellm.completion``."""

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        seed: int | None,
        api_base: str | None,
    ) -> CompletionResult:
        import litellm  # local import: no cost when only the stub backend is used

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if seed is not None:
            kwargs["seed"] = seed
        if api_base is not None:
            kwargs["api_base"] = api_base

        try:
            resp = litellm.completion(**kwargs)
        except Exception as e:
            if _is_connection_error(e):
                raise BackendUnreachableError(
                    f"litellm.completion({model!r}) unreachable: {e}",
                ) from e
            raise GatewayError(f"litellm.completion({model!r}) failed: {e}") from e

        try:
            text = resp.choices[0].message.content or ""
        except (IndexError, AttributeError, TypeError) as e:
            raise GatewayError(
                f"litellm response missing content for {model!r}: {resp!r}"
            ) from e

        usage = getattr(resp, "usage", None)
        return CompletionResult(
            text=text,
            prompt_tokens=_int_or_none(_field(usage, "prompt_tokens")),
            completion_tokens=_int_or_none(_field(usage, "completion_tokens")),
            cost_usd=_float_or_none(getattr(resp, "_response_cost", None)),
        )

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        seed: int | None,
        api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        """Yield :class:`CompletionStreamChunk` from ``litellm.completion(stream=True)``.

        Each upstream chunk has ``choices[0].delta.content`` (the new text
        fragment) and an optional ``finish_reason``. Some providers emit a
        ``usage`` block on the terminal chunk; when present, we surface its
        token counts on the matching :class:`CompletionStreamChunk`.
        """
        import litellm  # local import: keep import-time cost off stub-only tests

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if seed is not None:
            kwargs["seed"] = seed
        if api_base is not None:
            kwargs["api_base"] = api_base

        try:
            stream = litellm.completion(**kwargs)
        except Exception as e:
            if _is_connection_error(e):
                raise BackendUnreachableError(
                    f"litellm.completion({model!r}, stream=True) unreachable: {e}",
                ) from e
            raise GatewayError(
                f"litellm.completion({model!r}, stream=True) failed: {e}"
            ) from e

        for raw in stream:
            try:
                choice = raw.choices[0]
                delta_obj = choice.delta
                delta_text = getattr(delta_obj, "content", None) or ""
                finish_reason = getattr(choice, "finish_reason", None)
            except (IndexError, AttributeError, TypeError) as e:
                raise GatewayError(
                    f"litellm stream chunk malformed for {model!r}: {raw!r}"
                ) from e
            usage = getattr(raw, "usage", None)
            yield CompletionStreamChunk(
                delta=delta_text,
                finish_reason=finish_reason,
                prompt_tokens=_int_or_none(_field(usage, "prompt_tokens")),
                completion_tokens=_int_or_none(_field(usage, "completion_tokens")),
                cost_usd=_float_or_none(getattr(raw, "_response_cost", None)),
            )


class LiteLLMGateway:
    """Typed adapter from :class:`ModelEntry` + prompt to ``(text, ModelInvocation)``.

    F22.2: every call/stream is wrapped in a ``MODEL`` span on the
    injected :class:`Tracer` (default :class:`NoopTracer` for backward
    compatibility). When the production tracer is :class:`JsonlTracer`,
    each LLM call appends one line to ``runs/<design_id>/reasoning.jsonl``
    carrying provider/model/temperature/seed/prompt/completion/usage —
    the source-of-truth reasoning record for the run.
    """

    def __init__(
        self,
        *,
        backend: CompletionBackend | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._backend: CompletionBackend = backend or LiteLLMBackend()
        self._tracer: Tracer = tracer or NoopTracer()

    def call(
        self,
        entry: ModelEntry,
        *,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.2,
        seed: int | None = None,
    ) -> tuple[str, ModelInvocation]:
        """Run one completion. Returns the generated text and a provenance record.

        ``entry.endpoint`` (if set) becomes ``api_base`` so e.g. a local Ollama
        endpoint is reachable through the same call site as a frontier API.
        The model string is rendered as ``"<provider>/<model>"`` — LiteLLM's
        canonical routing form.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        model_str = f"{entry.provider}/{entry.model}"
        with self._tracer.span(
            name=f"model:{entry.model}", kind=SpanKind.MODEL,
        ) as span:
            span.set_attribute("provider", entry.provider)
            span.set_attribute("model_id", entry.model)
            span.set_attribute("temperature", temperature)
            span.set_attribute("seed", seed)
            span.set_attribute("prompt", prompt)
            span.set_attribute("system", system or "")
            result = self._backend.complete(
                model=model_str,
                messages=messages,
                temperature=temperature,
                seed=seed,
                api_base=entry.endpoint,
            )
            span.set_attribute("completion", result.text)
            if result.prompt_tokens is not None and result.completion_tokens is not None:
                span.set_tokens(
                    prompt=result.prompt_tokens,
                    completion=result.completion_tokens,
                )
            if result.cost_usd is not None:
                span.set_cost(result.cost_usd)
        invocation = ModelInvocation(
            provider=entry.provider,
            model=entry.model,
            temperature=temperature,
            seed=seed,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=result.cost_usd,
        )
        return result.text, invocation

    def stream(
        self,
        entry: ModelEntry,
        *,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.2,
        seed: int | None = None,
    ) -> Iterator[StreamChunk]:
        """Stream tokens from ``entry``. Yields one :class:`StreamChunk` per delta.

        The terminal chunk (``finish_reason != None``) carries a finalised
        :class:`ModelInvocation` aggregating any usage / cost info the backend
        emitted across the stream. Mid-stream chunks have ``invocation=None``
        and just carry the next text fragment.

        Backends that don't emit a usage block still produce a terminal
        chunk; its invocation simply has ``prompt_tokens=None`` /
        ``completion_tokens=None``.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        model_str = f"{entry.provider}/{entry.model}"
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        cost_usd: float | None = None
        # F22.2: open the MODEL span once and yield from inside its
        # ``with`` block. Generator cleanup (consumer exhaustion or
        # close()) runs the contextmanager's ``__exit__``, so the span
        # closes correctly even on early break.
        with self._tracer.span(
            name=f"model:{entry.model}", kind=SpanKind.MODEL,
        ) as span:
            span.set_attribute("provider", entry.provider)
            span.set_attribute("model_id", entry.model)
            span.set_attribute("temperature", temperature)
            span.set_attribute("seed", seed)
            span.set_attribute("prompt", prompt)
            span.set_attribute("system", system or "")
            span.set_attribute("streamed", True)
            try:
                backend_stream = self._backend.stream(
                    model=model_str,
                    messages=messages,
                    temperature=temperature,
                    seed=seed,
                    api_base=entry.endpoint,
                )
            except (AttributeError, NotImplementedError) as e:
                raise GatewayError(
                    f"backend {type(self._backend).__name__!r} does not support stream()"
                ) from e

            assembled: list[str] = []
            # try/finally so that an early ``gen.close()`` (GeneratorExit
            # raised mid-yield) still records the partial completion +
            # whatever usage tokens we'd already seen, instead of leaving
            # the span with no ``completion`` attribute.
            try:
                for chunk in backend_stream:
                    if chunk.prompt_tokens is not None:
                        prompt_tokens = chunk.prompt_tokens
                    if chunk.completion_tokens is not None:
                        completion_tokens = chunk.completion_tokens
                    if chunk.cost_usd is not None:
                        cost_usd = chunk.cost_usd
                    assembled.append(chunk.delta)
                    invocation: ModelInvocation | None = None
                    if chunk.finish_reason is not None:
                        invocation = ModelInvocation(
                            provider=entry.provider,
                            model=entry.model,
                            temperature=temperature,
                            seed=seed,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            cost_usd=cost_usd,
                        )
                    yield StreamChunk(
                        delta=chunk.delta,
                        finish_reason=chunk.finish_reason,
                        invocation=invocation,
                    )
            finally:
                span.set_attribute("completion", "".join(assembled))
                if prompt_tokens is not None and completion_tokens is not None:
                    span.set_tokens(prompt=prompt_tokens, completion=completion_tokens)
                if cost_usd is not None:
                    span.set_cost(cost_usd)


def _field(obj: object, key: str) -> object | None:
    """Read ``key`` from either an object attribute or a dict — LiteLLM uses both."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _int_or_none(x: object) -> int | None:
    return int(x) if isinstance(x, int | float) and x is not None else None


def _float_or_none(x: object) -> float | None:
    return float(x) if isinstance(x, int | float) and x is not None else None
