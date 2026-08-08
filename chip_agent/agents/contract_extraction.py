"""ContractExtractionAgent (F19.3).

Spec + ModuleDecl -> ContractArtifact via one router call. Mirrors
PlannerAgent (chip_agent/agents/planner.py): single-shot, JSON-mode
output, no ReAct loop. The control graph (F19.7) wraps escalation; this
agent stays simple.

The model is asked for a JSON object whose shape mirrors
ContractArtifact's content fields one-for-one. We let Pydantic do
value-level validation on the sub-models (Literal[...] polarity,
synchronicity, encoding) and wrap ValidationError as
ContractExtractionError so callers see a uniform error class.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from pydantic import ValidationError

from chip_agent.design_state import (
    ArtifactRef,
    BehaviorInvariant,
    ClockDomain,
    ContractArtifact,
    ModelRouter,
    ModuleDecl,
    PortAssumption,
    Provenance,
    ResetSpec,
    Spec,
    Stage,
    TaskType,
)

__all__ = ["ContractExtractionAgent", "ContractExtractionError"]


_SYSTEM_PROMPT = """\
You are a chip-spec contract extractor. Given a normalised module spec
and a typed ModuleDecl (id / name / description / ports), produce a JSON
object capturing the module's behavioural CONTRACT. Output ONLY valid
JSON, no preamble, no markdown fences.

Schema:

  {
    "behavior_invariants": [
      {"name": "<snake_case_id>",
       "description": "<one-line NL summary>",
       "condition": "<precise NL rule, NOT executable code>"}
    ],
    "port_assumptions": [
      {"port_name": "<name from ModuleDecl.ports>",
       "polarity": "positive" | "negative" | "n/a",
       "expected_range": [lo, hi] | null,
       "encoding": "binary" | "one_hot" | "gray" | "twos_complement" | "n/a",
       "notes": "<optional>"}
    ],
    "clock_domains": [
      {"name": "<clk port name>",
       "frequency_mhz": <float | null>,
       "source": "<external" | "PLL" | "divided" | ... | null>",
       "notes": "<optional>"}
    ],
    "reset": {"name": "<reset port name>",
              "polarity": "active_high" | "active_low" | "none",
              "synchronicity": "sync" | "async" | "none",
              "affects": ["<signal>", ...]} | null,
    "encoding": {"<key>": "<value>"},
    "ambiguity_notes": ["<spec didn't say X, I assumed Y>", ...]
  }

Rules:

* The condition strings MUST be natural language, NOT executable code.
  Downstream agents translate them independently into a Python
  reference model and into callable assertions; keeping condition as
  NL preserves their independence (the triangulation property).
* Put protocol parameters as string -> string entries in "encoding":
  baud rates ("baud_rate": "115200"), data widths ("data_bits": "8"),
  parity ("parity": "even"), FSM encodings ("fsm_style": "one_hot"),
  pipelining flags ("is_pipelined": "false"), etc.
* List EVERY "spec didn't say X, I assumed Y" decision in
  "ambiguity_notes". Downstream verification surfaces these when the
  reference model and the assertions disagree -- the disagreement
  usually traces back to a specific assumption.
* Use 1 for single-bit values; use the explicit "n/a" string for
  polarity / encoding fields rather than dropping them.
""".strip()


# Allow either a bare JSON object or one wrapped in a ```json ... ``` fence.
_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)


class ContractExtractionError(ValueError):
    """The router output couldn't be parsed or violated the schema."""


class ContractExtractionAgent:
    """Spec + ModuleDecl -> ContractArtifact. The caller persists the result."""

    SYSTEM_PROMPT: ClassVar[str] = _SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        design_id: str,
        agent_name: str = "contract_extractor",
    ) -> None:
        if not design_id:
            raise ValueError("design_id must be non-empty")
        self.router = router
        self.design_id = design_id
        self.agent_name = agent_name

    def extract(
        self,
        spec: Spec,
        module: ModuleDecl,
        *,
        plan_ref: ArtifactRef | None = None,
        system_prompt: str | None = None,
    ) -> ContractArtifact:
        """Run one router call and return a validated ContractArtifact."""
        if spec.design_id != self.design_id:
            raise ContractExtractionError(
                f"spec.design_id {spec.design_id!r} does not match "
                f"agent.design_id {self.design_id!r}"
            )
        prompt = _user_prompt(spec, module)
        result = self.router.generate(
            TaskType.CONTRACT_EXTRACTION,
            context={
                "prompt": prompt,
                "system": system_prompt or self.SYSTEM_PROMPT,
            },
        )
        payload = _load_json(result.chosen)

        behavior_invariants = _parse_behavior_invariants(payload)
        port_assumptions = _parse_port_assumptions(payload)
        clock_domains = _parse_clock_domains(payload)
        reset = _parse_reset(payload)
        encoding = _parse_encoding(payload)
        ambiguity_notes = _parse_ambiguity_notes(payload)

        spec_ref = spec.ref() if spec.content_hash else _placeholder_ref(spec)
        inputs: list[ArtifactRef] = [spec_ref]
        if plan_ref is not None:
            inputs.append(plan_ref)

        return ContractArtifact(
            artifact_id=f"{self.design_id}.{module.module_id}.contract",
            design_id=self.design_id,
            module_id=module.module_id,
            behavior_invariants=behavior_invariants,
            port_assumptions=port_assumptions,
            clock_domains=clock_domains,
            reset=reset,
            encoding=encoding,
            ambiguity_notes=ambiguity_notes,
            provenance=Provenance(
                produced_by=Stage.PLAN,
                agent=self.agent_name,
                model=result.invocation,
                inputs=inputs,
            ),
        )


