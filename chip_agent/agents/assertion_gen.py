"""AssertionGenAgent (F19.5).

ContractArtifact -> AssertionSpec via the three-stage AssertLLM
pipeline (arXiv 2402.00386 §3):

  A: ASSERTION_EXTRACT   — contract -> SignalMap (JSON)
  B: ASSERTION_MAP       — SignalMap -> SignalDefinitions (JSON)
  C: ASSERTION_TRANSLATE — SignalDefinitions -> Python source (text)

Each stage is independently bindable via routing.tasks so cheap
structural tasks (extract / map) can route to a smaller model while
the codegen stage (translate) uses the frontier model.

Independence from F19.4 OracleGenAgent is structural:
* separate class
* separate router tasks (ASSERTION_EXTRACT/MAP/TRANSLATE, never
  ORACLE_GEN)
* no reference to OracleArtifact at any point

The F19.6 triangulation gate is the empirical check that both agents
read the same contract consistently. This agent only honours the
mechanical side (a, b above).

Persistence mirrors F19.4 OracleGenAgent: agent owns the store seam,
compile-check before persisting, leading ``# rationale: <text>``
comments lift into ``AssertionSpec.rationale_notes`` (F19.2's
``_NON_CONTENT_FIELDS`` keeps them out of the content hash).
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from chip_agent.design_state import (
    ArtifactRef,
    AssertionSpec,
    ContractArtifact,
    ModelRouter,
    ModuleDecl,
    Provenance,
    Spec,
    Stage,
    StructuredInvariant,
    TaskType,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = [
    "AssertionGenAgent",
    "AssertionGenError",
    "Signal",
    "SignalDefinition",
    "SignalDefinitions",
    "SignalMap",
]


# --------------------------------------------------------------------------- #
# Intermediate typed payloads. NOT artifacts — they live for the lifetime of
# one generate() call. No content hash, no store registry entry.
# --------------------------------------------------------------------------- #
class Signal(BaseModel):
    """One signal the contract references — structural metadata."""
    name: str
    direction: Literal["in", "out", "inout"] = "in"
    width: int = 1
    role: str = ""  # free-form: "clock", "reset", "data_in", "enable", etc.


class SignalMap(BaseModel):
    """Stage A output — structural signal inventory.

    ``referenced_invariants`` lets stage B know which invariants the
    model wants to assert against; the contract may carry more
    invariants than this list (an invariant may be inherently
    unverifiable in the cycle-by-cycle dispatch shape).
    """
    signals: list[Signal] = Field(default_factory=list)
    referenced_invariants: list[str] = Field(default_factory=list)


class SignalDefinition(BaseModel):
    """One signal's semantic role bound to its structural slot."""
    signal_name: str
    semantic_role: str
    constraints: list[str] = Field(default_factory=list)


class SignalDefinitions(BaseModel):
    """Stage B output — semantic-to-structural bindings."""
    definitions: list[SignalDefinition] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Fence stripping — mirrors oracle_gen.py.
