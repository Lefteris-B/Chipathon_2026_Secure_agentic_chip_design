"""F19.3 acceptance: ContractExtractionAgent produces a typed ContractArtifact.

The router is stubbed with canned JSON (the live-router path is exercised by
the gated end-to-end smoke tests, not here). The two AC tests pin that the
counter spec yields ``count_wraps_at_2_to_n`` / ``reset_polarity`` /
``clock_domain_count`` and that the UART RX spec yields ``baud_rate`` /
``data_bits`` / ``parity`` / ``stop_bits`` / ``oversampling_strategy``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from chip_agent.agents.contract_extraction import (
    ContractExtractionAgent,
    ContractExtractionError,
)
from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    ContractArtifact,
    DesignConstraints,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    ModuleDecl,
    Port,
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
            temperature=0.0,
            seed=None,
            prompt_tokens=320,
            completion_tokens=180,
            cost_usd=0.0045,
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
            "escalation": escalation,
            "n": n,
        })
        return GenerationResult(
            candidates=[self.chosen],
            chosen=self.chosen,
            invocation=self.invocation,
        )


# --------------------------------------------------------------------------- #
# Spec + ModuleDecl fixtures
# --------------------------------------------------------------------------- #
def _spec_counter(design_id: str = "d0") -> Spec:
    return Spec(
        artifact_id=f"{design_id}.spec",
        design_id=design_id,
        raw_text="8-bit synchronous counter, async active-low reset, enable input",
        normalized=(
            "* Module: counter\n"
            "* Ports: clk (in, 1), rst_n (in, 1), en (in, 1), q (out, 8)\n"
            "* Reset: async active-low\n"
            "* On clk rising while rst_n high and en high, q += 1 (modulo 256).\n"
        ),
        requirements=[
            "Counter increments on rising clock edge when enabled",
            "Asynchronous active-low reset clears q to 0",
            "8-bit width; wraps at 256",
        ],
        constraints=DesignConstraints(pdk="sky130A", target_clock_ns=10.0),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


def _module_counter() -> ModuleDecl:
    return ModuleDecl(
        module_id="counter",
        name="counter",
        description="8-bit synchronous up-counter with async active-low reset",
        ports=[
            Port(name="clk", direction="in", width=1, description="primary clock"),
            Port(name="rst_n", direction="in", width=1, description="async active-low reset"),
            Port(name="en", direction="in", width=1, description="sync enable"),
            Port(name="q", direction="out", width=8, description="current count value"),
        ],
    )


def _spec_uart_rx(design_id: str = "u0") -> Spec:
    return Spec(
        artifact_id=f"{design_id}.spec",
        design_id=design_id,
        raw_text="UART receiver, 115200 baud, 8N1, 16x oversampling",
        normalized=(
            "* Module: uart_rx\n"
            "* Ports: clk (in, 1), rst_n (in, 1), rx (in, 1), data (out, 8), "
            "valid (out, 1), framing_error (out, 1)\n"
            "* Baud: 115200; Frame: 8N1; Oversampling: 16x.\n"
            "* FSM: IDLE -> START_DETECT -> DATA -> STOP\n"
        ),
        requirements=[
            "Deserialise 8N1 frames at 115200 baud",
            "Oversample input 16x; sample at the middle of each bit window",
            "Pulse valid for one clk when a byte is received cleanly",
            "Pulse framing_error when stop bit is not high",
        ],
        constraints=DesignConstraints(pdk="sky130A", target_clock_ns=50.0),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


def _module_uart_rx() -> ModuleDecl:
    return ModuleDecl(
        module_id="uart_rx",
        name="uart_rx",
        description="UART receiver: 8N1 at 115200 baud, 16x oversampling",
        ports=[
            Port(name="clk", direction="in", width=1, description="16x oversampling clock"),
            Port(name="rst_n", direction="in", width=1, description="sync active-low reset"),
            Port(name="rx", direction="in", width=1, description="serial data input"),
            Port(name="data", direction="out", width=8, description="last received byte"),
            Port(name="valid", direction="out", width=1, description="byte-valid strobe"),
            Port(name="framing_error", direction="out", width=1, description="stop-bit error"),
        ],
    )


# --------------------------------------------------------------------------- #
# Canned model outputs (JSON the stub returns).
# --------------------------------------------------------------------------- #
_COUNTER_CONTRACT_JSON = json.dumps({
    "behavior_invariants": [
        {
            "name": "increment_by_one",
            "description": "On rising clk while rst_n high and en high, q advances by 1.",
            "condition": "(en && rst_n) -> next(q) == (q + 1) mod 256",
        },
        {
            "name": "reset_clears_count",
            "description": "When rst_n is low, q is held at 0 asynchronously.",
            "condition": "!rst_n -> q == 0",
        },
        {
            "name": "count_wraps_at_2_to_n",
            "description": "An 8-bit counter wraps from 255 back to 0.",
            "condition": "q == 255 && en -> next(q) == 0",
        },
    ],
    "port_assumptions": [
        {"port_name": "clk", "polarity": "positive", "encoding": "n/a",
         "notes": "rising edge"},
        {"port_name": "q", "expected_range": [0, 255], "encoding": "binary",
         "polarity": "n/a"},
    ],
    "clock_domains": [
        {"name": "clk", "frequency_mhz": 100.0, "source": "external",
         "notes": "single domain"},
    ],
    "reset": {
        "name": "rst_n",
        "polarity": "active_low",
        "synchronicity": "async",
        "affects": ["q"],
    },
    "encoding": {"is_pipelined": "false", "fsm_style": "n/a"},
    "ambiguity_notes": [
        "spec did not explicitly mention overflow handling; assumed wrap modulo 256",
    ],
})


_UART_RX_CONTRACT_JSON = json.dumps({
    "behavior_invariants": [
        {
            "name": "start_bit_detected_on_falling_edge",
            "description": "FSM advances from IDLE on a falling edge on rx.",
            "condition": "idle && rx == 0 -> next(state) == START_DETECT",
        },
        {
            "name": "valid_pulses_after_clean_frame",
            "description": "When the stop bit is high at sample time, valid pulses for one clk.",
            "condition": "stop && rx == 1 -> valid for one clk",
        },
    ],
    "port_assumptions": [
        {"port_name": "rx", "polarity": "n/a", "encoding": "binary",
         "notes": "idle high"},
        {"port_name": "data", "expected_range": [0, 255], "encoding": "binary",
         "polarity": "n/a"},
    ],
    "clock_domains": [
        {"name": "clk", "frequency_mhz": 1.8432, "source": "external",
         "notes": "16x baud (115200 * 16 ~= 1.8432 MHz)"},
    ],
    "reset": {
        "name": "rst_n",
        "polarity": "active_low",
        "synchronicity": "sync",
        "affects": ["state", "valid", "framing_error"],
    },
    "encoding": {
        "baud_rate": "115200",
        "data_bits": "8",
        "parity": "none",
        "stop_bits": "1",
        "oversampling_strategy": "16x_middle_sample",
        "fsm_style": "binary",
    },
    "ambiguity_notes": [
        "spec did not specify behaviour on rx noise during the start bit; "
        "assumed mid-cell resample confirms",
    ],
})


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_extract_returns_contract_artifact_for_counter() -> None:
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    contract = agent.extract(_spec_counter(), _module_counter())

    assert isinstance(contract, ContractArtifact)
    assert contract.kind is ArtifactKind.CONTRACT
    assert contract.module_id == "counter"
    assert contract.design_id == "d0"
    assert contract.artifact_id == "d0.counter.contract"


def test_counter_contract_has_count_wraps_invariant() -> None:
    """F19.3 AC #1a: counter contract surfaces the count_wraps_at_2_to_n
    behaviour invariant by name."""
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    contract = agent.extract(_spec_counter(), _module_counter())

    names = {inv.name for inv in contract.behavior_invariants}
    assert "count_wraps_at_2_to_n" in names


def test_counter_contract_has_reset_polarity_populated() -> None:
    """F19.3 AC #1b: counter contract carries a concrete reset polarity."""
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    contract = agent.extract(_spec_counter(), _module_counter())

    assert contract.reset is not None
    assert contract.reset.polarity == "active_low"
    assert contract.reset.synchronicity == "async"


