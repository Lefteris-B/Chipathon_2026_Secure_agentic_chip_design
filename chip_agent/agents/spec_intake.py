"""Spec-intake agent (F4.1 + F11.4).

Takes a natural-language module spec and produces a typed :class:`Spec`
artifact: a normalised description that names ports / widths / reset, plus
extracted requirements and :class:`DesignConstraints`. Pure consumer of the
:class:`ModelRouter` seam — no transport-specific knowledge here.

F11.4 promotes :meth:`SpecIntakeAgent.intake` to a bounded multi-turn
exchange. When the input is under-specified, the model may return a JSON
``{"type": "question", ...}`` payload; intake then surfaces it as a typed
:class:`ClarifyingQuestion` so a caller (e.g. the chat REPL) can elicit
the missing constraint and retry. A clarifying budget (default 3) caps
the dialog: once the budget is hit, intake forces a Spec materialisation
and tags ``Spec.requirements`` with an ``under-specified`` flag so
downstream consumers can decide whether to proceed or abort.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from chip_agent.design_state import (
    DesignConstraints,
    ModelRouter,
    Provenance,
    Spec,
    Stage,
    TaskType,
)
from chip_agent.settings import ConstraintDefaults

__all__ = [
    "UNDERSPECIFIED_REQUIREMENT_PREFIX",
    "ClarifyingQuestion",
    "SpecIntakeAgent",
    "SpecIntakeError",
]


UNDERSPECIFIED_REQUIREMENT_PREFIX = "under-specified after"


_MissingField = Literal["clock", "reset", "ports", "constraints", "other"]


class ClarifyingQuestion(BaseModel):
    """A single clarifying question returned by :class:`SpecIntakeAgent`.

    The model's response is parsed into this shape when the input is
    under-specified. The chat REPL surfaces the question to the user,
    collects an answer, appends the Q/A pair to the transcript, and
    retries intake.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: Literal["question"] = "question"
    question: str
    missing_field: _MissingField = "other"
    rationale: str = ""


_SYSTEM_PROMPT = """\
You normalise natural-language chip-module specs into a structured description.

If the input is missing any critical detail (clock domain, reset polarity,
port names + widths, or top-level constraints such as target clock period
or utilisation), respond with a single-line JSON object instead of a
normalised description:

{"type": "question", "question": "<short question>", "missing_field": "clock|reset|ports|constraints|other", "rationale": "<one sentence>"}

Output ONLY the JSON object — no preamble, no markdown fences. Ask one
question at a time.

Otherwise, the normalised output MUST explicitly state, on separate
labelled lines or bullets:

* Ports: every port's name, direction (input/output/inout), and width in bits.
* Reset: whether the module has a reset, synchronous vs asynchronous, active
  polarity (e.g. "active-high synchronous reset").
* Clock: the clock signal (if any) and target clock period in ns when given.
* Any additional constraints stated by the user (area, utilisation, etc.).

Use short bullet points. Do not add a preamble; output ONLY the normalised
description.
""".strip()


_FORCE_MATERIALISE_NOTE = (
    "\n\nThe clarifying budget has been exhausted. Do NOT ask further "
    "questions. Produce the best-effort normalised description from "
    "whatever information is on hand; any missing field may be left "
    "with a sensible default or omitted."
)


# Regexes for the lightweight post-processing pass.
_CLOCK_RE = re.compile(
    r"target\s+clock(?:\s+period)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*ns",
    re.IGNORECASE,
)
_DIE_AREA_RE = re.compile(
    r"max(?:imum)?\s+die\s+area\s*[:=]?\s*(\d+(?:\.\d+)?)\s*um\^?2",
    re.IGNORECASE,
)
_UTILISATION_RE = re.compile(
    r"(?:target\s+)?utili[sz]ation\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%?",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+\.)\s+(.+?)\s*$")


class SpecIntakeError(ValueError):
    """The raw text was empty or the router produced no usable content."""


