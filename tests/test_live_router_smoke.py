"""F10.4 — live LiteLLM smoke.

Opt-in. Builds a real :class:`LiteLLMRouter` (no stub backend) over the
first provider whose API-key env var is set, calls
``router.generate(TaskType.RTL_GEN, ...)`` with a tiny prompt, and
asserts the returned :class:`ModelInvocation` records the configured
provider + model and non-zero token counts.

Activation gates (all must be true):

* ``CHIP_AGENT_LIVE_ROUTER=1`` is set (handled by ``conftest.py``);
* one of the supported providers' API-key env vars is set (handled by
  the per-provider skips below).

Token counts are NOT pinned to exact values — providers vary across
model versions and even runs. We only assert positivity + correct
provider/model identity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

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


@dataclass(frozen=True)
class _ProviderProfile:
    """Pins which provider/model the smoke routes against per available env var."""

    env_var: str
    provider: str
    model: str


# Ordered: first match wins. Add new providers here as the project expands.
_PROVIDER_PROFILES = (
    _ProviderProfile(
        env_var="ANTHROPIC_API_KEY",
        provider="anthropic",
        model="claude-sonnet-4-6",
    ),
    _ProviderProfile(
        env_var="OPENAI_API_KEY",
        provider="openai",
        model="gpt-4o-mini",
    ),
)


def _resolve_provider() -> _ProviderProfile:
    for profile in _PROVIDER_PROFILES:
        if os.environ.get(profile.env_var):
            return profile
    available = ", ".join(p.env_var for p in _PROVIDER_PROFILES)
    pytest.skip(
        f"no provider API key found in env; set one of: {available}",
    )


@pytest.mark.live_router
def test_live_router_round_trips_a_real_provider() -> None:
    """A real ``router.generate(...)`` hits the provider and records provenance.

    Asserts that the returned :class:`GenerationResult` carries a
    :class:`ModelInvocation` with the configured provider/model name and
    strictly positive prompt + completion token counts. The chosen
    candidate must be non-empty text.
    """
    profile = _resolve_provider()

    routing = RoutingSettings(
        registry={
            "live_model": ModelEntry(
                provider=profile.provider, model=profile.model,
            ),
        },
        loops={
            "inner": LoopBinding(model="live_model", temperature=0.0, n=1),
            "outer": LoopBinding(model="live_model", temperature=0.0, n=1),
        },
        tasks={
            "rtl_gen": TaskBinding(model="live_model", temperature=0.0, n=1),
        },
    )
    router = LiteLLMRouter(routing=routing, gateway=LiteLLMGateway())

    result = router.generate(
        TaskType.RTL_GEN,
        context={
            "system": "You are a one-line Verilog assistant. Output only RTL.",
            "prompt": (
                "Emit a one-line synthesisable Verilog assign for "
                "'output wire y = a & b;' (no fences, no prose)."
            ),
        },
    )

    assert result.chosen.strip(), "live provider returned an empty candidate"

    invocation = result.invocation
    assert invocation.provider == profile.provider, (
        f"provenance.provider {invocation.provider!r} does not match "
        f"configured {profile.provider!r}"
    )
    assert invocation.model == profile.model, (
        f"provenance.model {invocation.model!r} does not match "
        f"configured {profile.model!r}"
    )
    # Range-only assertions — providers vary across model versions.
    assert isinstance(invocation.prompt_tokens, int)
    assert invocation.prompt_tokens > 0, (
        f"expected positive prompt_tokens, got {invocation.prompt_tokens}"
    )
    assert isinstance(invocation.completion_tokens, int)
    assert invocation.completion_tokens > 0, (
        f"expected positive completion_tokens, got {invocation.completion_tokens}"
    )
