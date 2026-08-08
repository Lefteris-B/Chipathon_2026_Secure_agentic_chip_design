"""F10.1 — ``cmd_run`` drives the real :class:`SpecIntakeAgent` + :class:`PlannerAgent`.

These tests pin the wiring: every run now goes through the agents over a
real :class:`LiteLLMRouter` (a stub :class:`CompletionBackend` returns
canned responses so the test is fully offline). Specifically:

* the SpecIntake system prompt + the Planner system prompt both land in
  the backend's recorded calls (proof the agents fired);
* the resulting :class:`Spec` and :class:`DesignPlan` have a non-stub
  ``provenance.model`` provider/model pair and a ``provenance.agent``
  string identifying the producing agent;
* a malformed Planner JSON body surfaces as a clear ``PlannerError``;
* an empty SpecIntake body surfaces as a clear ``SpecIntakeError``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from chip_agent.agents.planner import PlannerError
from chip_agent.agents.spec_intake import SpecIntakeError
from chip_agent.cli import RunArgs, cmd_run
from chip_agent.design_state import ArtifactKind
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import (
    COUNTER_RTL,
    COUNTER_SPEC_NORMALISED,
    DEFAULT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

HMAC_KEY = b"f10-1-real-agents-hmac-key"
SPEC_MD = """\
# 8-bit counter
* Top-level module ID: `counter`
* Target clock period: 10 ns.
* Target utilization: 50%.
"""


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    p = tmp_path / "counter.md"
    p.write_text(SPEC_MD)
    return p


@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


def _install_router(
    monkeypatch: pytest.MonkeyPatch,
    routing_config: Path,
    backend: StubBackend,
) -> None:
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr("chip_agent.cli._resolve_router", lambda _args, *, settings: router)


def _args(*, spec_file: Path, run_dir: Path, config: Path, design_id: str) -> RunArgs:
    return RunArgs(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id=design_id, hmac_key=HMAC_KEY, config_path=config,
    )


# --------------------------------------------------------------------------- #
# AC strand 1 — both agents fire (system prompts land in the backend log).
# --------------------------------------------------------------------------- #
def test_cmd_run_invokes_spec_intake_and_planner(
    spec_file: Path, tmp_path: Path, routing_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = StubBackend()
    _install_router(monkeypatch, routing_config, backend)

    cmd_run(_args(
        spec_file=spec_file, run_dir=tmp_path / "run",
        config=routing_config, design_id="d-agents",
    ))

    systems = [str(c["system"]) for c in backend.calls]
    assert any(
        "You normalise natural-language chip-module specs" in s
        for s in systems
    ), "SpecIntakeAgent system prompt never reached the backend"
    assert any(
        "You are a chip-design planner" in s for s in systems
    ), "PlannerAgent system prompt never reached the backend"


# --------------------------------------------------------------------------- #
# AC strand 2 — Spec + Plan provenance records the routed model invocation.
# --------------------------------------------------------------------------- #
def test_spec_and_plan_provenance_records_routed_model(
    spec_file: Path, tmp_path: Path, routing_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = StubBackend()
    _install_router(monkeypatch, routing_config, backend)
    run_dir = tmp_path / "run"

    out = cmd_run(_args(
        spec_file=spec_file, run_dir=run_dir,
        config=routing_config, design_id="d-prov",
    ))

    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        spec = store.get(out.spec_ref)
        plan = store.get(out.plan_ref)

    assert spec.provenance.agent == "spec_intake"
    assert spec.provenance.model is not None
    assert spec.provenance.model.provider == "stub"
    assert spec.provenance.model.model == "deterministic"

    assert plan.provenance.agent == "planner"
    assert plan.provenance.model is not None
    assert plan.provenance.model.provider == "stub"
    assert plan.provenance.model.model == "deterministic"
    # Plan's lineage carries the Spec ref it was generated against.
    assert spec.ref() in plan.provenance.inputs


# --------------------------------------------------------------------------- #
# AC strand 3 — agent failures surface as typed errors at the CLI seam.
# --------------------------------------------------------------------------- #
def test_planner_invalid_json_errors_clearly(
    spec_file: Path, tmp_path: Path, routing_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed Planner body propagates ``PlannerError`` through cmd_run."""
    matchers = _override(
        DEFAULT_RESPONSES,
        needle="You are a chip-design planner",
        response="not valid json{",
    )
    backend = StubBackend(matchers=matchers)
    _install_router(monkeypatch, routing_config, backend)

    with pytest.raises(PlannerError, match="not valid JSON"):
        cmd_run(_args(
            spec_file=spec_file, run_dir=tmp_path / "run",
            config=routing_config, design_id="d-bad-plan",
        ))


def test_spec_intake_empty_body_errors_clearly(
    spec_file: Path, tmp_path: Path, routing_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty SpecIntake body propagates ``SpecIntakeError`` through cmd_run."""
    matchers = _override(
        DEFAULT_RESPONSES,
        needle="You normalise natural-language chip-module specs",
        response="   \n   ",
    )
    backend = StubBackend(matchers=matchers)
    _install_router(monkeypatch, routing_config, backend)

    with pytest.raises(SpecIntakeError, match="empty"):
        cmd_run(_args(
            spec_file=spec_file, run_dir=tmp_path / "run",
            config=routing_config, design_id="d-bad-spec",
        ))


# --------------------------------------------------------------------------- #
# AC strand 4 — the produced Spec + Plan match the backend's canned bodies.
# --------------------------------------------------------------------------- #
def test_spec_and_plan_content_matches_backend_responses(
    spec_file: Path, tmp_path: Path, routing_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = StubBackend()
    _install_router(monkeypatch, routing_config, backend)
    run_dir = tmp_path / "run"

    out = cmd_run(_args(
        spec_file=spec_file, run_dir=run_dir,
        config=routing_config, design_id="d-content",
    ))

    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        spec = store.get(out.spec_ref)
        plan = store.get(out.plan_ref)
        rtl = store.get_by_id("d-content.counter.rtl")
        rtl_body = store.get_blob(rtl.source).decode("utf-8")

    # SpecIntakeAgent strips the body it persists into Spec.normalized.
    assert spec.normalized.strip() == COUNTER_SPEC_NORMALISED.strip()
    # PlannerAgent decodes the JSON; the top-module id is one of the
    # observable fields it lifts out.
    assert plan.top_module_id == "counter"
    # Plan kind round-trips.
    assert plan.kind is ArtifactKind.PLAN
    # RTLGenerationAgent's persisted body matches the backend's canned text
    # (normalised to exactly one trailing newline by ``_persist``).
    assert rtl_body == COUNTER_RTL.rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _override(
    base: Iterable, *, needle: str, response: str,
) -> tuple:
    """Return a new matchers tuple with ``needle``'s response replaced."""
    from tests._routing_stub import _PromptMatcher
    out: list[_PromptMatcher] = []
    replaced = False
    for m in base:
        if m.needle == needle:
            out.append(_PromptMatcher(needle=needle, response=response))
            replaced = True
        else:
            out.append(m)
    if not replaced:
        out.append(_PromptMatcher(needle=needle, response=response))
    return tuple(out)
