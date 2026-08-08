"""F23.3 / F23.4 acceptance: interactive human-repair dispatch seam.

Covers :mod:`chip_agent.graph.human_repair`:

* ``route_from_hint`` maps a hint to a validated :class:`ReflectionRoute`
  (explicit steer honoured; default per hint_kind; unresolvable
  ``revisit_sibling_rtl`` degrades to ESCALATE_HUMAN).
* ``grant_human_retry`` is bounded by ``StageState.max_human_turns`` and
  never touches gate/head/last_failure state (F23.4: the gate stays
  binding — a hint only arms another attempt, it cannot pass anything).
"""

from __future__ import annotations

from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    EscalationLevel,
    HumanHint,
    HumanHintKind,
    Provenance,
    ReflectionRouteKind,
    Stage,
    StageState,
    StageStatus,
)
from chip_agent.graph.human_repair import (
    grant_human_retry,
    hint_prompt_section,
    route_from_hint,
)


def _hint(
    *,
    hint_kind: HumanHintKind,
    suggested_route: ReflectionRouteKind | None = None,
    summary: str = "guidance",
) -> HumanHint:
    return HumanHint(
        artifact_id="d0.m.hint",
        design_id="d0",
        module_id="m",
        hint_kind=hint_kind,
        target_stage=Stage.RTL,
        summary=summary,
        suggested_route=suggested_route,
        provenance=Provenance(produced_by=Stage.RTL, agent="human_hint_distill"),
    )


# --------------------------------------------------------------------------- #
# route_from_hint
# --------------------------------------------------------------------------- #
def test_explicit_suggested_route_is_honoured() -> None:
    route = route_from_hint(
        _hint(
            hint_kind=HumanHintKind.SUGGEST_APPROACH,
            suggested_route=ReflectionRouteKind.RE_EXTRACT_CONTRACT,
        ),
    )
    assert route.kind is ReflectionRouteKind.RE_EXTRACT_CONTRACT
    assert route.target_module is None
    assert "human hint" in route.reason


def test_default_route_from_hint_kind() -> None:
    # POINT_AT_BUG with no explicit route -> REGEN_CURRENT_RTL.
    assert route_from_hint(
        _hint(hint_kind=HumanHintKind.POINT_AT_BUG),
    ).kind is ReflectionRouteKind.REGEN_CURRENT_RTL
    # REDIRECT_STAGE -> RE_EXTRACT_CONTRACT.
    assert route_from_hint(
        _hint(hint_kind=HumanHintKind.REDIRECT_STAGE),
    ).kind is ReflectionRouteKind.RE_EXTRACT_CONTRACT


def test_sibling_route_resolves_when_sibling_valid() -> None:
    route = route_from_hint(
        _hint(
            hint_kind=HumanHintKind.SUGGEST_APPROACH,
            suggested_route=ReflectionRouteKind.REVISIT_SIBLING_RTL,
        ),
        siblings=["alu", "regfile"],
        sibling_module="alu",
    )
    assert route.kind is ReflectionRouteKind.REVISIT_SIBLING_RTL
    assert route.target_module == "alu"


def test_unresolvable_sibling_degrades_to_escalate_human() -> None:
    route = route_from_hint(
        _hint(
            hint_kind=HumanHintKind.SUGGEST_APPROACH,
            suggested_route=ReflectionRouteKind.REVISIT_SIBLING_RTL,
        ),
        siblings=["alu"],
        sibling_module=None,
    )
    assert route.kind is ReflectionRouteKind.ESCALATE_HUMAN


def test_prompt_section_uses_summary_not_transcript() -> None:
    hint = _hint(hint_kind=HumanHintKind.POINT_AT_BUG, summary="add final XOR")
    hint.raw_transcript = "SECRET raw chat that must not leak into the prompt"
    section = hint_prompt_section(hint)
    assert "add final XOR" in section
    assert "SECRET raw chat" not in section
    assert "still checked by the same gate" in section


# --------------------------------------------------------------------------- #
# grant_human_retry — bounded, and gate stays binding (F23.4)
# --------------------------------------------------------------------------- #
def test_grant_human_retry_is_bounded() -> None:
    ss = StageState(stage=Stage.RTL, max_human_turns=2)
    assert grant_human_retry(ss) is True
    assert ss.human_turns_used == 1
    assert grant_human_retry(ss) is True
    assert ss.human_turns_used == 2
    # Third turn refused — never unbounded, even with a human in the loop.
    assert grant_human_retry(ss) is False
    assert ss.human_turns_used == 2
    assert ss.human_turns_left() == 0


def test_grant_human_retry_arms_fresh_bounded_attempt() -> None:
    ss = StageState(stage=Stage.RTL, attempts=3, max_attempts=3)
    assert ss.budget_left() == 0
    grant_human_retry(ss)
    # Fresh attempt budget for the seeded retry, parked at the outer loop.
    assert ss.attempts == 0
    assert ss.budget_left() == 3
    assert ss.escalation is EscalationLevel.OUTER
    assert ss.status is StageStatus.ESCALATED


def test_grant_human_retry_does_not_touch_gate_state() -> None:
    """F23.4: a human turn arms a retry — it can NEVER mark a stage passed.

    ``grant_human_retry`` must not advance the head or clear the recorded
    failure; only a real verification artifact can. We assert it leaves
    ``head`` / ``last_failure`` untouched and never sets ``PASSED``.
    """
    failure_ref = ArtifactRef(
        artifact_id="d0.m.sim", version=1, kind=ArtifactKind.SIM,
        content_hash=f"sha256:{'b' * 64}",
    )
    ss = StageState(
        stage=Stage.RTL,
        status=StageStatus.BLOCKED,
        escalation=EscalationLevel.HUMAN,
        head=None,
        last_failure=failure_ref,
        attempts=0,
    )
    granted = grant_human_retry(ss)
    assert granted is True
    assert ss.head is None                       # head never promoted by a hint
    assert ss.last_failure == failure_ref        # failure record preserved
    assert ss.status is not StageStatus.PASSED   # a hint cannot pass a stage
