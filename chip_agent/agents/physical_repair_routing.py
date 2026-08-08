"""PhysicalRepairRoutingAgent (F21.3).

Adaptive recovery dispatcher for the SIGNOFF stage's timing leg. When
SIGNOFF closes with negative slack at one or more corners (F21.2
``MultiCornerSTAReport``, or the single-corner ``TimingReport`` fallback),
this agent picks one of four targeted recovery routes via a single
frontier-model classification call:

* :attr:`PhysicalRepairRouteKind.LOWER_DENSITY` — reduce ``pl_target_density``
  by 0.05 (floored at 0.30). Effective when the failure is congestion-
  driven (long routes / wire delay).
* :attr:`PhysicalRepairRouteKind.INCREASE_DELAY_OPTIMIZATION` — set
  ``synth_strategy="DELAY 3"``. Effective when synthesis didn't optimise
  the critical path enough.
* :attr:`PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD` — multiply
  ``clock_period_ns`` by 1.10. The "admit the target frequency was too
  tight" fallback.
* :attr:`PhysicalRepairRouteKind.ESCALATE_HUMAN` — out of ideas, route
  the spine to the human gate.

Mirrors F19.9 :class:`ReflectionRoutingAgent`: single frontier router
call, strict JSON output the agent parses + validates, three defensive
fallbacks to ESCALATE_HUMAN (budget short-circuit, JSON-parse failure,
unknown route value). The router task binding is
:attr:`TaskType.PHYSICAL_REPAIR_ROUTING`.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from chip_agent.design_state import (
    ModelRouter,
    MultiCornerSTAReport,
    PhysicalRepairRoute,
    PhysicalRepairRouteKind,
    TaskType,
    TimingReport,
)
from chip_agent.tools.librelane import PhysicalConfig

__all__ = ["PhysicalRepairRoutingAgent", "PhysicalRepairRoutingError"]


_SYSTEM_PROMPT = """\
You are a chip-design physical-flow repair router. The SIGNOFF stage
just closed with negative timing slack at one or more process corners
(tt/ss/ff or typical-only). The PHYSICAL stage already ran LibreLane
once; your job is to pick ONE LibreLane knob delta that the spine will
apply before re-running PHYSICAL + SIGNOFF.

Routes (pick exactly one):

* "lower_density" — reduce ``PL_TARGET_DENSITY`` by 0.05 (floored at
  0.30). Choose this when the failure looks placement-bound — long
  routes, congestion, wire-delay-dominated WNS.
* "increase_delay_optimization" — set ``SYNTH_STRATEGY="DELAY 3"`` so
  Yosys spends more passes optimising the critical path. Choose this
  when the failure looks synthesis-bound — a deep cone of logic on the
  failing path, slack negative even at the typical corner.
* "relax_clock_period" — multiply ``CLOCK_PERIOD`` by 1.10. Choose this
  only when the other two routes have already been tried (look at the
  route history) and the design still won't close. This admits the
  target frequency was too tight; the chip will still tape out but
  slower.
* "escalate_human" — out of ideas. Choose this when:
  - The failure shape is structural (read_verilog errors, LVS / DRC
    failures riding along) rather than slack-driven.
  - The route history already shows all three other routes tried.
  - The reasoning is too ambiguous to pick a meaningful knob.

Output STRICT JSON, no preamble, no markdown fences:

  {"route": "<one of the four>",
   "reason": "<one-sentence rationale>"}

Rules:
* ``route`` MUST be one of the four exact strings above.
* ``reason`` is one short sentence — logged for provenance, never
  affecting dispatch.
