"""F11.4 acceptance: multi-turn :class:`SpecIntakeAgent`.

The ACs from the M11 plan:

* an under-specified prompt makes the agent surface a
  :class:`ClarifyingQuestion` instead of materialising a Spec;
* an over-specified prompt produces a :class:`Spec` on the first call;
* after ``clarifying_budget`` consecutive questions, the next call forces
  a Spec materialisation and stamps ``Spec.requirements`` with an
  ``under-specified`` flag.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from chip_agent.agents.spec_intake import (
    UNDERSPECIFIED_REQUIREMENT_PREFIX,
    ClarifyingQuestion,
    SpecIntakeAgent,
)
from chip_agent.design_state import (
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    Spec,
    TaskType,
)

_NORMALISED_COUNTER = """\
* Ports:
  - clk: input, 1 bit
  - rst_n: input, 1 bit (async active-low reset)
  - q: output, 8 bits
* Reset: asynchronous active-low
* Clock: target clock period 10 ns
""".strip()


_QUESTION_JSON_CLOCK = json.dumps({
    "type": "question",
    "question": "What clock period do you want?",
    "missing_field": "clock",
    "rationale": "no period stated in the prompt",
})

_QUESTION_JSON_RESET = json.dumps({
    "type": "question",
    "question": "Active-low or active-high reset?",
    "missing_field": "reset",
    "rationale": "reset polarity not specified",
})

_QUESTION_JSON_PORTS = json.dumps({
    "type": "question",
    "question": "What port width should the counter have?",
    "missing_field": "ports",
    "rationale": "no width specified",
})


@dataclass
class _CyclingRouter:
    """Stub :class:`ModelRouter` that yields a fixed sequence of responses."""

    responses: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)
    _idx: int = 0

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        self.calls.append({
            "task": task, "context": dict(context),
            "failure": failure, "escalation": escalation, "n": n,
        })
        text = self.responses[self._idx]
        # After the sequence is exhausted, replay the last one.
        if self._idx + 1 < len(self.responses):
            self._idx += 1
        return GenerationResult(
            candidates=[text], chosen=text,
            invocation=ModelInvocation(
                provider="stub", model="canned",
                temperature=0.0, seed=None,
                prompt_tokens=12, completion_tokens=4, cost_usd=0.0,
            ),
        )


# --------------------------------------------------------------------------- #
# AC: under-specified prompt -> ClarifyingQuestion.
# --------------------------------------------------------------------------- #
def test_underspecified_prompt_returns_clarifying_question() -> None:
    router = _CyclingRouter(responses=[_QUESTION_JSON_CLOCK])
    agent = SpecIntakeAgent(router=router, design_id="d0")
    outcome = agent.intake("make me a counter")
    assert isinstance(outcome, ClarifyingQuestion)
    assert outcome.question == "What clock period do you want?"
    assert outcome.missing_field == "clock"
    assert agent.clarifications_asked == 1


def test_clarifying_question_increments_counter() -> None:
    router = _CyclingRouter(responses=[_QUESTION_JSON_CLOCK, _QUESTION_JSON_RESET])
    agent = SpecIntakeAgent(router=router, design_id="d0")
    agent.intake("a counter")
    agent.intake("a counter, period 10ns")
    assert agent.clarifications_asked == 2


# --------------------------------------------------------------------------- #
# AC: over-specified prompt -> Spec on first call.
# --------------------------------------------------------------------------- #
def test_overspecified_prompt_returns_spec_immediately() -> None:
    router = _CyclingRouter(responses=[_NORMALISED_COUNTER])
    agent = SpecIntakeAgent(router=router, design_id="d0")
    outcome = agent.intake(
        "8-bit synchronous counter with async active-low reset, clock 10ns",
    )
    assert isinstance(outcome, Spec)
    assert agent.clarifications_asked == 0
    # Constraint extraction still works on the normalised body.
    assert outcome.constraints.target_clock_ns == 10.0
    # No under-spec flag.
    assert not any(
        r.startswith(UNDERSPECIFIED_REQUIREMENT_PREFIX)
        for r in outcome.requirements
    )


# --------------------------------------------------------------------------- #
# AC: budget exhaustion forces a Spec with under-spec flag.
# --------------------------------------------------------------------------- #
def test_clarifying_budget_forces_materialisation_on_exhaustion() -> None:
    """After ``budget`` (3) questions, the next call materialises a Spec.

    The fourth model call (here: still a question JSON) is treated as
    under-specified content; the Spec carries the under-spec flag.
    """
    router = _CyclingRouter(responses=[
        _QUESTION_JSON_CLOCK,
        _QUESTION_JSON_RESET,
        _QUESTION_JSON_PORTS,
        # Fourth response: model is still asking — but the budget is hit, so
        # intake forces a Spec from whatever shape comes back.
        _QUESTION_JSON_CLOCK,
    ])
    agent = SpecIntakeAgent(router=router, design_id="d0", clarifying_budget=3)
    assert isinstance(agent.intake("a counter"), ClarifyingQuestion)
    assert isinstance(agent.intake("a counter"), ClarifyingQuestion)
    assert isinstance(agent.intake("a counter"), ClarifyingQuestion)
    outcome = agent.intake("a counter")
    assert isinstance(outcome, Spec)
    assert any(
        r.startswith(UNDERSPECIFIED_REQUIREMENT_PREFIX)
        for r in outcome.requirements
    ), outcome.requirements


def test_budget_exhaustion_appends_force_note_to_system_prompt() -> None:
    """When the budget is exhausted, the agent re-prompts with a strict
    "do not ask further questions" instruction. The stub router captures
    every system prompt; the final call's prompt must carry the note."""
    router = _CyclingRouter(responses=[
        _QUESTION_JSON_CLOCK, _QUESTION_JSON_CLOCK,
    ])
    agent = SpecIntakeAgent(router=router, design_id="d0", clarifying_budget=1)
    agent.intake("a counter")  # uses 1/1 of budget
    agent.intake("a counter")  # budget exhausted; force-materialise call
    final_system = router.calls[-1]["context"]["system"]
    assert "clarifying budget has been exhausted" in final_system
    # The earlier call's system prompt does NOT have the force note.
    assert "clarifying budget has been exhausted" not in router.calls[0]["context"]["system"]


