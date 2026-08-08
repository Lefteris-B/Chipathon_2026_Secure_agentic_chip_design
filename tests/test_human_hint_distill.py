"""F23.2 acceptance: HumanHintDistillAgent -> typed HumanHint.

Mirrors :mod:`tests.test_reflection_routing`'s ``StubRouter`` pattern:
the agent gets a canned ``chosen`` string; the test asserts the typed
:class:`HumanHint`. The agent never raises on bad output — the defensive
path carries the operator's own words through as the ``summary`` so the
human's guidance still reaches the retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from chip_agent.agents.human_hint_distill import (
    HumanHintDistillAgent,
    HumanHintDistillError,
)
from chip_agent.design_state import (
    ArtifactKind,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    HumanHintKind,
    ModelInvocation,
    Provenance,
    ReflectionRouteKind,
    Stage,
    TaskType,
)


@dataclass
class StubRouter:
    chosen: str
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic", model="claude-sonnet-4-6",
            temperature=0.0, seed=None,
            prompt_tokens=180, completion_tokens=48, cost_usd=0.0015,
        ),
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        self.calls.append({"task": task, "context": dict(context)})
        return GenerationResult(
            candidates=[self.chosen], chosen=self.chosen,
            invocation=self.invocation,
        )


def _diagnosis() -> FailureDiagnosis:
    return FailureDiagnosis(
        artifact_id="d0.present80.diagnosis",
        design_id="d0",
        module_id="present80",
        failing_signal="ciphertext",
        cycle=240,
        expected="5579c1387b228445",
        actual="38d2f04c34635345",
        nl_summary="ciphertext wrong for the all-zero vector",
        provenance=Provenance(produced_by=Stage.RTL, agent="rtl_stage"),
    )


def test_distills_valid_json_into_typed_hint() -> None:
    router = StubRouter(chosen=(
        '{"hint_kind": "point_at_bug", '
        '"summary": "Add the final addRoundKey after round 31.", '
        '"suggested_route": "regen_current_rtl"}'
    ))
    agent = HumanHintDistillAgent(router=router, design_id="d0")
    hint = agent.distill(
        "you forgot the last XOR with the 32nd subkey",
        _diagnosis(),
        target_stage=Stage.RTL,
        target_module="present80",
    )
    assert hint.kind is ArtifactKind.HUMAN_HINT
    assert hint.hint_kind is HumanHintKind.POINT_AT_BUG
    assert hint.suggested_route is ReflectionRouteKind.REGEN_CURRENT_RTL
    assert "addRoundKey" in hint.summary
    assert hint.target_stage is Stage.RTL
    assert hint.module_id == "present80"
    assert hint.artifact_id == "d0.present80.hint"
    # Raw chat preserved out-of-hash for provenance.
    assert "last XOR" in hint.raw_transcript
    assert router.calls[0]["task"] is TaskType.HUMAN_HINT_DISTILL


def test_null_suggested_route_is_dropped() -> None:
    router = StubRouter(chosen=(
        '{"hint_kind": "extend_stimulus", '
        '"summary": "Drive the TB to done; the window is too short.", '
        '"suggested_route": null}'
    ))
    hint = HumanHintDistillAgent(router=router, design_id="d0").distill(
        "the testbench never reaches done", _diagnosis(),
        target_stage=Stage.RTL, target_module="present80",
    )
    assert hint.hint_kind is HumanHintKind.EXTEND_STIMULUS
    assert hint.suggested_route is None


def test_unparseable_output_falls_back_to_transcript() -> None:
    """Bad model output must not raise and must keep the operator's words."""
    router = StubRouter(chosen="I'm not going to answer in JSON, sorry!")
    transcript = "hold load_en high for the full 144-cycle load"
    hint = HumanHintDistillAgent(router=router, design_id="d0").distill(
        transcript, _diagnosis(), target_stage=Stage.RTL, target_module="present80",
    )
    # Fallback: usable hint, operator's words survive as the summary.
    assert hint.hint_kind is HumanHintKind.SUGGEST_APPROACH
    assert hint.summary == transcript
    assert hint.suggested_route is None


def test_empty_design_id_rejected() -> None:
    with pytest.raises(HumanHintDistillError):
        HumanHintDistillAgent(router=StubRouter(chosen="{}"), design_id="")