def test_counter_contract_has_at_least_one_clock_domain() -> None:
    """F19.3 AC #1c: contract carries the clock domain (clock_domain_count
    populated)."""
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    contract = agent.extract(_spec_counter(), _module_counter())

    assert len(contract.clock_domains) >= 1
    assert contract.clock_domains[0].name == "clk"


def test_uart_rx_contract_carries_protocol_parameters_in_encoding() -> None:
    """F19.3 AC #2: UART RX contract surfaces baud_rate, data_bits, parity,
    stop_bits, and oversampling_strategy in the encoding dict."""
    router = StubRouter(chosen=_UART_RX_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="u0")
    contract = agent.extract(_spec_uart_rx(), _module_uart_rx())

    for key in ("baud_rate", "data_bits", "parity", "stop_bits", "oversampling_strategy"):
        assert key in contract.encoding, f"missing {key!r} in {contract.encoding!r}"
    assert contract.encoding["baud_rate"] == "115200"
    assert contract.encoding["data_bits"] == "8"


def test_extract_dispatches_to_contract_extraction_task() -> None:
    """Plumbing: the agent must dispatch to TaskType.CONTRACT_EXTRACTION
    so the new task binding in configs/*.yaml is what gets resolved."""
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    agent.extract(_spec_counter(), _module_counter())

    assert len(router.calls) == 1
    assert router.calls[0]["task"] == TaskType.CONTRACT_EXTRACTION


