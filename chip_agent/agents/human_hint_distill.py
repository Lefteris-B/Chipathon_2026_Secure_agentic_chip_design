"""HumanHintDistillAgent (F23.2).

The interactive HUMAN-escalation turn. When a stage escalates to
``EscalationLevel.HUMAN`` (F19.9's ``ESCALATE_HUMAN`` or a spent
budget), the operator converses about the failure; this agent runs a
single frontier-model call that distills that free-text chat + the
``FailureDiagnosis`` into a typed :class:`HumanHint`. The hint re-seeds
a *bounded* repair retry (see :mod:`chip_agent.graph.human_repair`).

Design invariants (CLAUDE.md):

* **Typed, not freeform.** The chat is not the carrier of state — the
  distilled ``summary`` (+ ``hint_kind`` / ``suggested_route`` /
  ``references``) is. The raw transcript is kept on the artifact only in
  a ``_NON_CONTENT_FIELDS`` slot (out of the content hash) for
  provenance/trace.
* **Never a gate.** The hint only shapes the next attempt's prompt/route;
  it can never set ``gate_ok``. The dispatcher VALIDATES any
  ``suggested_route`` before acting on it.
* **Never raises on bad model output.** Like F19.9, every defensive path
  falls back to a usable hint — here, one that carries the operator's own
  words as the ``summary`` so the human's input still reaches the retry
  even when JSON distillation fails.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from chip_agent.design_state import (
    ArtifactRef,
    FailureDiagnosis,
    HumanHint,
    HumanHintKind,
    ModelRouter,
    Provenance,
    ReflectionRouteKind,
    Stage,
    TaskType,
)

__all__ = ["HumanHintDistillAgent", "HumanHintDistillError"]


_SYSTEM_PROMPT = """\
You are a chip-design repair liaison. A stage escalated to a human
because the automated repair loop could not close the gate within its
budget. An engineer has just told you, in free text, what they think is
wrong or what to try. Distil their message into ONE structured hint the
repair loop can act on.

