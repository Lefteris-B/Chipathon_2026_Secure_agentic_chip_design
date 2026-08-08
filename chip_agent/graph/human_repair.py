"""Interactive human-repair dispatch seam (F23.3).

Pure, side-effect-light helpers that turn a distilled :class:`HumanHint`
into the same typed :class:`ReflectionRoute` the F19.9 dispatcher already
knows how to act on, build the model-facing prompt section the retry is
seeded with, and manage the bounded human sub-budget on
:class:`StageState`.

Why reuse ``ReflectionRoute``: the RTL node's ``_dispatch_rtl_failure``
already maps every ``ReflectionRouteKind`` to a graph re-entry (clear the
relevant heads, ``Command(goto=...)``). An interactive turn is just
another source of that verdict — now human-seeded — so it inherits the
exact same, already-tested dispatch mechanics. The only new machinery is
validation of the human's steer and the turn budget.

Invariants preserved:

* **Gate stays binding.** Nothing here touches ``gate_ok`` / promotion.
  A hint can only pick a re-entry route and seed a prompt; the re-run's
  own verification artifact still decides advancement.
* **Never unbounded.** :func:`grant_human_retry` consumes one
  ``StageState.human_turns_used`` per dispatched retry and refuses once
  ``max_human_turns`` is spent, so a human in the loop cannot spin it
  forever.
* **Validated steer.** A hallucinated / unresolvable route
  (``REVISIT_SIBLING_RTL`` with no resolvable sibling, unknown kind)
  falls back to ``ESCALATE_HUMAN`` — i.e. re-open the turn rather than
  act on a bad route, exactly as F19.9's parser does.
"""

from __future__ import annotations

from chip_agent.design_state import (
    EscalationLevel,
    HumanHint,
    HumanHintKind,
    ReflectionRoute,
    ReflectionRouteKind,
    StageState,
    StageStatus,
)

__all__ = [
    "grant_human_retry",
    "hint_prompt_section",
    "route_from_hint",
]


# Default recovery route per hint kind when the hint carries no explicit
# ``suggested_route``. Mirrors the distiller's map but lives here too so
# the dispatch decision is well-defined even for a hand-built hint.
_DEFAULT_ROUTE: dict[HumanHintKind, ReflectionRouteKind] = {
    HumanHintKind.POINT_AT_BUG: ReflectionRouteKind.REGEN_CURRENT_RTL,
    HumanHintKind.ADD_CONSTRAINT: ReflectionRouteKind.REGEN_CURRENT_RTL,
    HumanHintKind.SUGGEST_APPROACH: ReflectionRouteKind.REGEN_CURRENT_RTL,
    HumanHintKind.EXTEND_STIMULUS: ReflectionRouteKind.REGEN_CURRENT_RTL,
    HumanHintKind.REDIRECT_STAGE: ReflectionRouteKind.RE_EXTRACT_CONTRACT,
}


def route_from_hint(
    hint: HumanHint,
    *,
    siblings: list[str] | None = None,
    sibling_module: str | None = None,
) -> ReflectionRoute:
    """Map a validated :class:`HumanHint` to a :class:`ReflectionRoute`.

    ``sibling_module`` is only consulted for a ``REVISIT_SIBLING_RTL``
    steer (the hint has no structured sibling target); when it is unset
    or not in ``siblings`` the route degrades to ``ESCALATE_HUMAN`` so the
    turn re-opens rather than dispatching to a bogus module.
    """
    siblings = siblings or []
    kind = hint.suggested_route or _DEFAULT_ROUTE[hint.hint_kind]
    reason = f"human hint ({hint.hint_kind.value}): {hint.summary[:120]}"

    if kind is ReflectionRouteKind.REVISIT_SIBLING_RTL:
        if sibling_module is None or sibling_module not in siblings:
            return ReflectionRoute(
                kind=ReflectionRouteKind.ESCALATE_HUMAN,
                reason=(
                    "human hint suggested revisit_sibling_rtl but no "
                    f"resolvable sibling (got {sibling_module!r}, "
                    f"siblings {siblings!r})"
                ),
            )
        return ReflectionRoute(
            kind=kind, target_module=sibling_module, reason=reason,
        )

    # RE_EXTRACT_CONTRACT / REGEN_CURRENT_RTL / ESCALATE_HUMAN carry no
    # target_module.
    return ReflectionRoute(kind=kind, reason=reason)


def hint_prompt_section(hint: HumanHint) -> str:
    """The model-facing block inlined into the next repair attempt's prompt.

    Deterministic text derived from the typed hint — the raw transcript is
    intentionally NOT inlined (it is provenance-only); the distilled
    ``summary`` is the actionable carrier.
    """
    return "\n".join(
        [
            "## Operator guidance (human-in-the-loop)",
            f"Kind: {hint.hint_kind.value}",
            "An engineer reviewed this failure and advised:",
            f"  {hint.summary.strip()}",
            "Act on this guidance in your next attempt. It is a hint, not a "
            "guarantee — your output is still checked by the same gate.",
        ]
    )


def grant_human_retry(
    ss: StageState,
    *,
    retry_escalation: EscalationLevel = EscalationLevel.OUTER,
) -> bool:
    """Consume one human turn and arm a fresh bounded retry on ``ss``.

    Returns ``True`` when a turn was available (and the stage is reset for
    another repair pass), ``False`` when ``max_human_turns`` is spent — in
    which case the caller should hard-stop at the human gate.

    Resets ``attempts`` to 0 so the seeded retry gets a full loop budget,
    and parks the loop at ``retry_escalation`` (semantic outer loop by
    default — the hint is semantic guidance). Deliberately does NOT touch
    heads, ``last_failure``, or any gate state.
    """
    if ss.human_turns_left() <= 0:
        return False
    ss.human_turns_used += 1
    ss.attempts = 0
    ss.escalation = retry_escalation
    ss.status = StageStatus.ESCALATED
    return True
