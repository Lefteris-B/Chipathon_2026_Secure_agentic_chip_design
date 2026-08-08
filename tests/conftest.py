"""Auto-skip tests marked `docker` when no docker daemon is reachable.

Also registers the F10.3 ``librelane_closure`` marker, which is opt-in
(env-var gated) on top of the docker availability check.
"""

from __future__ import annotations

import os

import pytest

from chip_agent.tools.sandbox import docker_daemon_available


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "librelane_closure: opt-in real-LibreLane closure smoke; "
        "requires CHIP_AGENT_LIBRELANE_CLOSURE=1 + the pinned IIC image.",
    )
    config.addinivalue_line(
        "markers",
        "live_router: opt-in real-provider LiteLLM smoke; "
        "requires CHIP_AGENT_LIVE_ROUTER=1 + the configured provider's API key.",
    )
    config.addinivalue_line(
        "markers",
        "live_ollama: opt-in real-Ollama LiteLLM smoke; "
        "requires CHIP_AGENT_LIVE_OLLAMA=1 + an Ollama daemon reachable "
        "at the configured endpoint (default http://localhost:11434).",
    )
    config.addinivalue_line(
        "markers",
        "live_end_to_end: opt-in full chat->run->resume->GDS smoke; "
        "requires CHIP_AGENT_LIVE_E2E=1, ANTHROPIC_API_KEY set, an Ollama "
        "daemon at the configured endpoint, and the pinned IIC image. "
        "Multi-minute runtime — never run as part of the default suite.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    docker_ok = docker_daemon_available()
    closure_enabled = os.environ.get("CHIP_AGENT_LIBRELANE_CLOSURE") == "1"
    live_router_enabled = os.environ.get("CHIP_AGENT_LIVE_ROUTER") == "1"
    live_ollama_enabled = os.environ.get("CHIP_AGENT_LIVE_OLLAMA") == "1"
    live_e2e_enabled = os.environ.get("CHIP_AGENT_LIVE_E2E") == "1"
    skip_docker = pytest.mark.skip(reason="docker daemon not available")
    skip_closure = pytest.mark.skip(
        reason="LibreLane closure smoke disabled; "
        "set CHIP_AGENT_LIBRELANE_CLOSURE=1 to enable",
    )
    skip_live_router = pytest.mark.skip(
        reason="live LiteLLM smoke disabled; "
        "set CHIP_AGENT_LIVE_ROUTER=1 to enable",
    )
    skip_live_ollama = pytest.mark.skip(
        reason="live Ollama smoke disabled; "
        "set CHIP_AGENT_LIVE_OLLAMA=1 to enable",
    )
    skip_live_e2e = pytest.mark.skip(
        reason="live end-to-end smoke disabled; "
        "set CHIP_AGENT_LIVE_E2E=1 to enable",
    )
    for item in items:
        if "docker" in item.keywords and not docker_ok:
            item.add_marker(skip_docker)
        if "librelane_closure" in item.keywords and not closure_enabled:
            item.add_marker(skip_closure)
        if "live_router" in item.keywords and not live_router_enabled:
            item.add_marker(skip_live_router)
        if "live_ollama" in item.keywords and not live_ollama_enabled:
            item.add_marker(skip_live_ollama)
        if "live_end_to_end" in item.keywords and not live_e2e_enabled:
            item.add_marker(skip_live_e2e)
