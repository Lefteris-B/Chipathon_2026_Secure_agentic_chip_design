"""Planner agent (F4.2).

Converts a typed :class:`Spec` into a typed :class:`DesignPlan`: a list of
:class:`ModuleDecl` (id / name / description / ports / params / depends_on)
plus a ``top_module_id``. The plan is the structural contract every
downstream RTL agent generates against.

Implementation notes
--------------------
* The model is asked for **JSON** (a stable parser surface), not freeform
  prose. The system prompt names the exact field shape required.
* The agent is model-free above the router seam; ``ModelRouter`` is the
  only thing it imports for transport.
* Validation enforces the structural invariants downstream stages rely on:
  ``top_module_id`` exists in ``modules``; every ``depends_on`` references a
  known module; the dependency graph is acyclic.
"""

from __future__ import annotations

import json
import re
from typing import Any

from chip_agent.design_state import (
    ArtifactRef,
    DesignPlan,
    ModelRouter,
    ModuleDecl,
    Port,
    Provenance,
    Spec,
    Stage,
    TaskType,
)

__all__ = ["PlannerAgent", "PlannerError"]


_SYSTEM_PROMPT = """\
You are a chip-design planner. Given a normalised module spec, produce a
JSON object describing the module hierarchy. Output ONLY valid JSON, no
preamble, no trailing prose, no markdown fences.

Schema:

  {
    "top_module_id": "<id of the top module>",
    "modules": [
      {
        "module_id": "<unique id>",
        "name": "<verilog module name>",
        "description": "<one-line summary>",
        "ports": [
          {"name": "<sig>", "direction": "in"|"out"|"inout",
           "width": <int>, "description": "<optional>"}
        ],
        "params": {"<param>": <value>},
        "depends_on": ["<submodule_id>", ...]
      }
    ],
    "rationale": "<optional short reasoning>"
  }

Rules:

* A single-module spec yields one entry in "modules" with depends_on=[].
* Multi-module designs decompose hierarchically; the top module's
  depends_on lists its direct submodules. Submodules each appear as their
  own module entry. Do not duplicate modules.
* The dependency graph MUST be acyclic.
* Use 1 for single-bit ports.
""".strip()


# Allow either a bare JSON object or one wrapped in a ```json ... ``` fence.
_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)


class PlannerError(ValueError):
    """The router output couldn't be parsed or violated structural invariants."""