Output STRICT JSON, no preamble, no markdown fences:

  {"hint_kind": "<one of: point_at_bug | add_constraint |
                 suggest_approach | redirect_stage | extend_stimulus>",
   "summary": "<one self-contained paragraph, addressed to the repair
                model, capturing the engineer's guidance as an
                actionable instruction>",
   "suggested_route": "<one of: regen_current_rtl | re_extract_contract |
                        revisit_sibling_rtl | escalate_human | null>"}

Guidance:
* "point_at_bug" — the engineer named a concrete defect (wrong sign,
  missing step, off-by-one). Usually pairs with "regen_current_rtl".
* "add_constraint" — a stimulus/assumption to hold (e.g. "keep load_en
  high the whole load"). Usually "regen_current_rtl".
* "suggest_approach" — a different implementation tack.
* "redirect_stage" — the real bug is upstream (contract/spec). Usually
  "re_extract_contract".
* "extend_stimulus" — the test window is too short / never reaches done.
* Set "suggested_route" to null when unsure — the dispatcher will choose
  a sensible default from hint_kind.
* NEVER claim the design is correct or that the gate should pass. You are
  only shaping the next repair attempt.
""".strip()


# Greedy object match so a model that prefixes prose still parses.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Default recovery route per hint kind when the model gives none.
_DEFAULT_ROUTE: dict[HumanHintKind, ReflectionRouteKind] = {
    HumanHintKind.POINT_AT_BUG: ReflectionRouteKind.REGEN_CURRENT_RTL,
    HumanHintKind.ADD_CONSTRAINT: ReflectionRouteKind.REGEN_CURRENT_RTL,
    HumanHintKind.SUGGEST_APPROACH: ReflectionRouteKind.REGEN_CURRENT_RTL,
    HumanHintKind.EXTEND_STIMULUS: ReflectionRouteKind.REGEN_CURRENT_RTL,
    HumanHintKind.REDIRECT_STAGE: ReflectionRouteKind.RE_EXTRACT_CONTRACT,
}


class HumanHintDistillError(ValueError):
    """The agent was misconfigured (e.g. empty design_id)."""


class HumanHintDistillAgent:
    """Free-text operator chat + FailureDiagnosis -> typed :class:`HumanHint`.

    Bound to ``TaskType.HUMAN_HINT_DISTILL`` (frontier, single-shot, low
    temperature). Returns a fully-formed ``HumanHint`` artifact with
    provenance set; the caller persists it via the store (which assigns
    the content hash + version).
    """

    SYSTEM_PROMPT: ClassVar[str] = _SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        design_id: str,
        agent_name: str = "human_hint_distill",
    ) -> None:
        if not design_id:
            raise HumanHintDistillError("design_id must be non-empty")
        self.router = router
        self.design_id = design_id
        self.agent_name = agent_name

    def distill(
        self,
        transcript: str,
        diagnosis: FailureDiagnosis,
        *,
        target_stage: Stage,
        target_module: str | None = None,
        references: list[ArtifactRef] | None = None,
    ) -> HumanHint:
        """Distil ``transcript`` into a typed hint. Never raises on bad output."""
        refs: list[ArtifactRef] = list(references or [])
        prompt = _user_prompt(transcript, diagnosis)
        result = self.router.generate(
            TaskType.HUMAN_HINT_DISTILL,
            context={"prompt": prompt, "system": self.SYSTEM_PROMPT},
        )
        hint_kind, summary, suggested_route = _parse_hint(result.chosen, transcript)

        return HumanHint(
            artifact_id=(
                f"{self.design_id}.{target_module}.hint"
                if target_module
                else f"{self.design_id}.hint"
            ),
            design_id=self.design_id,
            module_id=target_module,
            hint_kind=hint_kind,
            target_stage=target_stage,
            summary=summary,
            suggested_route=suggested_route,
            references=refs,
            raw_transcript=transcript,
            provenance=Provenance(
                produced_by=target_stage,
                agent=self.agent_name,
                model=result.invocation,
                inputs=refs,
                notes="interactive HUMAN-escalation turn",
            ),
        )


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def _user_prompt(transcript: str, diagnosis: FailureDiagnosis) -> str:
    lines = [
        "Failure the loop could not fix:",
        f"  nl_summary: {diagnosis.nl_summary.strip()!r}",
        f"  failing_signal: {diagnosis.failing_signal!r}",
        f"  cycle: {diagnosis.cycle}",
        f"  expected: {diagnosis.expected!r}",
        f"  actual: {diagnosis.actual!r}",
    ]
    if diagnosis.suspected_cause:
        lines.append(f"  suspected_cause: {diagnosis.suspected_cause!r}")
    lines += [
        "",
        "Engineer's message (free text):",
        transcript.strip(),
        "",
        "Output the hint JSON now.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _parse_hint(
    raw: str, transcript: str,
) -> tuple[HumanHintKind, str, ReflectionRouteKind | None]:
    """Best-effort parse; fall back to a usable hint carrying the raw chat.

    The fallback ``summary`` is the operator's own words so the human's
    guidance still reaches the retry even when JSON distillation fails.
    """
    obj = _coerce_json_object(raw)
    if obj is None:
        return HumanHintKind.SUGGEST_APPROACH, transcript.strip(), None

    kind_value = obj.get("hint_kind")
    try:
        hint_kind = HumanHintKind(kind_value) if isinstance(kind_value, str) else None
    except ValueError:
        hint_kind = None
    if hint_kind is None:
        hint_kind = HumanHintKind.SUGGEST_APPROACH

    summary_value = obj.get("summary")
    summary = (
        summary_value.strip()
        if isinstance(summary_value, str) and summary_value.strip()
        else transcript.strip()
    )

    route_value = obj.get("suggested_route")
    suggested_route: ReflectionRouteKind | None
    if isinstance(route_value, str):
        try:
            suggested_route = ReflectionRouteKind(route_value)
        except ValueError:
            suggested_route = None
    else:
        suggested_route = None

    return hint_kind, summary, suggested_route


def _coerce_json_object(raw: str) -> dict[str, Any] | None:
    stripped = raw.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJECT_RE.search(stripped)
    if m is None:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
