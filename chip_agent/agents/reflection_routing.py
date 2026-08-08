"""ReflectionRoutingAgent (F19.9).

Adaptive recovery dispatcher for the RTL outer loop. When the F19.4
oracle + F19.5 assertions disagree with the RTL after the inner +
outer repair budgets are spent, this agent picks one of four targeted
recovery routes via a single frontier-model classification call:

* :attr:`ReflectionRouteKind.RE_EXTRACT_CONTRACT` — the contract is
  wrong upstream; clear the M19 heads and re-enter the CONTRACT node.
* :attr:`ReflectionRouteKind.REVISIT_SIBLING_RTL` — the current
  module is consuming a sibling's wrong outputs; re-run that sibling.
* :attr:`ReflectionRouteKind.REGEN_CURRENT_RTL` — the RTL is the
  problem; clear it and let the RTL node re-drive from scratch.
* :attr:`ReflectionRouteKind.ESCALATE_HUMAN` — out of ideas, route
  the spine to the human gate.

The router is bound to ``TaskType.REFLECTION_ROUTING`` (frontier model,
single-shot, low temperature). The output is strict JSON the agent
JSON-parses + validates. Two defensive fallbacks:

* When ``attempts_so_far >= max_attempts`` the agent returns
  ESCALATE_HUMAN without calling the router. The global
  ``DesignState.global_feedback_budget`` caps the dispatcher's total
  re-entries; defending at the agent layer too keeps the budget
  decisive when the dispatcher's accounting drifts.
* When the model output isn't valid JSON, isn't a dict, has no
  ``route`` key, or names an unknown route, the agent returns
  ESCALATE_HUMAN with the raw output recorded as ``reason``.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from chip_agent.design_state import (
    AssertionSpec,
    ContractArtifact,
    FailureDiagnosis,
    ModelRouter,
    OracleArtifact,
    ReflectionRoute,
    ReflectionRouteKind,
    RTLArtifact,
    TaskType,
)

__all__ = ["ReflectionRoutingAgent", "ReflectionRoutingError"]


_SYSTEM_PROMPT = """\
You are a chip-design reflection router. The RTL outer loop just
exhausted its repair budget against a differential testbench
(F19.8) that compares the DUT against an executable reference model
derived from a behavioural contract.

Your job is to pick ONE recovery route from these four:

* "re_extract_contract" — the contract itself is wrong (it
  disagrees with the spec, or its ambiguity_notes hide a real
  decision). The contract / oracle / assertions need to be
  regenerated from the spec.
* "revisit_sibling_rtl" — the failing module's inputs come from a
  sibling module whose RTL is buggy. Re-run that sibling first.
  When you pick this, set ``target_module`` to the sibling's
  module_id from the provided sibling list.
* "regen_current_rtl" — the contract is correct; the current
  module's RTL just needs another regeneration. Use this when the
  failure looks like a local bug (wrong sign, wrong shift,
  off-by-one) and the contract + sibling outputs look fine.
* "escalate_human" — none of the above will help. Use this when
  the failure pattern is unclear or the diagnosis is ambiguous.

Output STRICT JSON, no preamble, no markdown fences:

  {"route": "<one of the four>",
   "target_module": "<sibling module_id or null>",
   "reason": "<one-sentence rationale>"}

Rules:
* ``route`` MUST be one of the four exact strings above.
* ``target_module`` is meaningful ONLY for "revisit_sibling_rtl";
  use null otherwise.
* ``reason`` is one short sentence — logged for provenance, never
  affecting dispatch.
