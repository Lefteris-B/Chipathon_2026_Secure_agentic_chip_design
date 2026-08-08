"""F4.1 acceptance: a counter spec yields ports / width / reset in the normalised spec."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from chip_agent.agents.spec_intake import SpecIntakeAgent, SpecIntakeError
from chip_agent.design_state import (
    ArtifactKind,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    Spec,
    Stage,
    TaskType,
)
from chip_agent.settings import ConstraintDefaults


# --------------------------------------------------------------------------- #
# StubRouter — satisfies the ModelRouter Protocol with canned output.
# --------------------------------------------------------------------------- #
@dataclass
class StubRouter:
    chosen: str
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="ollama",
            model="qwen2.5-coder:7b",
            temperature=0.0,
            seed=None,
            prompt_tokens=80,
            completion_tokens=40,
            cost_usd=0.0,
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
            "task": task,
            "context": dict(context),
            "failure": failure,
            "escalation": escalation,
            "n": n,
        })
        return GenerationResult(
            candidates=[self.chosen],
            chosen=self.chosen,
            invocation=self.invocation,
        )


_COUNTER_NORMALISED = """\
* Ports:
  - clk: input, 1 bit (clock signal)
  - rst: input, 1 bit (active-high synchronous reset)
  - q:   output, 4 bits (current count)
