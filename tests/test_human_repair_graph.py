"""F23.3 graph wiring: the interactive HUMAN turn on an RTL escalation.

Exercises the real ``state_graph`` seams (not just the pure helpers):

* ``_try_interactive_human_turn`` opens a turn, distils + persists a
  ``HumanHint``, and returns a re-entry ``Command`` — bounded by
  ``max_human_turns``, and declining (``None``) when no transcript is
  supplied so the caller falls back to the human gate.
* ``_load_hint_text`` renders the latest stored hint for the RTL repair
  prompt (and returns ``None`` when there is none).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chip_agent.agents.human_hint_distill import HumanHintDistillAgent
from chip_agent.design_state import (
    DesignState,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    HumanHint,
    ModelInvocation,
    ModuleState,
    Provenance,
    Stage,
    TaskType,
)
from chip_agent.graph.blackboard import get_or_create_stage_state
from chip_agent.graph.stage_context import StageContext
from chip_agent.graph.state_graph import (
    _NODE_NAMES,
    _load_hint_text,
    _try_interactive_human_turn,
)
from chip_agent.store import SqliteArtifactStore


@dataclass
class StubRouter:
    chosen: str
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic", model="claude-sonnet-4-6", temperature=0.0,
        ),
    )

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
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
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


_DISTILL_JSON = (
    '{"hint_kind": "point_at_bug", '
    '"summary": "Add the final addRoundKey after round 31.", '
    '"suggested_route": "regen_current_rtl"}'
)


def _state() -> DesignState:
    st = DesignState(design_id="d0", name="present80")
    st.modules["m"] = ModuleState(module_id="m", name="present80")
    return st


def _diagnosis() -> FailureDiagnosis:
    return FailureDiagnosis(
        artifact_id="d0.m.diagnosis", design_id="d0", module_id="m",
        nl_summary="ciphertext wrong", failing_signal="ciphertext",
        provenance=Provenance(produced_by=Stage.RTL, agent="rtl_stage"),
    )


def _ctx(store: SqliteArtifactStore, transcript: str | None) -> StageContext:
    return StageContext(
        store=store,
        human_hint_distiller=HumanHintDistillAgent(
            router=StubRouter(chosen=_DISTILL_JSON), design_id="d0",
        ),
        human_transcript_for=lambda _s, _m, _d: transcript,
    )


def test_interactive_turn_dispatches_bounded_retry(
    store: SqliteArtifactStore,
) -> None:
    state = _state()
    ctx = _ctx(store, "you forgot the final XOR with the 32nd subkey")
    outcome = SimpleNamespace(diagnosis=_diagnosis())

    cmd = _try_interactive_human_turn(ctx, state, "m", outcome, siblings=[])

    assert cmd is not None
    assert cmd.goto == _NODE_NAMES[Stage.RTL]          # REGEN_CURRENT_RTL re-entry
    # A HumanHint was persisted and is retrievable for prompt seeding.
    hint = store.get_by_id("d0.m.hint")
    assert isinstance(hint, HumanHint)
    assert "addRoundKey" in hint.summary
    # Exactly one human turn consumed on the RTL stage state.
    ss = get_or_create_stage_state(state, Stage.RTL, module_id="m")
    assert ss.human_turns_used == 1


def test_turn_declines_without_transcript(store: SqliteArtifactStore) -> None:
    """No operator input -> None (caller falls back to the human gate)."""
    state = _state()
    ctx = _ctx(store, None)
    outcome = SimpleNamespace(diagnosis=_diagnosis())
    assert _try_interactive_human_turn(ctx, state, "m", outcome, siblings=[]) is None
    ss = get_or_create_stage_state(state, Stage.RTL, module_id="m")
    assert ss.human_turns_used == 0


def test_turn_is_bounded_by_max_human_turns(store: SqliteArtifactStore) -> None:
    state = _state()
    ctx = _ctx(store, "fix the final round")
    outcome = SimpleNamespace(diagnosis=_diagnosis())
    # max_human_turns defaults to 2.
    assert _try_interactive_human_turn(ctx, state, "m", outcome, siblings=[]) is not None
    assert _try_interactive_human_turn(ctx, state, "m", outcome, siblings=[]) is not None
    # Budget spent -> declines, even with a transcript available.
    assert _try_interactive_human_turn(ctx, state, "m", outcome, siblings=[]) is None


def test_load_hint_text_renders_latest_or_none(store: SqliteArtifactStore) -> None:
    # No hint on file -> None (prompt stays byte-identical to pre-F23).
    assert _load_hint_text(_ctx(store, None), "d0", "m") is None
    # After a turn persists a hint, the loader renders it for the prompt.
    state = _state()
    ctx = _ctx(store, "add the final XOR")
    _try_interactive_human_turn(ctx, state, "m", SimpleNamespace(diagnosis=_diagnosis()), siblings=[])
    section = _load_hint_text(ctx, "d0", "m")
    assert section is not None
    assert "Operator guidance" in section
    assert "addRoundKey" in section
