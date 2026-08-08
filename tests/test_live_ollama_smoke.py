"""F11.1 — live Ollama smoke through ``LiteLLMRouter``.

Opt-in. Verifies that a real ``router.generate(TaskType.RTL_REPAIR, ...)``
call round-trips through ``LiteLLMGateway -> LiteLLMBackend -> litellm.completion``
to a locally-running Ollama daemon and records the right
:class:`ModelInvocation`.

Activation gates:

* ``CHIP_AGENT_LIVE_OLLAMA=1`` (handled by ``conftest.py``);
* the Ollama daemon must be reachable at the configured endpoint
  (defaults to ``http://localhost:11434``; override via
  ``CHIP_AGENT_OLLAMA_URL``).

Model name defaults to ``qwen3-coder:latest`` (the coder model the
F11.1 stub config also names); override with ``CHIP_AGENT_OLLAMA_MODEL``
to point at whatever's installed locally.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from chip_agent.design_state import TaskType
from chip_agent.routing.gateway import LiteLLMGateway
from chip_agent.routing.router import LiteLLMRouter
from chip_agent.settings import (
    LoopBinding,
    ModelEntry,
    RoutingSettings,
    TaskBinding,
)

_DEFAULT_ENDPOINT = "http://localhost:11434"
_DEFAULT_MODEL = "qwen3-coder:latest"


def _ollama_endpoint() -> str:
    return os.environ.get("CHIP_AGENT_OLLAMA_URL", _DEFAULT_ENDPOINT).rstrip("/")


def _ollama_model() -> str:
    return os.environ.get("CHIP_AGENT_OLLAMA_MODEL", _DEFAULT_MODEL)


def _skip_unless_ollama_reachable() -> None:
    """Skip the test with a clear message when the daemon is unreachable."""
    endpoint = _ollama_endpoint()
    try:
        with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=3) as resp:
            if resp.status != 200:
                pytest.skip(
                    f"Ollama at {endpoint} returned status {resp.status}",
                )
    except urllib.error.URLError as e:
        pytest.skip(f"Ollama at {endpoint} not reachable: {e.reason}")
    except OSError as e:
        pytest.skip(f"Ollama at {endpoint} not reachable: {e}")


@pytest.mark.live_ollama
def test_live_ollama_router_round_trips() -> None:
    """Real ``router.generate(...)`` hits the local Ollama and stamps provenance.

    Asserts: the returned ``GenerationResult.invocation`` carries
    ``provider="ollama"`` + the configured model; the chosen candidate
    is non-empty text; token counts are positive integers (when
    reported by the daemon).
    """
    _skip_unless_ollama_reachable()

    endpoint = _ollama_endpoint()
    model = _ollama_model()

    routing = RoutingSettings(
        registry={
            "local-coder": ModelEntry(
                provider="ollama", model=model, endpoint=endpoint,
            ),
        },
        loops={
            "inner": LoopBinding(model="local-coder", temperature=0.0, n=1),
            "outer": LoopBinding(model="local-coder", temperature=0.0, n=1),
        },
        tasks={
            "rtl_repair": TaskBinding(
                model="local-coder", temperature=0.0, n=1,
            ),
        },
    )
    router = LiteLLMRouter(routing=routing, gateway=LiteLLMGateway())

    result = router.generate(
        TaskType.RTL_REPAIR,
        context={
            "system": (
                "You are a synthesisable Verilog assistant. "
                "Output ONE line of code, no prose, no markdown fences."
            ),
            "prompt": (
                "Emit a one-line Verilog continuous assignment that drives "
                "wire y to a & b. Output only the assign statement."
            ),
        },
    )

    assert result.chosen.strip(), "Ollama returned an empty candidate"

    invocation = result.invocation
    assert invocation.provider == "ollama", (
        f"provenance.provider {invocation.provider!r} != 'ollama'"
    )
    assert invocation.model == model, (
        f"provenance.model {invocation.model!r} does not match "
        f"configured {model!r}"
    )
    # Token counts can be missing on some Ollama versions — only assert
    # positivity when they're reported.
    if invocation.prompt_tokens is not None:
        assert invocation.prompt_tokens > 0
    if invocation.completion_tokens is not None:
        assert invocation.completion_tokens > 0