class PlannerAgent:
    """Spec -> :class:`DesignPlan`. The caller persists the result."""

    SYSTEM_PROMPT = _SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        design_id: str,
        agent_name: str = "planner",
    ) -> None:
        if not design_id:
            raise ValueError("design_id must be non-empty")
        self.router = router
        self.design_id = design_id
        self.agent_name = agent_name

    def plan(self, spec: Spec, *, system_prompt: str | None = None) -> DesignPlan:
        """Run the planner against ``spec`` and return a validated :class:`DesignPlan`."""
        if spec.design_id != self.design_id:
            raise PlannerError(
                f"spec.design_id {spec.design_id!r} does not match "
                f"agent.design_id {self.design_id!r}"
            )
        prompt = _user_prompt(spec)
        result = self.router.generate(
            TaskType.PLAN,
            context={
                "prompt": prompt,
                "system": system_prompt or self.SYSTEM_PROMPT,
            },
        )
        chosen = result.chosen
        payload = _load_json(chosen)
        modules = _parse_modules(payload)
        top = _parse_top(payload, modules)
        _validate_dependencies(modules)

        spec_ref = spec.ref() if spec.content_hash else _placeholder_ref(spec)
        return DesignPlan(
            artifact_id=f"{self.design_id}.plan",
            design_id=self.design_id,
            top_module_id=top,
            modules=modules,
            rationale=_str_or_none(payload.get("rationale")),
            provenance=Provenance(
                produced_by=Stage.PLAN,
                agent=self.agent_name,
                model=result.invocation,
                inputs=[spec_ref],
            ),
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _user_prompt(spec: Spec) -> str:
    """Inline the spec into the user prompt so the planner sees the full context."""
    parts = [
        "Normalised spec:",
        spec.normalized.strip(),
    ]
    if spec.requirements:
        parts += ["", "Requirements:", *[f"- {r}" for r in spec.requirements]]
    if spec.constraints.target_clock_ns is not None:
        parts += ["", f"Target clock period: {spec.constraints.target_clock_ns} ns"]
    parts += ["", "Produce the design plan JSON now."]
    return "\n".join(parts)


def _load_json(text: str) -> dict[str, Any]:
    """Strip an optional ```json fence and parse; raise PlannerError on failure."""
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        stripped = m.group("body").strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise PlannerError(
            f"planner output is not valid JSON: {e}; first 200 chars: {stripped[:200]!r}"
        ) from e
    if not isinstance(data, dict):
        raise PlannerError(
            f"planner output must be a JSON object, got {type(data).__name__}"
        )
    return data


def _parse_modules(payload: dict[str, Any]) -> list[ModuleDecl]:
    raw_modules = payload.get("modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        raise PlannerError("plan must declare a non-empty `modules` list")
    modules: list[ModuleDecl] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw_modules):
        if not isinstance(item, dict):
            raise PlannerError(f"modules[{i}] must be an object, got {type(item).__name__}")
        module_id = _required_str(item, "module_id", f"modules[{i}]")
        if module_id in seen_ids:
            raise PlannerError(f"duplicate module_id {module_id!r} in plan")
        seen_ids.add(module_id)
        modules.append(ModuleDecl(
            module_id=module_id,
            name=_required_str(item, "name", f"modules[{i}]"),
            description=_required_str(item, "description", f"modules[{i}]"),
            ports=_parse_ports(item.get("ports", []), where=f"modules[{i}].ports"),
            params=dict(item.get("params") or {}),
            depends_on=_string_list(item.get("depends_on") or [], where=f"modules[{i}].depends_on"),
        ))
    return modules


_VALID_DIRECTIONS = frozenset({"in", "out", "inout"})


def _parse_ports(raw: object, *, where: str) -> list[Port]:
    if not isinstance(raw, list):
        raise PlannerError(f"{where} must be a list")
    ports: list[Port] = []
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            raise PlannerError(f"{where}[{i}] must be an object")
        direction = _required_str(p, "direction", f"{where}[{i}]")
        if direction not in _VALID_DIRECTIONS:
            raise PlannerError(
                f"{where}[{i}].direction must be one of {sorted(_VALID_DIRECTIONS)}, "
                f"got {direction!r}"
            )
        width_raw = p.get("width", 1)
        if not isinstance(width_raw, int) or width_raw < 1:
            raise PlannerError(
                f"{where}[{i}].width must be a positive int, got {width_raw!r}"
            )
        ports.append(Port(
            name=_required_str(p, "name", f"{where}[{i}]"),
            direction=direction,
            width=width_raw,
            description=_str_or_none(p.get("description")),
        ))
    return ports


def _parse_top(payload: dict[str, Any], modules: list[ModuleDecl]) -> str:
    top = payload.get("top_module_id")
    if not isinstance(top, str) or not top:
        raise PlannerError("plan must declare a non-empty `top_module_id`")
    known = {m.module_id for m in modules}
    if top not in known:
        raise PlannerError(
            f"top_module_id {top!r} not in modules; declared: {sorted(known)}"
        )
    return top


def _validate_dependencies(modules: list[ModuleDecl]) -> None:
    known = {m.module_id for m in modules}
    for m in modules:
        for dep in m.depends_on:
            if dep not in known:
                raise PlannerError(
                    f"module {m.module_id!r} depends on unknown module {dep!r}; "
                    f"known: {sorted(known)}"
                )
            if dep == m.module_id:
                raise PlannerError(
                    f"module {m.module_id!r} declares a self-dependency"
                )
    _assert_acyclic(modules)


def _assert_acyclic(modules: list[ModuleDecl]) -> None:
    """Iterative DFS cycle detection over the dependency graph."""
    adj = {m.module_id: list(m.depends_on) for m in modules}
    color: dict[str, int] = {m.module_id: 0 for m in modules}  # 0=white,1=gray,2=black

    def visit(start: str) -> None:
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, child_idx = stack[-1]
            if child_idx == 0:
                if color[node] == 1:
                    cycle = [n for n, _ in stack]
                    raise PlannerError(
                        f"dependency cycle through {' -> '.join([*cycle, node])}"
                    )
                if color[node] == 2:
                    stack.pop()
                    continue
                color[node] = 1
            children = adj[node]
            if child_idx < len(children):
                stack[-1] = (node, child_idx + 1)
                child = children[child_idx]
                if color[child] == 1:
                    cycle_path = [n for n, _ in stack] + [child]
                    raise PlannerError(
                        f"dependency cycle: {' -> '.join(cycle_path)}"
                    )
                if color[child] == 0:
                    stack.append((child, 0))
            else:
                color[node] = 2
                stack.pop()

    for m in modules:
        if color[m.module_id] == 0:
            visit(m.module_id)


def _required_str(d: dict[str, Any], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise PlannerError(f"{where}.{key} must be a non-empty string, got {v!r}")
    return v


def _string_list(raw: object, *, where: str) -> list[str]:
    if not isinstance(raw, list):
        raise PlannerError(f"{where} must be a list of strings")
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item:
            raise PlannerError(f"{where}[{i}] must be a non-empty string, got {item!r}")
        out.append(item)
    return out


def _str_or_none(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    raise PlannerError(f"expected string or null, got {type(v).__name__}")


def _placeholder_ref(spec: Spec) -> ArtifactRef:
    """If the spec hasn't been stored yet, compute a ref so we still record lineage."""
    spec.content_hash = spec.compute_content_hash()
    return spec.ref()