""".strip()


# Match a JSON object greedily; the agent parses the first object
# it finds so a model that prefixes prose still gets parsed.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class ReflectionRoutingError(ValueError):
    """The agent was misconfigured (e.g. empty design_id)."""


class ReflectionRoutingAgent:
    """FailureDiagnosis + M19 lineage -> typed :class:`ReflectionRoute`.

    The router task binding is ``TaskType.REFLECTION_ROUTING``. The
    agent doesn't persist anything to the store; its output is the
    typed verdict the dispatcher consumes inline.
    """

    SYSTEM_PROMPT: ClassVar[str] = _SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        design_id: str,
        agent_name: str = "reflection_router",
    ) -> None:
        if not design_id:
            raise ReflectionRoutingError("design_id must be non-empty")
        self.router = router
        self.design_id = design_id
        self.agent_name = agent_name

    def classify(
        self,
        diagnosis: FailureDiagnosis,
        *,
        contract: ContractArtifact,
        oracle: OracleArtifact,
        assertion_spec: AssertionSpec,
        rtl: RTLArtifact,
        sibling_module_ids: list[str],
        attempts_so_far: int,
        max_attempts: int,
    ) -> ReflectionRoute:
        """Pick a recovery route. Never raises on bad model output."""
        if attempts_so_far >= max_attempts:
            return ReflectionRoute(
                kind=ReflectionRouteKind.ESCALATE_HUMAN,
                reason=(
                    f"global_feedback_budget exhausted "
                    f"({attempts_so_far}/{max_attempts}); escalating."
                ),
            )

        prompt = _user_prompt(
            diagnosis=diagnosis,
            contract=contract,
            oracle=oracle,
            assertion_spec=assertion_spec,
            rtl=rtl,
            sibling_module_ids=sibling_module_ids,
            attempts_so_far=attempts_so_far,
            max_attempts=max_attempts,
        )
        result = self.router.generate(
            TaskType.REFLECTION_ROUTING,
            context={"prompt": prompt, "system": self.SYSTEM_PROMPT},
        )
        return _parse_route(result.chosen, sibling_module_ids)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def _user_prompt(
    *,
    diagnosis: FailureDiagnosis,
    contract: ContractArtifact,
    oracle: OracleArtifact,
    assertion_spec: AssertionSpec,
    rtl: RTLArtifact,
    sibling_module_ids: list[str],
    attempts_so_far: int,
    max_attempts: int,
) -> str:
    lines = [
        f"Failing module: {diagnosis.target_module or rtl.module_id!r}",
        f"Attempts so far: {attempts_so_far} / {max_attempts}",
        "",
        "Failure diagnosis:",
        f"  nl_summary: {diagnosis.nl_summary.strip()!r}",
        f"  cycle: {diagnosis.cycle}",
        f"  failing_signal: {diagnosis.failing_signal!r}",
        f"  expected: {diagnosis.expected!r}",
        f"  actual: {diagnosis.actual!r}",
    ]
    if diagnosis.suspected_cause:
        lines.append(f"  suspected_cause: {diagnosis.suspected_cause!r}")
    lines += [
        "",
        "Contract behaviour invariants:",
    ]
    for inv in contract.behavior_invariants:
        lines.append(f"  - {inv.name}: {inv.condition}")
    if contract.ambiguity_notes:
        lines += ["", "Contract ambiguity notes (FYI):"]
        for note in contract.ambiguity_notes:
            lines.append(f"  - {note}")
    lines += [
        "",
        f"Oracle reference: {oracle.artifact_id} "
        f"(reference_fn_name={oracle.reference_fn_name!r})",
        f"Assertions defined: {len(assertion_spec.assertions)}",
        f"RTL artifact: {rtl.artifact_id} (top_module={rtl.top_module!r})",
        "",
        "Sibling modules in the plan (candidates for "
        "REVISIT_SIBLING_RTL): "
        f"{sibling_module_ids if sibling_module_ids else '[none]'}",
        "",
        "Output JSON now.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _parse_route(
    raw: str, sibling_module_ids: list[str],
) -> ReflectionRoute:
    """Best-effort JSON-parse the model output; ESCALATE_HUMAN on failure.

    Validates: top-level object, ``route`` key, known route value.
    For REVISIT_SIBLING_RTL the ``target_module`` MUST be in
    ``sibling_module_ids`` — otherwise we fall back to ESCALATE_HUMAN
    (a hallucinated sibling would just re-fail downstream).
    """
    obj = _coerce_json_object(raw)
    if obj is None:
        return ReflectionRoute(
            kind=ReflectionRouteKind.ESCALATE_HUMAN,
            reason=f"unparseable router output: {raw[:160]!r}",
        )
    route_value = obj.get("route")
    if not isinstance(route_value, str):
        return ReflectionRoute(
            kind=ReflectionRouteKind.ESCALATE_HUMAN,
            reason=f"missing 'route' key in: {obj!r}",
        )
    try:
        kind = ReflectionRouteKind(route_value)
    except ValueError:
        return ReflectionRoute(
            kind=ReflectionRouteKind.ESCALATE_HUMAN,
            reason=f"unknown route value: {route_value!r}",
        )
    target_module = obj.get("target_module")
    if not isinstance(target_module, str) or not target_module:
        target_module = None
    reason_value = obj.get("reason")
    reason = reason_value if isinstance(reason_value, str) else ""

    if (
        kind is ReflectionRouteKind.REVISIT_SIBLING_RTL
        and (target_module is None or target_module not in sibling_module_ids)
    ):
        return ReflectionRoute(
            kind=ReflectionRouteKind.ESCALATE_HUMAN,
            reason=(
                f"REVISIT_SIBLING_RTL target_module "
                f"{target_module!r} not in siblings "
                f"{sibling_module_ids!r}"
            ),
        )

    if kind is not ReflectionRouteKind.REVISIT_SIBLING_RTL:
        # Only REVISIT_SIBLING_RTL carries target_module; drop the
        # field on other routes so the artifact stays canonical.
        target_module = None

    return ReflectionRoute(
        kind=kind, target_module=target_module, reason=reason,
    )


def _coerce_json_object(raw: str) -> dict[str, Any] | None:
    """Try strict ``json.loads`` first; fall back to greedy regex.

    Frontier models occasionally wrap JSON in prose despite the
    system prompt; the greedy fallback survives that.
    """
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
