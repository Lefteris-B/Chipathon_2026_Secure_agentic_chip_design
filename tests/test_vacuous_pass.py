"""F23.6: vacuous-pass detection + gate.

* ``completion_port_never_asserted`` flags a design whose completion output
  the oracle never drives high across a non-trivial window.
* ``_check_vacuous_pass`` opens an interactive-repair turn (rather than
  silently advancing) when the TB is flagged and a distiller + turn budget
  are available; otherwise returns None (advance as normal).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chip_agent.agents.differential_tb import completion_port_never_asserted
from chip_agent.agents.human_hint_distill import HumanHintDistillAgent
from chip_agent.design_state import (
    DesignState,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    ModuleDecl,
    ModuleState,
    Port,
    TaskType,
)
from chip_agent.graph.stage_context import StageContext
from chip_agent.graph.state_graph import HUMAN_REPAIR_NODE, _check_vacuous_pass
from chip_agent.store import SqliteArtifactStore


# --------------------------------------------------------------------------- #
# completion_port_never_asserted (pure)
# --------------------------------------------------------------------------- #
def _module(ports: list[Port]) -> ModuleDecl:
    return ModuleDecl(module_id="m", name="present80", description="cipher", ports=ports)


_DONE_OUT = Port(name="done", direction="out", width=1)


def test_flags_completion_port_that_never_asserts() -> None:
    module = _module([_DONE_OUT])
    observed = [{"done": 0} for _ in range(30)]        # never finishes
    assert completion_port_never_asserted(module, observed) == "done"


def test_no_flag_when_completion_asserts() -> None:
    module = _module([_DONE_OUT])
    observed = [{"done": 0}] * 20 + [{"done": 1}] + [{"done": 0}] * 5
    assert completion_port_never_asserted(module, observed) is None


def test_no_flag_for_short_window() -> None:
    module = _module([_DONE_OUT])
    assert completion_port_never_asserted(module, [{"done": 0}, {"done": 0}]) is None


def test_no_flag_without_completion_port() -> None:
    module = _module([Port(name="count", direction="out", width=8)])
    observed = [{"count": 0} for _ in range(30)]
    assert completion_port_never_asserted(module, observed) is None


# --------------------------------------------------------------------------- #
# _check_vacuous_pass (graph gate)
# --------------------------------------------------------------------------- #
@dataclass
class StubRouter:
    chosen: str = (
        '{"hint_kind": "extend_stimulus", '
        '"summary": "Drive the TB to done — the window is too short.", '
        '"suggested_route": "regen_current_rtl"}'
    )
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic", model="claude-sonnet-4-6", temperature=0.0,
        ),
    )

    def generate(
        self, task: TaskType, *, context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        return GenerationResult(
            candidates=[self.chosen], chosen=self.chosen,
            invocation=self.invocation,
        )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteArtifactStore]:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite", content_dir=tmp_path / "content",
    )
    yield s
    s.close()


def _state() -> DesignState:
    st = DesignState(design_id="d0", name="present80")
    st.modules["m"] = ModuleState(module_id="m", name="present80")
    return st


def _plan() -> Any:
    return SimpleNamespace(modules=[SimpleNamespace(module_id="m")])


def _ctx(store: SqliteArtifactStore, *, with_distiller: bool = True) -> StageContext:
    return StageContext(
        store=store,
        human_hint_distiller=(
            HumanHintDistillAgent(router=StubRouter(), design_id="d0")
            if with_distiller else None
        ),
        human_transcript_for=None,  # Option B (pause) path
    )


def _vacuous_tb() -> Any:
    return SimpleNamespace(metadata={"vacuous_completion_port": "done"})


def test_vacuous_pass_opens_turn(store: SqliteArtifactStore) -> None:
    state = _state()
    cmd = _check_vacuous_pass(_ctx(store), state, _plan(), "m", _vacuous_tb())
    assert cmd is not None
    # Option B pause (no blocking provider wired) — halts for review.
    assert cmd.goto == HUMAN_REPAIR_NODE
    assert cmd.update["pending_human_repair"].module_id == "m"
    # A synthesized diagnosis was persisted explaining the vacuous pass.
    diag = store.get_by_id("d0.m.diagnosis")
    assert isinstance(diag, FailureDiagnosis)
    assert "vacuous" in diag.nl_summary


def test_clean_pass_advances(store: SqliteArtifactStore) -> None:
    tb_clean = SimpleNamespace(metadata={})
    assert _check_vacuous_pass(_ctx(store), _state(), _plan(), "m", tb_clean) is None


def test_no_distiller_advances(store: SqliteArtifactStore) -> None:
    # Without an interactive distiller wired, do not block a pass.
    cmd = _check_vacuous_pass(
        _ctx(store, with_distiller=False), _state(), _plan(), "m", _vacuous_tb(),
    )
    assert cmd is None