class SpecIntakeAgent:
    """Normalises a natural-language spec into a typed :class:`Spec` artifact.

    The agent does NOT persist the artifact — the caller owns the store and
    decides when to ``store.put`` it. This keeps the agent reusable from
    handlers, tests, and ad-hoc scripts.

    F11.4: :meth:`intake` returns ``Spec | ClarifyingQuestion``. The agent
    tracks a per-instance ``clarifying_budget`` (default 3). When the model
    returns a clarifying-question JSON payload AND the budget is not yet
    exhausted, intake surfaces it as a :class:`ClarifyingQuestion`; when
    the budget is exhausted, intake re-prompts with a "force materialise"
    instruction and treats whatever comes back as a normalised body, with
    a flag in ``Spec.requirements`` recording the under-specification.
    """

    SYSTEM_PROMPT = _SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        design_id: str,
        defaults: ConstraintDefaults | None = None,
        agent_name: str = "spec_intake",
        clarifying_budget: int = 3,
    ) -> None:
        if not design_id:
            raise ValueError("design_id must be non-empty")
        if clarifying_budget < 0:
            raise ValueError("clarifying_budget must be >= 0")
        self.router = router
        self.design_id = design_id
        self.defaults = defaults or ConstraintDefaults()
        self.agent_name = agent_name
        self.clarifying_budget = clarifying_budget
        self._clarifications_asked = 0

    @property
    def clarifications_asked(self) -> int:
        """Number of :class:`ClarifyingQuestion`s the agent has surfaced so far."""
        return self._clarifications_asked

    def intake(
        self, raw_text: str, *, system_prompt: str | None = None,
    ) -> Spec | ClarifyingQuestion:
        """Normalise ``raw_text`` and return either a :class:`Spec` or a
        :class:`ClarifyingQuestion` when the input is under-specified.

        ``system_prompt`` overrides :attr:`SYSTEM_PROMPT` for callers that
        want to inject project-specific guidance; the default already asks
        for ports / widths / reset (F4.1 AC) and instructs the model to
        return a JSON question when critical fields are missing (F11.4).

        After the agent has surfaced :attr:`clarifying_budget` consecutive
        questions, the next call forces a Spec materialisation: the
        agent re-prompts with a "do not ask further questions" instruction
        and stamps ``Spec.requirements`` with an ``under-specified after N
        clarifications`` flag — so a downstream consumer can decide whether
        to proceed or abort.
        """
        if not raw_text or not raw_text.strip():
            raise SpecIntakeError("raw_text must be non-empty")

        budget_exhausted = self._clarifications_asked >= self.clarifying_budget
        prompt_system = system_prompt or self.SYSTEM_PROMPT
        if budget_exhausted:
            prompt_system = prompt_system + _FORCE_MATERIALISE_NOTE

        context: dict[str, Any] = {"prompt": raw_text, "system": prompt_system}
        result = self.router.generate(TaskType.SPEC_INTAKE, context=context)
        body = result.chosen.strip()
        if not body:
            raise SpecIntakeError("router returned empty normalised spec")

        question = _try_parse_question(body)
        if question is not None and not budget_exhausted:
            self._clarifications_asked += 1
            return question

        underspecified = question is not None  # True only when budget was exhausted
        normalized = body
        requirements = _extract_requirements(normalized)
        if underspecified:
            requirements.insert(
                0,
                f"{UNDERSPECIFIED_REQUIREMENT_PREFIX} "
                f"{self.clarifying_budget} clarifications",
            )
        constraints = _extract_constraints(normalized, defaults=self.defaults)

        return Spec(
            artifact_id=f"{self.design_id}.spec",
            design_id=self.design_id,
            raw_text=raw_text,
            normalized=normalized,
            requirements=requirements,
            constraints=constraints,
            provenance=Provenance(
                produced_by=Stage.SPEC,
                agent=self.agent_name,
                model=result.invocation,
            ),
        )


def _try_parse_question(body: str) -> ClarifyingQuestion | None:
    """Detect the F11.4 clarifying-question JSON shape.

    The model is instructed to emit a single-line JSON object with
    ``type: "question"`` when the input is under-specified. We strip
    common wrappers (``json``-fenced markdown, leading prose) defensively;
    when the payload doesn't parse or doesn't carry the question shape,
    we return ``None`` so the caller treats ``body`` as a normalised spec.
    """
    candidate = body.strip()
    if candidate.startswith("```"):
        # Strip markdown fences if the model wrapped its JSON output.
        candidate = candidate.strip("`")
        # Drop a leading "json" language tag if present.
        if candidate.startswith("json"):
            candidate = candidate[len("json"):].strip()
        candidate = candidate.strip()
    if not candidate.startswith("{"):
        return None
    # Trim anything after the last ``}`` so trailing prose doesn't break parsing.
    end = candidate.rfind("}")
    if end == -1:
        return None
    candidate = candidate[: end + 1]
    try:
        payload = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "question":
        return None
    try:
        return ClarifyingQuestion.model_validate(payload)
    except (ValueError, TypeError):
        return None


def _extract_requirements(normalized: str) -> list[str]:
    """Pick out bulleted / numbered lines as discrete requirements."""
    reqs: list[str] = []
    for line in normalized.splitlines():
        m = _BULLET_RE.match(line)
        if m:
            text = m.group(1).strip()
            if text:
                reqs.append(text)
    return reqs


def _extract_constraints(
    normalized: str,
    *,
    defaults: ConstraintDefaults,
) -> DesignConstraints:
    """Pull DesignConstraints out of the normalised text; fall back to defaults."""
    constraints = DesignConstraints(
        pdk=defaults.pdk,
        std_cell_lib=defaults.std_cell_lib,
        target_clock_ns=defaults.target_clock_ns,
        max_die_area_um2=defaults.max_die_area_um2,
        target_utilization=defaults.target_utilization,
    )

    m = _CLOCK_RE.search(normalized)
    if m:
        constraints.target_clock_ns = float(m.group(1))

    m = _DIE_AREA_RE.search(normalized)
    if m:
        constraints.max_die_area_um2 = float(m.group(1))

    m = _UTILISATION_RE.search(normalized)
    if m:
        value = float(m.group(1))
        # Accept either fractional (0.7) or percentage (70) inputs.
        constraints.target_utilization = value / 100.0 if value > 1.0 else value

    return constraints