# --------------------------------------------------------------------------- #
# Backward-compat: plain normalised text still goes the Spec path.
# --------------------------------------------------------------------------- #
def test_plain_normalised_text_is_not_misread_as_question() -> None:
    """A normalised description that doesn't start with ``{`` must not
    confuse the JSON-detection heuristic."""
    router = _CyclingRouter(responses=[_NORMALISED_COUNTER])
    agent = SpecIntakeAgent(router=router, design_id="d0")
    outcome = agent.intake("any non-trivial spec")
    assert isinstance(outcome, Spec)


def test_json_question_in_markdown_fences_is_parsed() -> None:
    """Models sometimes wrap JSON in ```json fences; the parser should
    strip them defensively."""
    wrapped = "```json\n" + _QUESTION_JSON_CLOCK + "\n```"
    router = _CyclingRouter(responses=[wrapped])
    agent = SpecIntakeAgent(router=router, design_id="d0")
    outcome = agent.intake("a counter")
    assert isinstance(outcome, ClarifyingQuestion)
    assert outcome.missing_field == "clock"


def test_non_question_json_is_treated_as_spec_body() -> None:
    """A JSON payload that lacks ``type=question`` flows down the Spec
    path (the JSON serves as the normalised body)."""
    spec_shaped_json = json.dumps({"type": "spec", "ports": "..."})
    router = _CyclingRouter(responses=[spec_shaped_json])
    agent = SpecIntakeAgent(router=router, design_id="d0")
    outcome = agent.intake("a counter")
    assert isinstance(outcome, Spec)


def test_clarifying_budget_zero_skips_question_path_entirely() -> None:
    """``cmd_run`` constructs the agent with budget=0 so non-interactive
    runs never surface a question — even a question response is treated
    as an under-specified Spec body."""
    router = _CyclingRouter(responses=[_QUESTION_JSON_CLOCK])
    agent = SpecIntakeAgent(router=router, design_id="d0", clarifying_budget=0)
    outcome = agent.intake("a counter")
    assert isinstance(outcome, Spec)
    assert any(
        r.startswith(UNDERSPECIFIED_REQUIREMENT_PREFIX)
        for r in outcome.requirements
    )


# --------------------------------------------------------------------------- #
# Construction-time validation.
# --------------------------------------------------------------------------- #
def test_negative_clarifying_budget_rejected() -> None:
    router = _CyclingRouter(responses=[_NORMALISED_COUNTER])
    with pytest.raises(ValueError, match="clarifying_budget"):
        SpecIntakeAgent(router=router, design_id="d0", clarifying_budget=-1)
