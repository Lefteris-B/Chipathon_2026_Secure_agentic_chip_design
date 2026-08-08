"""F4.2 acceptance: planner produces ModuleDecls + dependency edges from a Spec."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from chip_agent.agents.planner import PlannerAgent, PlannerError
from chip_agent.design_state import (
    ArtifactKind,
    DesignConstraints,
    DesignPlan,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    Provenance,
    Spec,
    Stage,
    TaskType,
)


# --------------------------------------------------------------------------- #
# StubRouter — satisfies the ModelRouter Protocol with canned JSON.
# --------------------------------------------------------------------------- #
@dataclass
class StubRouter:
    chosen: str
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic",
            model="claude-opus-4-7",
            temperature=0.2,
            seed=None,
            prompt_tokens=200,
            completion_tokens=120,
            cost_usd=0.0035,
        )
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

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
            "escalation": escalation, "n": n,
        })
        return GenerationResult(
            candidates=[self.chosen],
            chosen=self.chosen,
            invocation=self.invocation,
        )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _spec(design_id: str = "d0") -> Spec:
    return Spec(
        artifact_id=f"{design_id}.spec",
        design_id=design_id,
        raw_text="4-bit synchronous counter, active-high sync reset",
        normalized=(
            "* Ports: clk (in, 1), rst (in, 1), q (out, 4)\n"
            "* Width: 4 bits\n"
            "* Reset: active-high synchronous\n"
        ),
        requirements=["Counter increments on rising clock edge"],
        constraints=DesignConstraints(pdk="sky130A", target_clock_ns=10.0),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


_COUNTER_JSON = json.dumps({
    "top_module_id": "counter",
    "modules": [
        {
            "module_id": "counter",
            "name": "counter",
            "description": "4-bit synchronous counter",
            "ports": [
                {"name": "clk", "direction": "in", "width": 1,
                 "description": "clock signal"},
                {"name": "rst", "direction": "in", "width": 1,
                 "description": "active-high sync reset"},
                {"name": "q", "direction": "out", "width": 4,
                 "description": "count value"},
            ],
            "params": {},
            "depends_on": [],
        }
    ],
    "rationale": "Single-module design — counter only",
})


_ALU_JSON = json.dumps({
    "top_module_id": "alu",
    "modules": [
        {
            "module_id": "adder",
            "name": "ripple_adder",
            "description": "8-bit ripple-carry adder",
            "ports": [
                {"name": "a", "direction": "in", "width": 8},
                {"name": "b", "direction": "in", "width": 8},
                {"name": "sum", "direction": "out", "width": 8},
                {"name": "cout", "direction": "out", "width": 1},
            ],
            "depends_on": [],
        },
        {
            "module_id": "shifter",
            "name": "barrel_shifter",
            "description": "8-bit barrel shifter",
            "ports": [
                {"name": "a", "direction": "in", "width": 8},
                {"name": "shamt", "direction": "in", "width": 3},
                {"name": "y", "direction": "out", "width": 8},
            ],
            "depends_on": [],
        },
        {
            "module_id": "alu",
            "name": "alu",
            "description": "8-bit ALU with add/shift",
            "ports": [
                {"name": "a", "direction": "in", "width": 8},
                {"name": "b", "direction": "in", "width": 8},
                {"name": "op", "direction": "in", "width": 2},
                {"name": "y", "direction": "out", "width": 8},
            ],
            "depends_on": ["adder", "shifter"],
        },
    ],
})


# --------------------------------------------------------------------------- #
# AC: single-module spec → one ModuleDecl with correct port list.
# --------------------------------------------------------------------------- #
def test_single_module_plan_has_one_module_with_full_port_list() -> None:
    router = StubRouter(chosen=_COUNTER_JSON)
    agent = PlannerAgent(router=router, design_id="d0")
    plan = agent.plan(_spec())

    assert isinstance(plan, DesignPlan)
    assert plan.kind is ArtifactKind.PLAN
    assert plan.design_id == "d0"
    assert plan.top_module_id == "counter"
    assert len(plan.modules) == 1

    m = plan.modules[0]
    assert m.module_id == "counter"
    assert m.name == "counter"
    port_names = [p.name for p in m.ports]
    assert port_names == ["clk", "rst", "q"]
    widths = {p.name: p.width for p in m.ports}
    assert widths == {"clk": 1, "rst": 1, "q": 4}
    dirs = {p.name: p.direction for p in m.ports}
    assert dirs == {"clk": "in", "rst": "in", "q": "out"}
    assert m.depends_on == []
    assert plan.rationale and "Single-module" in plan.rationale


# --------------------------------------------------------------------------- #
# AC: multi-module spec → dependency edges.
# --------------------------------------------------------------------------- #
def test_multi_module_plan_records_dependency_edges() -> None:
    router = StubRouter(chosen=_ALU_JSON)
    agent = PlannerAgent(router=router, design_id="d0")
    plan = agent.plan(_spec())

    ids = [m.module_id for m in plan.modules]
    assert set(ids) == {"adder", "shifter", "alu"}
    assert plan.top_module_id == "alu"

    by_id = {m.module_id: m for m in plan.modules}
    # The top module depends on its two submodules; leaves have no deps.
    assert set(by_id["alu"].depends_on) == {"adder", "shifter"}
    assert by_id["adder"].depends_on == []
    assert by_id["shifter"].depends_on == []


# --------------------------------------------------------------------------- #
# Router plumbing
# --------------------------------------------------------------------------- #
def test_router_called_with_plan_task_and_system_prompt() -> None:
    router = StubRouter(chosen=_COUNTER_JSON)
    agent = PlannerAgent(router=router, design_id="d0")
    agent.plan(_spec())
    call = router.calls[0]
    assert call["task"] is TaskType.PLAN
    assert call["context"]["system"] == PlannerAgent.SYSTEM_PROMPT
    # The spec's normalised text is inlined into the user prompt.
    user_prompt = call["context"]["prompt"]
    assert "Ports: clk" in user_prompt
    assert "Counter increments on rising clock edge" in user_prompt
    assert "10.0 ns" in user_prompt


def test_custom_system_prompt_override() -> None:
    router = StubRouter(chosen=_COUNTER_JSON)
    agent = PlannerAgent(router=router, design_id="d0")
    agent.plan(_spec(), system_prompt="custom")
    assert router.calls[0]["context"]["system"] == "custom"


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
def test_provenance_links_spec_and_records_invocation() -> None:
    invocation = ModelInvocation(
        provider="anthropic", model="claude-opus-4-7",
        temperature=0.2, seed=7, prompt_tokens=200,
        completion_tokens=120, cost_usd=0.0035,
    )
    router = StubRouter(chosen=_COUNTER_JSON, invocation=invocation)
    spec = _spec()
    agent = PlannerAgent(router=router, design_id="d0")
    plan = agent.plan(spec)

    assert plan.provenance.produced_by is Stage.PLAN
    assert plan.provenance.agent == "planner"
    assert plan.provenance.model == invocation
    assert len(plan.provenance.inputs) == 1
    inp = plan.provenance.inputs[0]
    assert inp.artifact_id == spec.artifact_id
    assert inp.kind is ArtifactKind.SPEC


def test_design_id_flows_into_artifact_id() -> None:
    router = StubRouter(chosen=_COUNTER_JSON)
    agent = PlannerAgent(router=router, design_id="counter4bit")
    spec = _spec(design_id="counter4bit")
    plan = agent.plan(spec)
    assert plan.artifact_id == "counter4bit.plan"


def test_design_id_mismatch_rejected() -> None:
    router = StubRouter(chosen=_COUNTER_JSON)
    agent = PlannerAgent(router=router, design_id="alpha")
    spec = _spec(design_id="beta")
    with pytest.raises(PlannerError):
        agent.plan(spec)


# --------------------------------------------------------------------------- #
# Code-fence stripping
# --------------------------------------------------------------------------- #
def test_code_fence_stripped() -> None:
    fenced = f"```json\n{_COUNTER_JSON}\n```"
    router = StubRouter(chosen=fenced)
    agent = PlannerAgent(router=router, design_id="d0")
    plan = agent.plan(_spec())
    assert plan.top_module_id == "counter"


def test_bare_code_fence_also_stripped() -> None:
    fenced = f"```\n{_COUNTER_JSON}\n```"
    router = StubRouter(chosen=fenced)
    agent = PlannerAgent(router=router, design_id="d0")
    plan = agent.plan(_spec())
    assert plan.top_module_id == "counter"


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #
def test_malformed_json_raises() -> None:
    router = StubRouter(chosen="not even json {{")
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError):
        agent.plan(_spec())


def test_non_object_root_rejected() -> None:
    router = StubRouter(chosen='["just", "a", "list"]')
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError):
        agent.plan(_spec())


def test_empty_modules_list_rejected() -> None:
    router = StubRouter(chosen=json.dumps({"top_module_id": "x", "modules": []}))
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "modules" in str(ei.value)


def test_missing_top_rejected() -> None:
    bad = json.dumps({
        "modules": [{
            "module_id": "x", "name": "x", "description": "y", "ports": [],
        }],
    })
    router = StubRouter(chosen=bad)
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "top_module_id" in str(ei.value)


def test_top_not_in_modules_rejected() -> None:
    bad = json.dumps({
        "top_module_id": "ghost",
        "modules": [{
            "module_id": "real", "name": "r", "description": "d", "ports": [],
        }],
    })
    router = StubRouter(chosen=bad)
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "ghost" in str(ei.value)


def test_duplicate_module_id_rejected() -> None:
    bad = json.dumps({
        "top_module_id": "x",
        "modules": [
            {"module_id": "x", "name": "x", "description": "d", "ports": []},
            {"module_id": "x", "name": "x", "description": "d", "ports": []},
        ],
    })
    router = StubRouter(chosen=bad)
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "duplicate" in str(ei.value).lower()


def test_dangling_dependency_rejected() -> None:
    bad = json.dumps({
        "top_module_id": "alu",
        "modules": [{
            "module_id": "alu", "name": "alu", "description": "d", "ports": [],
            "depends_on": ["ghost"],
        }],
    })
    router = StubRouter(chosen=bad)
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "ghost" in str(ei.value)


def test_self_dependency_rejected() -> None:
    bad = json.dumps({
        "top_module_id": "x",
        "modules": [{
            "module_id": "x", "name": "x", "description": "d", "ports": [],
            "depends_on": ["x"],
        }],
    })
    router = StubRouter(chosen=bad)
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "self-dependency" in str(ei.value).lower()


def test_dependency_cycle_detected() -> None:
    cycle = json.dumps({
        "top_module_id": "a",
        "modules": [
            {"module_id": "a", "name": "a", "description": "d", "ports": [],
             "depends_on": ["b"]},
            {"module_id": "b", "name": "b", "description": "d", "ports": [],
             "depends_on": ["a"]},
        ],
    })
    router = StubRouter(chosen=cycle)
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "cycle" in str(ei.value).lower()


def test_invalid_port_direction_rejected() -> None:
    bad = json.dumps({
        "top_module_id": "x",
        "modules": [{
            "module_id": "x", "name": "x", "description": "d",
            "ports": [{"name": "p", "direction": "sideways", "width": 1}],
        }],
    })
    router = StubRouter(chosen=bad)
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "direction" in str(ei.value)


def test_nonpositive_port_width_rejected() -> None:
    bad = json.dumps({
        "top_module_id": "x",
        "modules": [{
            "module_id": "x", "name": "x", "description": "d",
            "ports": [{"name": "p", "direction": "in", "width": 0}],
        }],
    })
    router = StubRouter(chosen=bad)
    agent = PlannerAgent(router=router, design_id="d0")
    with pytest.raises(PlannerError) as ei:
        agent.plan(_spec())
    assert "width" in str(ei.value)


def test_empty_design_id_rejected() -> None:
    with pytest.raises(ValueError):
        PlannerAgent(router=StubRouter(chosen=_COUNTER_JSON), design_id="")