* Width: 4 bits
* Reset: active-high synchronous reset
* Clock: target clock period 10 ns
* The counter increments by 1 on every rising clock edge when rst is low.
"""


# --------------------------------------------------------------------------- #
# AC: counter spec yields ports / width / reset in the normalised spec.
# --------------------------------------------------------------------------- #
def test_counter_spec_yields_ports_width_reset() -> None:
    router = StubRouter(chosen=_COUNTER_NORMALISED)
    agent = SpecIntakeAgent(router=router, design_id="d0")

    spec = agent.intake(
        "I need a 4-bit synchronous counter with an active-high reset, "
        "running at 100 MHz."
    )

    assert isinstance(spec, Spec)
    assert spec.kind is ArtifactKind.SPEC
    # The AC: ports / width / reset are explicitly named in the normalised spec.
    n = spec.normalized.lower()
    assert "port" in n
    assert "width" in n
    assert "reset" in n
    # Counter-specific: the four-bit width and the three port names landed.
    assert "4 bit" in n or "4-bit" in n
    assert "clk" in n
    assert "rst" in n
    assert "q" in n


# --------------------------------------------------------------------------- #
# Router plumbing
# --------------------------------------------------------------------------- #
def test_router_called_with_spec_intake_task_and_system_prompt() -> None:
    router = StubRouter(chosen=_COUNTER_NORMALISED)
    agent = SpecIntakeAgent(router=router, design_id="d0")
    agent.intake("4-bit counter")
    assert len(router.calls) == 1
    call = router.calls[0]
    assert call["task"] is TaskType.SPEC_INTAKE
    assert call["context"]["prompt"] == "4-bit counter"
    assert call["context"]["system"] == SpecIntakeAgent.SYSTEM_PROMPT


def test_custom_system_prompt_overrides_default() -> None:
    router = StubRouter(chosen="* Ports: clk")
    agent = SpecIntakeAgent(router=router, design_id="d0")
    agent.intake("x", system_prompt="custom")
    assert router.calls[0]["context"]["system"] == "custom"


# --------------------------------------------------------------------------- #
# Spec field population
# --------------------------------------------------------------------------- #
def test_raw_text_preserved_verbatim() -> None:
    raw = "I need a counter."
    agent = SpecIntakeAgent(router=StubRouter(chosen=_COUNTER_NORMALISED), design_id="d0")
    spec = agent.intake(raw)
    assert spec.raw_text == raw


def test_normalized_is_stripped() -> None:
    router = StubRouter(chosen="   * Ports: clk   \n")
    agent = SpecIntakeAgent(router=router, design_id="d0")
    spec = agent.intake("x")
    assert spec.normalized == "* Ports: clk"


def test_design_id_flows_into_artifact_id() -> None:
    router = StubRouter(chosen=_COUNTER_NORMALISED)
    agent = SpecIntakeAgent(router=router, design_id="counter4bit")
    spec = agent.intake("x")
    assert spec.design_id == "counter4bit"
    assert spec.artifact_id == "counter4bit.spec"


def test_provenance_records_model_invocation() -> None:
    invocation = ModelInvocation(
        provider="ollama", model="qwen2.5-coder:7b",
        temperature=0.0, seed=None, prompt_tokens=80,
        completion_tokens=40, cost_usd=0.0,
    )
    router = StubRouter(chosen=_COUNTER_NORMALISED, invocation=invocation)
    agent = SpecIntakeAgent(router=router, design_id="d0")
    spec = agent.intake("4-bit counter")
    assert spec.provenance.produced_by is Stage.SPEC
    assert spec.provenance.agent == "spec_intake"
    assert spec.provenance.model == invocation


# --------------------------------------------------------------------------- #
# Lightweight post-processing pass: requirements + constraints from the
# normalised text. Defaults fill anything the model didn't state.
# --------------------------------------------------------------------------- #
def test_requirements_extracted_from_bulleted_output() -> None:
    body = (
        "* Ports: clk, rst, q\n"
        "* Width: 4 bits\n"
        "* Reset: synchronous active-high\n"
        "- The counter increments on the rising clock edge.\n"
        "Some prose without a bullet.\n"
        "1. The counter wraps to zero on overflow.\n"
    )
    agent = SpecIntakeAgent(router=StubRouter(chosen=body), design_id="d0")
    spec = agent.intake("x")
    assert any("Ports" in r for r in spec.requirements)
    assert any("rising clock edge" in r for r in spec.requirements)
    assert any("overflow" in r for r in spec.requirements)
    assert not any("Some prose" in r for r in spec.requirements)


def test_target_clock_extracted_from_normalised_text() -> None:
    agent = SpecIntakeAgent(router=StubRouter(chosen=_COUNTER_NORMALISED), design_id="d0")
    spec = agent.intake("x")
    assert spec.constraints.target_clock_ns == 10.0


def test_die_area_extracted_when_present() -> None:
    body = "* Ports: clk\n* Max die area: 1234.5 um^2\n"
    agent = SpecIntakeAgent(router=StubRouter(chosen=body), design_id="d0")
    spec = agent.intake("x")
    assert spec.constraints.max_die_area_um2 == 1234.5


def test_utilisation_extracted_as_fraction() -> None:
    # "70 %" -> 0.70 (the schema stores a fraction).
    body = "* Ports: clk\n* Target utilisation: 70%\n"
    agent = SpecIntakeAgent(router=StubRouter(chosen=body), design_id="d0")
    spec = agent.intake("x")
    assert spec.constraints.target_utilization == 0.7


def test_utilisation_accepts_fractional_input() -> None:
    body = "* Ports: clk\n* Target utilization 0.65\n"
    agent = SpecIntakeAgent(router=StubRouter(chosen=body), design_id="d0")
    spec = agent.intake("x")
    assert spec.constraints.target_utilization == 0.65


def test_constraint_defaults_applied_when_unstated() -> None:
    body = "* Ports: clk\n* Width: 8 bits\n"  # no clock / area / utilisation
    defaults = ConstraintDefaults(
        pdk="sky130A",
        std_cell_lib="sky130_fd_sc_hd",
        target_clock_ns=12.5,
        max_die_area_um2=2000.0,
        target_utilization=0.55,
    )
    agent = SpecIntakeAgent(
        router=StubRouter(chosen=body), design_id="d0", defaults=defaults,
    )
    spec = agent.intake("x")
    assert spec.constraints.pdk == "sky130A"
    assert spec.constraints.std_cell_lib == "sky130_fd_sc_hd"
    assert spec.constraints.target_clock_ns == 12.5
    assert spec.constraints.max_die_area_um2 == 2000.0
    assert spec.constraints.target_utilization == 0.55


def test_normalised_text_overrides_defaults() -> None:
    body = "* Ports: clk\n* target clock 8 ns\n"
    defaults = ConstraintDefaults(target_clock_ns=12.5)
    agent = SpecIntakeAgent(
        router=StubRouter(chosen=body), design_id="d0", defaults=defaults,
    )
    spec = agent.intake("x")
    assert spec.constraints.target_clock_ns == 8.0


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #
def test_empty_raw_text_rejected() -> None:
    agent = SpecIntakeAgent(router=StubRouter(chosen="x"), design_id="d0")
    with pytest.raises(SpecIntakeError):
        agent.intake("")
    with pytest.raises(SpecIntakeError):
        agent.intake("   \n  ")


def test_empty_router_output_rejected() -> None:
    agent = SpecIntakeAgent(router=StubRouter(chosen="   "), design_id="d0")
    with pytest.raises(SpecIntakeError):
        agent.intake("4-bit counter")


def test_empty_design_id_rejected() -> None:
    with pytest.raises(ValueError):
        SpecIntakeAgent(router=StubRouter(chosen="x"), design_id="")
