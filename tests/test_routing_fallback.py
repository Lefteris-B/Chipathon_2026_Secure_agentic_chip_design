"""F11.5 acceptance: transparent backend fallback + ``BACKEND_FALLBACK`` audit.

The ACs from the M11 plan:

* an unreachable primary backend transparently fails over to the
  configured fallback; the result carries the fallback's provider;
* the demotion lands in the audit log as a ``BACKEND_FALLBACK`` event;
* when no fallback is configured for the primary, the
  ``BackendUnreachableError`` propagates.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.design_state import TaskType
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.routing.gateway import (
    BackendUnreachableError,
    CompletionResult,
    CompletionStreamChunk,
    LiteLLMGateway,
)
from chip_agent.routing.router import LiteLLMRouter
from chip_agent.settings import (
    LoopBinding,
    ModelEntry,
    RoutingSettings,
    TaskBinding,
)


# --------------------------------------------------------------------------- #
# A per-model dispatching stub: route by ``model`` string.
# --------------------------------------------------------------------------- #
@dataclass
class _PerModelBackend:
    """Dispatch ``complete`` / ``stream`` per the ``model`` arg.

    A handler may return text (success), raise ``BackendUnreachableError``
    (simulate refused connection), or raise any other exception.
    """

    handlers: dict[str, str | BaseException]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> CompletionResult:
        self.calls.append({
            "mode": "complete", "model": model,
            "temperature": temperature, "seed": seed,
            "api_base": api_base,
        })
        h = self.handlers.get(model)
        if isinstance(h, BaseException):
            raise h
        if h is None:
            raise AssertionError(f"no handler for model {model!r}")
        return CompletionResult(text=h, prompt_tokens=10, completion_tokens=5, cost_usd=0.0)

    def stream(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        self.calls.append({
            "mode": "stream", "model": model,
            "temperature": temperature, "seed": seed,
            "api_base": api_base,
        })
        h = self.handlers.get(model)
        if isinstance(h, BaseException):
            raise h
        if h is None:
            raise AssertionError(f"no handler for model {model!r}")
        yield CompletionStreamChunk(
            delta=h, finish_reason="stop",
            prompt_tokens=10, completion_tokens=5, cost_usd=0.0,
        )


def _routing(*, with_fallback: bool = True) -> RoutingSettings:
    return RoutingSettings(
        registry={
            "local-coder": ModelEntry(
                provider="ollama", model="qwen2.5-coder:7b",
                endpoint="http://localhost:11434",
            ),
            "frontier": ModelEntry(provider="anthropic", model="claude-sonnet"),
        },
        loops={
            "inner": LoopBinding(model="local-coder", temperature=0.4, n=1),
            "outer": LoopBinding(model="frontier", temperature=0.2, n=1),
        },
        tasks={
            "rtl_gen": TaskBinding(model="local-coder", temperature=0.8, n=1),
            "rtl_repair": TaskBinding(model="local-coder", temperature=0.4, n=1),
            "diagnose": TaskBinding(model="frontier", temperature=0.2, n=1),
            "plan": TaskBinding(model="frontier", temperature=0.1, n=1),
            "spec_intake": TaskBinding(model="frontier", temperature=0.0, n=1),
            "tb_gen": TaskBinding(model="local-coder", temperature=0.2, n=1),
        },
        fallback={"local-coder": "frontier"} if with_fallback else {},
    )


def _router_with_backend(
    backend: _PerModelBackend,
    *,
    routing: RoutingSettings,
    audit_log: SqliteAuditLog | None = None,
    design_id: str | None = None,
) -> LiteLLMRouter:
    gw = LiteLLMGateway(backend=backend)
    return LiteLLMRouter(
        routing=routing, gateway=gw,
        audit_log=audit_log, design_id=design_id,
    )


# --------------------------------------------------------------------------- #
# AC: unreachable primary -> fallback returns successfully via the fallback.
# --------------------------------------------------------------------------- #
def test_local_backend_unreachable_falls_through_to_frontier() -> None:
    backend = _PerModelBackend(handlers={
        "ollama/qwen2.5-coder:7b": BackendUnreachableError(
            "connection refused at localhost:11434"
        ),
        "anthropic/claude-sonnet": "ok from frontier",
    })
    router = _router_with_backend(backend, routing=_routing())
    result = router.generate(
        TaskType.RTL_REPAIR, context={"prompt": "fix this"},
    )
    assert result.chosen == "ok from frontier"
    assert result.invocation.provider == "anthropic"
    assert result.invocation.model == "claude-sonnet"
    # Both backends saw a request: primary first (failed), then fallback.
    assert backend.calls[0]["model"] == "ollama/qwen2.5-coder:7b"
    assert backend.calls[1]["model"] == "anthropic/claude-sonnet"


# --------------------------------------------------------------------------- #
# AC: BACKEND_FALLBACK audit event recorded.
# --------------------------------------------------------------------------- #
def test_fallback_emits_backend_fallback_audit_event(tmp_path: Path) -> None:
    audit = SqliteAuditLog(
        db_path=tmp_path / "audit.sqlite", hmac_key=b"f11.5-test",
    )
    try:
        backend = _PerModelBackend(handlers={
            "ollama/qwen2.5-coder:7b": BackendUnreachableError("refused"),
            "anthropic/claude-sonnet": "ok",
        })
        router = _router_with_backend(
            backend, routing=_routing(),
            audit_log=audit, design_id="d-fallback",
        )
        router.generate(TaskType.RTL_REPAIR, context={"prompt": "x"})

        events = audit.events("d-fallback")
        fallback_events = [
            e for e in events if e.event_type is EventType.BACKEND_FALLBACK
        ]
        assert len(fallback_events) == 1
        payload = fallback_events[0].payload
        assert payload["task"] == "rtl_repair"
        assert payload["primary"]["name"] == "local-coder"
        assert payload["primary"]["provider"] == "ollama"
        assert payload["fallback"]["name"] == "frontier"
        assert payload["fallback"]["provider"] == "anthropic"
        assert "refused" in payload["reason"]
    finally:
        audit.close()


# --------------------------------------------------------------------------- #
# AC: no fallback configured -> BackendUnreachableError propagates.
# --------------------------------------------------------------------------- #
def test_no_fallback_configured_raises() -> None:
    backend = _PerModelBackend(handlers={
        "ollama/qwen2.5-coder:7b": BackendUnreachableError("refused"),
    })
    router = _router_with_backend(backend, routing=_routing(with_fallback=False))
    with pytest.raises(BackendUnreachableError, match="refused"):
        router.generate(TaskType.RTL_REPAIR, context={"prompt": "x"})


# --------------------------------------------------------------------------- #
# Non-connection errors don't trigger fallback.
# --------------------------------------------------------------------------- #
def test_non_connection_error_does_not_trigger_fallback() -> None:
    from chip_agent.routing.gateway import GatewayError

    backend = _PerModelBackend(handlers={
        "ollama/qwen2.5-coder:7b": GatewayError("malformed response"),
    })
    router = _router_with_backend(backend, routing=_routing())
    with pytest.raises(GatewayError, match="malformed"):
        router.generate(TaskType.RTL_REPAIR, context={"prompt": "x"})
    # Only the primary was called; fallback was NOT consulted.
    assert len(backend.calls) == 1
    assert backend.calls[0]["model"] == "ollama/qwen2.5-coder:7b"


# --------------------------------------------------------------------------- #
# Fallback is announced ONCE per router instance (stderr).
# --------------------------------------------------------------------------- #
def test_fallback_announces_to_stderr_once(capsys: pytest.CaptureFixture[str]) -> None:
    backend = _PerModelBackend(handlers={
        "ollama/qwen2.5-coder:7b": BackendUnreachableError("refused"),
        "anthropic/claude-sonnet": "ok",
    })
    router = _router_with_backend(backend, routing=_routing())
    router.generate(TaskType.RTL_REPAIR, context={"prompt": "x"})
    router.generate(TaskType.RTL_REPAIR, context={"prompt": "y"})
    err = capsys.readouterr().err
    # Exactly one warning across two fallbacks.
    assert err.count("WARNING") == 1
    assert "local-coder" in err
    assert "frontier" in err


# --------------------------------------------------------------------------- #
# Multi-candidate path also falls back (and per-call rather than per-batch).
# --------------------------------------------------------------------------- #
def test_multi_candidate_falls_back_per_call() -> None:
    """When ``n > 1``, every call is wrapped; each gets a chance at fallback.

    Setup: primary always refuses; fallback always returns "ok".
    Expected: every call falls back; result is "ok" with frontier provider.
    """
    backend = _PerModelBackend(handlers={
        "ollama/qwen2.5-coder:7b": BackendUnreachableError("refused"),
        "anthropic/claude-sonnet": "ok",
    })
    routing = RoutingSettings(
        registry={
            "local-coder": ModelEntry(
                provider="ollama", model="qwen2.5-coder:7b",
                endpoint="http://localhost:11434",
            ),
            "frontier": ModelEntry(provider="anthropic", model="claude-sonnet"),
        },
        loops={
            "inner": LoopBinding(model="local-coder", temperature=0.4, n=1),
        },
        tasks={
            "rtl_gen": TaskBinding(model="local-coder", temperature=0.8, n=3),
        },
        fallback={"local-coder": "frontier"},
    )
    router = _router_with_backend(backend, routing=routing)
    result = router.generate(TaskType.RTL_GEN, context={"prompt": "p"})
    assert result.chosen == "ok"
    assert result.invocation.provider == "anthropic"
    # 3 attempts on primary (all fail), 3 successes on fallback -> 6 calls.
    assert len(backend.calls) == 6


# --------------------------------------------------------------------------- #
# Stream fallback: BackendUnreachableError on first chunk routes via fallback.
# --------------------------------------------------------------------------- #
def test_stream_falls_back_when_primary_unreachable() -> None:
    backend = _PerModelBackend(handlers={
        "ollama/qwen2.5-coder:7b": BackendUnreachableError("refused"),
        "anthropic/claude-sonnet": "frontier-stream-text",
    })
    router = _router_with_backend(backend, routing=_routing())
    chunks = list(
        router.stream(TaskType.RTL_REPAIR, context={"prompt": "fix"})
    )
    text = "".join(c.delta for c in chunks)
    assert text == "frontier-stream-text"
    final = chunks[-1]
    assert final.invocation is not None
    assert final.invocation.provider == "anthropic"


def test_stream_no_fallback_propagates_unreachable_error() -> None:
    backend = _PerModelBackend(handlers={
        "ollama/qwen2.5-coder:7b": BackendUnreachableError("refused"),
    })
    router = _router_with_backend(
        backend, routing=_routing(with_fallback=False),
    )
    with pytest.raises(BackendUnreachableError):
        list(router.stream(TaskType.RTL_REPAIR, context={"prompt": "fix"}))


# --------------------------------------------------------------------------- #
# Fallback validates at settings construction time.
# --------------------------------------------------------------------------- #
def test_fallback_key_not_in_registry_rejected_at_settings_time() -> None:
    with pytest.raises(ValueError, match=r"routing\.fallback"):
        RoutingSettings(
            registry={
                "frontier": ModelEntry(provider="anthropic", model="m"),
            },
            fallback={"local-coder": "frontier"},  # primary not in registry
        )


def test_fallback_value_not_in_registry_rejected_at_settings_time() -> None:
    with pytest.raises(ValueError, match=r"routing\.fallback"):
        RoutingSettings(
            registry={
                "local-coder": ModelEntry(provider="ollama", model="m"),
            },
            fallback={"local-coder": "non-existent"},
        )


def test_fallback_self_loop_rejected_at_settings_time() -> None:
    with pytest.raises(ValueError, match="must not map to itself"):
        RoutingSettings(
            registry={
                "local-coder": ModelEntry(provider="ollama", model="m"),
            },
            fallback={"local-coder": "local-coder"},
        )


# --------------------------------------------------------------------------- #
# Connection-error classification (the gateway's heuristic).
# --------------------------------------------------------------------------- #
def test_gateway_classifies_python_connection_errors_as_unreachable() -> None:
    """The gateway's ``_is_connection_error`` matches Python-stdlib
    ``ConnectionError`` and any provider exception class named with a
    connection-flavoured noun (``APIConnectionError``, ``Timeout``)."""
    from chip_agent.routing.gateway import _is_connection_error

    assert _is_connection_error(ConnectionError("refused"))
    assert _is_connection_error(ConnectionRefusedError("nope"))
    assert _is_connection_error(TimeoutError("slow"))

    class APIConnectionError(Exception):
        """Mock litellm-shaped exception."""

    assert _is_connection_error(APIConnectionError("upstream is dead"))
    # An unrelated runtime error is not classified as unreachable.
    assert not _is_connection_error(RuntimeError("bad json"))