def test_extract_inlines_spec_and_module_in_user_prompt() -> None:
    """The user prompt must carry the spec's normalised text and the module
    shape — otherwise the model can't extract a contract for THIS module
    rather than some other one in the design."""
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    spec = _spec_counter()
    module = _module_counter()
    agent.extract(spec, module)

    prompt = router.calls[0]["context"]["prompt"]
    assert "8-bit width" in prompt or "modulo 256" in prompt  # from normalized
    assert module.name in prompt
    assert "rst_n" in prompt  # one of the port names


def test_extract_passes_system_prompt_to_router() -> None:
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    agent.extract(_spec_counter(), _module_counter())

    system = router.calls[0]["context"]["system"]
    assert system == ContractExtractionAgent.SYSTEM_PROMPT


def test_extract_records_provenance_with_spec_and_plan_refs() -> None:
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    spec = _spec_counter()

    # Without plan_ref: provenance carries only the spec ref.
    contract = agent.extract(spec, _module_counter())
    assert len(contract.provenance.inputs) == 1
    assert contract.provenance.inputs[0].artifact_id == "d0.spec"
    assert contract.provenance.agent == "contract_extractor"
    assert contract.provenance.produced_by is Stage.PLAN
    assert contract.provenance.model is not None
    assert contract.provenance.model.model == "claude-opus-4-7"

    # With plan_ref: both refs are recorded in input order.
    plan_ref = ArtifactRef(
        artifact_id="d0.plan", version=1, kind=ArtifactKind.PLAN,
        content_hash="sha256:" + "f" * 64,
    )
    contract2 = agent.extract(spec, _module_counter(), plan_ref=plan_ref)
    assert [r.artifact_id for r in contract2.provenance.inputs] == ["d0.spec", "d0.plan"]


def test_extract_raises_on_invalid_json() -> None:
    router = StubRouter(chosen="this is not json {")
    agent = ContractExtractionAgent(router=router, design_id="d0")
    with pytest.raises(ContractExtractionError, match="not valid JSON"):
        agent.extract(_spec_counter(), _module_counter())


def test_extract_raises_on_invalid_polarity() -> None:
    """Pydantic Literal validation must surface as ContractExtractionError —
    the agent's responsibility to wrap so callers get a uniform error
    class to catch."""
    bogus = json.dumps({
        "behavior_invariants": [],
        "port_assumptions": [],
        "clock_domains": [],
        "reset": {
            "name": "rst",
            "polarity": "active_pancake",
            "synchronicity": "sync",
            "affects": [],
        },
        "encoding": {},
        "ambiguity_notes": [],
    })
    router = StubRouter(chosen=bogus)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    with pytest.raises(ContractExtractionError, match="reset"):
        agent.extract(_spec_counter(), _module_counter())


def test_extract_handles_optional_reset_being_null() -> None:
    """A combinational module has no reset — null must round-trip to
    ``contract.reset is None``."""
    payload = json.dumps({
        "behavior_invariants": [
            {"name": "and_truth_table",
             "description": "y is the conjunction of a and b.",
             "condition": "y == a && b"},
        ],
        "port_assumptions": [],
        "clock_domains": [],
        "reset": None,
        "encoding": {},
        "ambiguity_notes": [],
    })
    router = StubRouter(chosen=payload)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    contract = agent.extract(_spec_counter(), _module_counter())
    assert contract.reset is None


def test_extract_strips_json_fence_markers() -> None:
    """Some models wrap JSON in ```json fences; the parser must strip them."""
    fenced = "```json\n" + _COUNTER_CONTRACT_JSON + "\n```"
    router = StubRouter(chosen=fenced)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    contract = agent.extract(_spec_counter(), _module_counter())
    assert any(inv.name == "count_wraps_at_2_to_n"
               for inv in contract.behavior_invariants)


def test_extract_raises_on_design_id_mismatch() -> None:
    router = StubRouter(chosen=_COUNTER_CONTRACT_JSON)
    agent = ContractExtractionAgent(router=router, design_id="d0")
    other_spec = _spec_counter(design_id="d1")
    with pytest.raises(ContractExtractionError, match="design_id"):
        agent.extract(other_spec, _module_counter())