# --------------------------------------------------------------------------- #
# Prompt + parsing helpers
# --------------------------------------------------------------------------- #
def _user_prompt(spec: Spec, module: ModuleDecl) -> str:
    """Inline the spec and module shape into the user prompt."""
    parts = [
        "Normalised spec:",
        spec.normalized.strip(),
    ]
    if spec.requirements:
        parts += ["", "Requirements:", *[f"- {r}" for r in spec.requirements]]
    parts += [
        "",
        f"Target module: {module.name}  (id={module.module_id})",
        f"Description: {module.description}",
    ]
    if module.ports:
        parts += ["", "Ports:"]
        for p in module.ports:
            parts.append(
                f"- {p.name} ({p.direction}, {p.width} bit"
                + ("s" if p.width != 1 else "")
                + (f", {p.description}" if p.description else "")
                + ")"
            )
    if module.params:
        parts += ["", f"Params: {module.params}"]
    parts += ["", "Produce the contract JSON now."]
    return "\n".join(parts)


def _load_json(text: str) -> dict[str, Any]:
    """Strip an optional ```json fence and parse; raise on failure."""
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        stripped = m.group("body").strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ContractExtractionError(
            f"contract output is not valid JSON: {e}; "
            f"first 200 chars: {stripped[:200]!r}"
        ) from e
    if not isinstance(data, dict):
        raise ContractExtractionError(
            f"contract output must be a JSON object, got {type(data).__name__}"
        )
    return data


def _parse_behavior_invariants(payload: dict[str, Any]) -> list[BehaviorInvariant]:
    raw = payload.get("behavior_invariants", [])
    if not isinstance(raw, list):
        raise ContractExtractionError("behavior_invariants must be a list")
    out: list[BehaviorInvariant] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ContractExtractionError(
                f"behavior_invariants[{i}] must be an object, got {type(item).__name__}"
            )
        try:
            out.append(BehaviorInvariant.model_validate(item))
        except ValidationError as e:
            raise ContractExtractionError(
                f"behavior_invariants[{i}] failed validation: {e}"
            ) from e
    return out


def _parse_port_assumptions(payload: dict[str, Any]) -> list[PortAssumption]:
    raw = payload.get("port_assumptions", [])
    if not isinstance(raw, list):
        raise ContractExtractionError("port_assumptions must be a list")
    out: list[PortAssumption] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ContractExtractionError(
                f"port_assumptions[{i}] must be an object, got {type(item).__name__}"
            )
        try:
            out.append(PortAssumption.model_validate(item))
        except ValidationError as e:
            raise ContractExtractionError(
                f"port_assumptions[{i}] failed validation: {e}"
            ) from e
    return out


def _parse_clock_domains(payload: dict[str, Any]) -> list[ClockDomain]:
    raw = payload.get("clock_domains", [])
    if not isinstance(raw, list):
        raise ContractExtractionError("clock_domains must be a list")
    out: list[ClockDomain] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ContractExtractionError(
                f"clock_domains[{i}] must be an object, got {type(item).__name__}"
            )
        try:
            out.append(ClockDomain.model_validate(item))
        except ValidationError as e:
            raise ContractExtractionError(
                f"clock_domains[{i}] failed validation: {e}"
            ) from e
    return out


def _parse_reset(payload: dict[str, Any]) -> ResetSpec | None:
    raw = payload.get("reset")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ContractExtractionError(
            f"reset must be an object or null, got {type(raw).__name__}"
        )
    try:
        return ResetSpec.model_validate(raw)
    except ValidationError as e:
        raise ContractExtractionError(f"reset failed validation: {e}") from e


def _parse_encoding(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("encoding", {})
    if not isinstance(raw, dict):
        raise ContractExtractionError(
            f"encoding must be an object, got {type(raw).__name__}"
        )
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            raise ContractExtractionError(
                f"encoding keys must be strings, got {type(k).__name__}"
            )
        if not isinstance(v, str):
            raise ContractExtractionError(
                f"encoding[{k!r}] must be a string, got {type(v).__name__}"
            )
        out[k] = v
    return out


def _parse_ambiguity_notes(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("ambiguity_notes", [])
    if not isinstance(raw, list):
        raise ContractExtractionError("ambiguity_notes must be a list of strings")
    out: list[str] = []
    for i, note in enumerate(raw):
        if not isinstance(note, str):
            raise ContractExtractionError(
                f"ambiguity_notes[{i}] must be a string, got {type(note).__name__}"
            )
        out.append(note)
    return out


def _placeholder_ref(spec: Spec) -> ArtifactRef:
    """If the spec hasn't been stored yet, compute a ref so lineage is still recorded."""
    spec.content_hash = spec.compute_content_hash()
    return spec.ref()