# --------------------------------------------------------------------------- #
_PY_FENCE_RE = re.compile(
    r"^\s*```(?:python|py)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)
_PY_FENCE_SEARCH_RE = re.compile(
    r"```(?:python|py)?\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)


# --------------------------------------------------------------------------- #
# System prompts — one per stage.
# --------------------------------------------------------------------------- #
_EXTRACT_PROMPT = """\
You are the EXTRACT stage of a three-stage assertion-generation
pipeline (AssertLLM §3). Given a structured behavioural contract and
a module's port list, produce a JSON object enumerating the signals
the contract references and the invariants the agent will translate
to assertions downstream.

Output ONLY JSON, no preamble, no markdown fences.

Schema:
  {
    "signals": [
      {"name": "<port_name>",
       "direction": "in" | "out" | "inout",
       "width": <int>,
       "role": "<clock|reset|data_in|data_out|enable|status|...>"}
    ],
    "referenced_invariants": ["<invariant_name>", ...]
  }

Rules:
* Include EVERY port from the module's port list. Use the role string
  to mark structural intent ("clock", "reset", "enable", ...).
* ``referenced_invariants`` lists every ``behavior_invariant`` name
  the downstream stage should produce an assertion for. Skip
  invariants that genuinely cannot be checked cycle-by-cycle (e.g.
  a temporal invariant requiring multiple clocks worth of state).
""".strip()


_MAP_PROMPT = """\
You are the MAP stage of a three-stage assertion-generation pipeline.
Given a contract and the SignalMap from the EXTRACT stage, produce a
JSON object that binds each signal to its semantic role and lifts
any port-assumption constraints into a structured list.

Output ONLY JSON, no preamble, no markdown fences.

Schema:
  {
    "definitions": [
      {"signal_name": "<name from SignalMap>",
       "semantic_role": "<one-line NL description>",
       "constraints": ["<constraint>", ...]}
    ]
  }

Rules:
* Every signal in the SignalMap must appear in ``definitions``.
* ``semantic_role`` is NL: e.g. "active-low async reset that forces q
  to 0", "8-bit count value".
* ``constraints`` carries port_assumptions (range, encoding, polarity)
  and clock_domain frequencies. Use one short string per constraint.
""".strip()


_TRANSLATE_PROMPT = """\
You are the TRANSLATE stage of a three-stage assertion-generation
pipeline. Given a contract, a SignalMap, and SignalDefinitions, emit
a self-contained Python module that defines one assertion function
per ``referenced_invariants`` entry.

Each assertion function MUST:
* Be named ``assert_<invariant_name>`` — the invariant name lifted
  verbatim from the contract.
* Take a single positional argument ``args``, which is a tuple
  ``(stim, observed)`` where:
    - ``stim`` is a list[dict[str, int]] of input cycle values
    - ``observed`` is a list[dict[str, int]] of recorded output values
  (one entry per cycle; same length).
* Return a tuple ``(passed: bool, message: str)``.
* Carry a short docstring describing what it checks — the docstring
  is lifted into the AssertionSpec's StructuredInvariant.description.

Output ONLY Python source — no markdown fences, no preamble, no
trailing prose. Use only the Python standard library; do not import
cocotb. The functions must be importable and pure (no I/O, no
persistent global state).

Optionally prefix the file with one or more ``# rationale: <text>``
single-line comments describing your reasoning. These do not affect
function semantics but are persisted out-of-band in
``AssertionSpec.rationale_notes`` for post-hoc inspection. Place
them BEFORE any ``def assert_...`` definitions.
""".strip()


class AssertionGenError(ValueError):
    """The router output was unparseable across one of the three stages."""


class AssertionGenAgent:
    """ContractArtifact -> AssertionSpec via three sequential router calls.

    Independent of F19.4 :class:`OracleGenAgent`: distinct class,
    distinct router tasks, distinct context. The F19.6 triangulation
    gate is the empirical check that both produced consistent
    interpretations of the same contract.
    """

    EXTRACT_SYSTEM_PROMPT: ClassVar[str] = _EXTRACT_PROMPT
    MAP_SYSTEM_PROMPT: ClassVar[str] = _MAP_PROMPT
    TRANSLATE_SYSTEM_PROMPT: ClassVar[str] = _TRANSLATE_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        store: SqliteArtifactStore,
        design_id: str,
        agent_name: str = "assertion_gen",
    ) -> None:
        if not design_id:
            raise AssertionGenError("design_id must be non-empty")
        self.router = router
        self.store = store
        self.design_id = design_id
        self.agent_name = agent_name

    def generate(
        self,
        contract: ContractArtifact,
        module: ModuleDecl,
        *,
        spec: Spec | None = None,
        system_prompts: dict[TaskType, str] | None = None,
    ) -> AssertionSpec:
        """Run the three-stage pipeline and persist the resulting spec.

        Returns the persisted :class:`AssertionSpec`. Raises
        :class:`AssertionGenError` if any stage fails to parse; on
        error nothing is written to the store (no orphan blob).
        """
        if contract.design_id != self.design_id:
            raise AssertionGenError(
                f"contract.design_id {contract.design_id!r} does not match "
                f"agent.design_id {self.design_id!r}"
            )
        if contract.module_id != module.module_id:
            raise AssertionGenError(
                f"contract.module_id {contract.module_id!r} does not match "
                f"module.module_id {module.module_id!r}"
            )

        prompts = system_prompts or {}

        # --- Stage A: extract ---
        signal_map = self._extract_signals(
            contract, module, spec=spec,
            system_prompt=prompts.get(TaskType.ASSERTION_EXTRACT),
        )

        # --- Stage B: map ---
        signal_definitions = self._map_signals(
            contract, signal_map, spec=spec,
            system_prompt=prompts.get(TaskType.ASSERTION_MAP),
        )

        # --- Stage C: translate + persist ---
        return self._translate_to_assertions(
            contract, module, signal_map, signal_definitions, spec=spec,
            system_prompt=prompts.get(TaskType.ASSERTION_TRANSLATE),
        )

    # ----------------------------------------------------------- Stage A
    def _extract_signals(
        self,
        contract: ContractArtifact,
        module: ModuleDecl,
        *,
        spec: Spec | None,
        system_prompt: str | None,
    ) -> SignalMap:
        prompt = _extract_user_prompt(contract, module, spec)
        result = self.router.generate(
            TaskType.ASSERTION_EXTRACT,
            context={
                "prompt": prompt,
                "system": system_prompt or self.EXTRACT_SYSTEM_PROMPT,
            },
        )
        payload = _load_json(result.chosen, stage="extract")
        return _parse_signal_map(payload)

    # ----------------------------------------------------------- Stage B
    def _map_signals(
        self,
        contract: ContractArtifact,
        signal_map: SignalMap,
        *,
        spec: Spec | None,
        system_prompt: str | None,
    ) -> SignalDefinitions:
        prompt = _map_user_prompt(contract, signal_map, spec)
        result = self.router.generate(
            TaskType.ASSERTION_MAP,
            context={
                "prompt": prompt,
                "system": system_prompt or self.MAP_SYSTEM_PROMPT,
            },
        )
        payload = _load_json(result.chosen, stage="map")
        return _parse_signal_definitions(payload)

    # ----------------------------------------------------------- Stage C
    def _translate_to_assertions(
        self,
        contract: ContractArtifact,
        module: ModuleDecl,
        signal_map: SignalMap,
        signal_definitions: SignalDefinitions,
        *,
        spec: Spec | None,
        system_prompt: str | None,
    ) -> AssertionSpec:
        prompt = _translate_user_prompt(
            contract, module, signal_map, signal_definitions, spec,
        )
        result = self.router.generate(
            TaskType.ASSERTION_TRANSLATE,
            context={
                "prompt": prompt,
                "system": system_prompt or self.TRANSLATE_SYSTEM_PROMPT,
            },
        )
        source = _strip_python_fences(result.chosen)
        if not source.strip():
            raise AssertionGenError(
                "translate stage produced no usable Python after fence stripping"
            )
        normalised = source.rstrip("\n") + "\n"

        # Syntax-check BEFORE persisting so a broken model output doesn't
        # leave an orphan blob in the store.
        try:
            tree = ast.parse(normalised, filename="<assertion_spec>")
        except SyntaxError as e:
            raise AssertionGenError(
                f"assertion source failed to parse: {e}; "
                f"first 200 chars: {normalised[:200]!r}"
            ) from e

        assertions = _extract_callsites_via_ast(tree)
        if not assertions:
            raise AssertionGenError(
                "translate stage produced no `def assert_*(...)` functions; "
                "AssertionSpec must enumerate at least one invariant"
            )

        rationale_notes = _extract_rationale_notes(normalised)

        blob = self.store.put_blob(
            normalised.encode("utf-8"), media_type="text/x-python",
        )

        inputs: list[ArtifactRef] = [contract.ref()]
        if spec is not None:
            inputs.append(spec.ref())

        spec_artifact = AssertionSpec(
            artifact_id=f"{self.design_id}.{module.module_id}.assertions",
            design_id=self.design_id,
            module_id=module.module_id,
            source=blob,
            assertions=assertions,
            rationale_notes=rationale_notes,
            provenance=Provenance(
                produced_by=Stage.PLAN,
                agent=self.agent_name,
                model=result.invocation,
                inputs=inputs,
            ),
        )
        self.store.put(spec_artifact)
        loaded = self.store.get_by_id(spec_artifact.artifact_id)
        assert isinstance(loaded, AssertionSpec)
        return loaded


# --------------------------------------------------------------------------- #
# User-prompt builders (one per stage).
# --------------------------------------------------------------------------- #
def _module_block(module: ModuleDecl) -> list[str]:
    lines = [
        f"Target module: {module.name}  (id={module.module_id})",
        f"Description: {module.description}",
    ]
    if module.ports:
        lines += ["", "Ports:"]
        for p in module.ports:
            lines.append(
                f"- {p.name} ({p.direction}, {p.width} bit"
                + ("s" if p.width != 1 else "")
                + (f", {p.description}" if p.description else "")
                + ")"
            )
    return lines


def _contract_block(contract: ContractArtifact) -> list[str]:
    lines: list[str] = ["Contract:"]
    if contract.behavior_invariants:
        lines += ["", "  behavior_invariants:"]
        for inv in contract.behavior_invariants:
            lines.append(f"    - {inv.name}: {inv.description}")
            lines.append(f"        condition: {inv.condition}")
    if contract.port_assumptions:
        lines += ["", "  port_assumptions:"]
        for pa in contract.port_assumptions:
            bits = [f"port_name={pa.port_name}"]
            if pa.polarity != "n/a":
                bits.append(f"polarity={pa.polarity}")
            if pa.expected_range is not None:
                bits.append(f"expected_range={list(pa.expected_range)}")
            if pa.encoding != "n/a":
                bits.append(f"encoding={pa.encoding}")
            lines.append("    - " + ", ".join(bits))
    if contract.clock_domains:
        lines += ["", "  clock_domains:"]
        for cd in contract.clock_domains:
            chunk = f"name={cd.name}"
            if cd.frequency_mhz is not None:
                chunk += f", frequency_mhz={cd.frequency_mhz}"
            if cd.source:
                chunk += f", source={cd.source}"
            lines.append(f"    - {chunk}")
    if contract.reset is not None:
        r = contract.reset
        lines += [
            "",
            f"  reset: name={r.name}, polarity={r.polarity}, "
            f"synchronicity={r.synchronicity}, affects={r.affects}",
        ]
    else:
        lines += ["", "  reset: none (combinational module)"]
    if contract.encoding:
        lines += ["", "  encoding:"]
        for k, v in contract.encoding.items():
            lines.append(f"    {k}: {v}")
    return lines


def _spec_block(spec: Spec | None) -> list[str]:
    if spec is None:
        return []
    return ["", "Raw spec (for grounding only):", spec.normalized.strip()]


def _extract_user_prompt(
    contract: ContractArtifact,
    module: ModuleDecl,
    spec: Spec | None,
) -> str:
    parts: list[str] = []
    parts += _module_block(module)
    parts += [""]
    parts += _contract_block(contract)
    parts += _spec_block(spec)
    parts += [
        "",
        "Produce the SignalMap JSON now.",
    ]
    return "\n".join(parts)


def _map_user_prompt(
    contract: ContractArtifact,
    signal_map: SignalMap,
    spec: Spec | None,
) -> str:
    parts: list[str] = _contract_block(contract)
    parts += [""]
    parts += ["SignalMap (from EXTRACT stage):"]
    parts.append(signal_map.model_dump_json(indent=2))
    parts += _spec_block(spec)
    parts += [
        "",
        "Produce the SignalDefinitions JSON now.",
    ]
    return "\n".join(parts)


def _translate_user_prompt(
    contract: ContractArtifact,
    module: ModuleDecl,
    signal_map: SignalMap,
    signal_definitions: SignalDefinitions,
    spec: Spec | None,
) -> str:
    parts: list[str] = _module_block(module)
    parts += [""]
    parts += _contract_block(contract)
    parts += [
        "",
        "SignalMap (from EXTRACT stage):",
        signal_map.model_dump_json(indent=2),
        "",
        "SignalDefinitions (from MAP stage):",
        signal_definitions.model_dump_json(indent=2),
    ]
    parts += _spec_block(spec)
    parts += [
        "",
        "Produce the Python assertion module now. One "
        "`def assert_<invariant_name>(args)` per "
        "`referenced_invariants` entry. Return `(bool, str)` from each.",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Parsing helpers.
# --------------------------------------------------------------------------- #
def _load_json(text: str, *, stage: str) -> dict[str, Any]:
    stripped = text.strip()
    m = _JSON_FENCE_RE.match(stripped)
    if m:
        stripped = m.group("body").strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise AssertionGenError(
            f"{stage} stage output is not valid JSON: {e}; "
            f"first 200 chars: {stripped[:200]!r}"
        ) from e
    if not isinstance(data, dict):
        raise AssertionGenError(
            f"{stage} stage output must be a JSON object, got {type(data).__name__}"
        )
    return data


def _parse_signal_map(payload: dict[str, Any]) -> SignalMap:
    try:
        return SignalMap.model_validate(payload)
    except ValidationError as e:
        raise AssertionGenError(f"extract stage SignalMap shape invalid: {e}") from e


def _parse_signal_definitions(payload: dict[str, Any]) -> SignalDefinitions:
    try:
        return SignalDefinitions.model_validate(payload)
    except ValidationError as e:
        raise AssertionGenError(
            f"map stage SignalDefinitions shape invalid: {e}"
        ) from e


def _strip_python_fences(text: str) -> str:
    stripped = text.strip()
    m = _PY_FENCE_RE.match(stripped)
    if m:
        return m.group("body").strip()
    m = _PY_FENCE_SEARCH_RE.search(stripped)
    if m:
        return m.group("body").strip()
    return stripped


def _extract_callsites_via_ast(tree: ast.Module) -> list[StructuredInvariant]:
    """Walk the module body for top-level ``def assert_<name>`` defs.

    Each becomes a StructuredInvariant whose ``name`` is the function
    name with ``assert_`` stripped, ``callsite`` is the full function
    name, and ``description`` is the docstring (or empty string).
    """
    out: list[StructuredInvariant] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("assert_"):
            continue
        name = node.name[len("assert_"):]
        description = ast.get_docstring(node) or ""
        out.append(StructuredInvariant(
            name=name,
            callsite=node.name,
            description=description.strip(),
        ))
    return out


def _extract_rationale_notes(source: str) -> list[str]:
    """Pull ``# rationale: ...`` lines from the top of the module.

    Stops at the first non-comment, non-blank line. Mirrors the F19.4
    OracleGenAgent convention so two specs with identical bodies but
    different rationale labels dedupe via F19.2's _NON_CONTENT_FIELDS.
    """
    notes: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("#"):
            break
        body = line[1:].lstrip()
        if body.lower().startswith("rationale:"):
            notes.append(body[len("rationale:"):].strip())
            continue
        break
    return notes