""".strip()


# Match a JSON object greedily; the agent parses the first object
# it finds so a model that prefixes prose still gets parsed.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class PhysicalRepairRoutingError(ValueError):
    """The agent was misconfigured (e.g. empty design_id)."""


class PhysicalRepairRoutingAgent:
    """SIGNOFF timing failure -> typed :class:`PhysicalRepairRoute`.

    The router task binding is :attr:`TaskType.PHYSICAL_REPAIR_ROUTING`.
    The agent doesn't persist anything to the store; its output is the
    typed verdict the dispatcher consumes inline (the route history
    rides on ``DesignState.physical_repair_routes`` instead).
    """

    SYSTEM_PROMPT: ClassVar[str] = _SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        design_id: str,
        agent_name: str = "physical_repair_router",
    ) -> None:
        if not design_id:
            raise PhysicalRepairRoutingError("design_id must be non-empty")
        self.router = router
        self.design_id = design_id
        self.agent_name = agent_name

    def classify(
        self,
        timing: MultiCornerSTAReport | TimingReport,
        *,
        current_config: PhysicalConfig,
        history: list[PhysicalRepairRoute],
        attempts_so_far: int,
        max_attempts: int,
    ) -> PhysicalRepairRoute:
        """Pick a recovery route. Never raises on bad model output."""
        if attempts_so_far >= max_attempts:
            return PhysicalRepairRoute(
                kind=PhysicalRepairRouteKind.ESCALATE_HUMAN,
                reason=(
                    f"global_feedback_budget exhausted "
                    f"({attempts_so_far}/{max_attempts}); escalating."
                ),
            )

        prompt = _user_prompt(
            timing=timing,
            current_config=current_config,
            history=history,
            attempts_so_far=attempts_so_far,
            max_attempts=max_attempts,
        )
        result = self.router.generate(
            TaskType.PHYSICAL_REPAIR_ROUTING,
            context={"prompt": prompt, "system": self.SYSTEM_PROMPT},
        )
        return _parse_route(result.chosen)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def _user_prompt(
    *,
    timing: MultiCornerSTAReport | TimingReport,
    current_config: PhysicalConfig,
    history: list[PhysicalRepairRoute],
    attempts_so_far: int,
    max_attempts: int,
) -> str:
    lines = [
        f"Design: {current_config.design_name} "
        f"(top_module={current_config.top_module})",
        f"Attempts so far: {attempts_so_far} / {max_attempts}",
        "",
        "Current PhysicalConfig:",
        f"  clock_period_ns: {current_config.clock_period_ns}",
        f"  target_utilization: {current_config.target_utilization}",
        f"  pl_target_density: {current_config.pl_target_density}",
        f"  synth_strategy: {current_config.synth_strategy!r}",
        "",
        "Timing verdict:",
    ]
    if isinstance(timing, MultiCornerSTAReport):
        lines.append(f"  worst_wns_ns: {timing.worst_wns_ns}")
        lines.append(f"  worst_tns_ns: {timing.worst_tns_ns}")
        lines.append("  per-corner:")
        for corner in timing.corners:
            lines.append(
                f"    - {corner.corner}: wns={corner.wns_ns} "
                f"tns={corner.tns_ns} "
                f"setup_violations={corner.setup_violations} "
                f"hold_violations={corner.hold_violations}"
            )
    else:
        # Single-corner TimingReport — format as a one-entry list so the
        # prompt shape is uniform.
        lines.append(f"  wns_ns: {timing.wns_ns}")
        lines.append(f"  tns_ns: {timing.tns_ns}")
        lines.append(f"  setup_violations: {timing.setup_violations}")
        lines.append(f"  hold_violations: {timing.hold_violations}")

    if timing.violations:
        lines += ["", "Violations (first 5):"]
        for v in timing.violations[:5]:
            lines.append(f"  - [{v.code}] {v.message}")

    lines += ["", "Repair history (most recent first):"]
    if history:
        for i, r in enumerate(reversed(history), start=1):
            lines.append(f"  attempt -{i}: {r.kind.value} — {r.reason}")
    else:
        lines.append("  [none — this is the first repair attempt]")

    lines += ["", "Output JSON now."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _parse_route(raw: str) -> PhysicalRepairRoute:
    """Best-effort JSON-parse the model output; ESCALATE_HUMAN on failure.

    Validates: top-level object, ``route`` key, known route value.
    """
    obj = _coerce_json_object(raw)
    if obj is None:
        return PhysicalRepairRoute(
            kind=PhysicalRepairRouteKind.ESCALATE_HUMAN,
            reason=f"unparseable router output: {raw[:160]!r}",
        )
    route_value = obj.get("route")
    if not isinstance(route_value, str):
        return PhysicalRepairRoute(
            kind=PhysicalRepairRouteKind.ESCALATE_HUMAN,
            reason=f"missing 'route' key in: {obj!r}",
        )
    try:
        kind = PhysicalRepairRouteKind(route_value)
    except ValueError:
        return PhysicalRepairRoute(
            kind=PhysicalRepairRouteKind.ESCALATE_HUMAN,
            reason=f"unknown route value: {route_value!r}",
        )
    reason_value = obj.get("reason")
    reason = reason_value if isinstance(reason_value, str) else ""
    return PhysicalRepairRoute(kind=kind, reason=reason)


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


