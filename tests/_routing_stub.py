"""Shared test infrastructure: stub :class:`CompletionBackend`.

F10.1 made ``cmd_run`` always require a real :class:`LiteLLMRouter`. Tests
that exercise the CLI now build a real router over a real
:class:`LiteLLMGateway` over a **stub backend** — keeping the routing /
policy / loop code paths in scope for every test while staying free of
network calls.

The backend dispatches on the system prompt the agent sent so the same
backend instance can serve every agent the demo exercises (SpecIntake,
Planner, RTLGen, RTLRepair, TBGen).
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from chip_agent.routing.gateway import (
    CompletionResult,
    CompletionStreamChunk,
    LiteLLMGateway,
)
from chip_agent.routing.router import LiteLLMRouter
from chip_agent.settings import Settings

__all__ = [
    "ALU_4BIT_PLAN_JSON",
    "ALU_4BIT_RESPONSES",
    "ALU_4BIT_RTL",
    "ALU_4BIT_SPEC_NORMALISED",
    "CHAT_RESPONSES",
    "COUNTER_ASSERTIONS_PY",
    "COUNTER_CONTRACT_JSON",
    "COUNTER_ORACLE_PY",
    "COUNTER_PLAN_JSON",
    "COUNTER_RTL",
    "COUNTER_SIGNAL_DEFINITIONS_JSON",
    "COUNTER_SIGNAL_MAP_JSON",
    "COUNTER_SPEC_NORMALISED",
    "DECODER_3TO8_PLAN_JSON",
    "DECODER_3TO8_RESPONSES",
    "DECODER_3TO8_RTL",
    "DECODER_3TO8_SPEC_NORMALISED",
    "DEFAULT_RESPONSES",
    "FSM_TRAFFIC_LIGHT_PLAN_JSON",
    "FSM_TRAFFIC_LIGHT_RESPONSES",
    "FSM_TRAFFIC_LIGHT_RTL",
    "FSM_TRAFFIC_LIGHT_SPEC_NORMALISED",
    "SHIFT_REGISTER_PLAN_JSON",
    "SHIFT_REGISTER_RESPONSES",
    "SHIFT_REGISTER_RTL",
    "SHIFT_REGISTER_SPEC_NORMALISED",
    "StubBackend",
    "make_routing_config",
    "make_test_router",
]


# --------------------------------------------------------------------------- #
# Canned responses for the counter demo (and shape-identical specs).
# --------------------------------------------------------------------------- #
COUNTER_SPEC_NORMALISED = """\
* Ports:
  - clk: input, 1 bit (primary clock)
  - rst_n: input, 1 bit (asynchronous active-low reset)
  - en: input, 1 bit (count enable)
  - q: output, 8 bits (current count value)
* Reset: asynchronous active-low; q clears to 0 when rst_n is low
* Clock: target clock period 10 ns
* Target utilisation: 30%
* PDK: gf180mcuD
* Behaviour: q increments by 1 on rising clk when rst_n is high and en is high
"""


COUNTER_PLAN_JSON = """\
{
  "top_module_id": "counter",
  "modules": [
    {
      "module_id": "counter",
      "name": "counter",
      "description": "8-bit synchronous up-counter with async active-low reset",
      "ports": [
        {"name": "clk",   "direction": "in",  "width": 1, "description": "primary clock"},
        {"name": "rst_n", "direction": "in",  "width": 1, "description": "async active-low reset"},
        {"name": "en",    "direction": "in",  "width": 1, "description": "count enable"},
        {"name": "q",     "direction": "out", "width": 8, "description": "current count value"}
      ],
      "params": {},
      "depends_on": []
    }
  ],
  "rationale": "single-module counter demo"
}
"""


# Verilog body matches the F9.5 stub shape so test fixtures that previously
# pinned the stub-router's output keep their content semantics.
COUNTER_RTL = """\
module counter (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    output reg  [7:0]  q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            q <= 8'd0;
        else if (en)
            q <= q + 8'd1;
    end
endmodule
"""


# --------------------------------------------------------------------------- #
# Canned responses for the F10.5 shift-register spec (used by the
# ``test_shift_register_demo`` suite + any spec-shape generality test).
# --------------------------------------------------------------------------- #
SHIFT_REGISTER_SPEC_NORMALISED = """\
* Ports:
  - clk: input, 1 bit (primary clock)
  - rst_n: input, 1 bit (asynchronous active-low reset)
  - serial_in: input, 1 bit (new bit shifted in on every clock)
  - q: output, 4 bits (current shift-register contents)
* Reset: asynchronous active-low; q clears to 0 when rst_n is low
* Clock: target clock period 10 ns
* Target utilisation: 30%
* PDK: gf180mcuD
* Behaviour: on rising clk, q shifts left by one — q[3:1] takes the
  previous q[2:0] and q[0] takes serial_in
"""


SHIFT_REGISTER_PLAN_JSON = """\
{
  "top_module_id": "shift_register",
  "modules": [
    {
      "module_id": "shift_register",
      "name": "shift_register",
      "description": "4-bit synchronous shift register with async active-low reset",
      "ports": [
        {"name": "clk",       "direction": "in",  "width": 1, "description": "primary clock"},
        {"name": "rst_n",     "direction": "in",  "width": 1, "description": "async active-low reset"},
        {"name": "serial_in", "direction": "in",  "width": 1, "description": "serial input bit"},
        {"name": "q",         "direction": "out", "width": 4, "description": "shift register state"}
      ],
      "params": {},
      "depends_on": []
    }
  ],
  "rationale": "single-module shift-register demo"
}
"""


SHIFT_REGISTER_RTL = """\
module shift_register (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        serial_in,
    output reg  [3:0]  q
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            q <= 4'd0;
        else
            q <= {q[2:0], serial_in};
    end
endmodule
"""


# --------------------------------------------------------------------------- #
# F19.7 — canned M19 Phase 1 responses (counter). The four agents wired
# between PLAN and RTL each need a stub response. These are taken from
# the F19.3-5 unit-test fixtures (the same shape AssertLLM emits) and
# are warning-only: the gate verdict that downstream verification
# produces is recorded but never blocks RTL.
#
# Non-counter demos reuse the same fixtures — assertion / oracle code
# is keyed off port names (``q``, ``en``, ``rst_n``) that won't be
# present in e.g. the FSM module, so the gate ends up reporting
# ``gate_ok=False`` informationally. That's fine: F19.10 will tighten.
# --------------------------------------------------------------------------- #
COUNTER_CONTRACT_JSON = _json.dumps({
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


COUNTER_ORACLE_PY = '''\
# rationale: counter wraps modulo 256 per contract ambiguity_notes
# rationale: rst_n is active-low and async — q snaps to 0 the instant it deasserts

def reference(stim):
    q = 0
    out = []
    for cyc in stim:
        if cyc.get("rst_n", 1) == 0:
            q = 0
        elif cyc.get("en", 0) == 1:
            q = (q + 1) % 256
        out.append({"q": q})
    return out
'''


COUNTER_SIGNAL_MAP_JSON = _json.dumps({
    "signals": [
        {"name": "clk", "direction": "in", "width": 1, "role": "clock"},
        {"name": "rst_n", "direction": "in", "width": 1, "role": "reset"},
        {"name": "en", "direction": "in", "width": 1, "role": "enable"},
        {"name": "q", "direction": "out", "width": 8, "role": "count"},
    ],
    "referenced_invariants": [
        "reset_clears_count",
        "increment_by_one",
        "count_wraps_at_2_to_n",
    ],
})


COUNTER_SIGNAL_DEFINITIONS_JSON = _json.dumps({
    "definitions": [
        {
            "signal_name": "rst_n",
            "semantic_role": "active-low async reset; forces q to 0",
            "constraints": [],
        },
        {
            "signal_name": "en",
            "semantic_role": "synchronous increment enable",
            "constraints": [],
        },
        {
            "signal_name": "q",
            "semantic_role": "8-bit unsigned count value",
            "constraints": ["range [0, 255]", "wraps modulo 256"],
        },
    ],
})


COUNTER_ASSERTIONS_PY = '''\
# rationale: counter assertions check reset / increment / wrap per contract
# rationale: each function takes (stim, observed) and returns (passed, message)

def assert_reset_clears_count(args):
    """When rst_n is low, q must be 0 on that cycle."""
    stim, observed = args
    for s, o in zip(stim, observed):
        if s.get("rst_n", 1) == 0 and o.get("q", 0) != 0:
            return (False, f"rst_n low but q={o['q']}")
    return (True, "reset always clears q")


def assert_increment_by_one(args):
    """When rst_n high and en high, q advances by 1 each cycle."""
    stim, observed = args
    prev_q = None
    for s, o in zip(stim, observed):
        if s.get("rst_n", 1) == 1 and s.get("en", 0) == 1 and prev_q is not None:
            expected = (prev_q + 1) % 256
            if o.get("q", 0) != expected:
                return (False, f"expected q={expected}, got {o['q']}")
        prev_q = o.get("q", 0)
    return (True, "increment behaviour holds")


def assert_count_wraps_at_2_to_n(args):
    """When q is 255 and en is high, next q must be 0."""
    stim, observed = args
    prev_q = None
    for s, o in zip(stim, observed):
        if (prev_q == 255 and s.get("en", 0) == 1
                and s.get("rst_n", 1) == 1
                and o.get("q", 0) != 0):
            return (False, f"q was 255 but next q was {o['q']}, not 0")
        prev_q = o.get("q", 0)
    return (True, "wrap behaviour holds")
'''


@dataclass(frozen=True)
class _PromptMatcher:
    """Matches by a substring of the system prompt."""

    needle: str
    response: str
    prompt_tokens: int = 100
    completion_tokens: int = 60
    cost_usd: float = 0.0
    # F11.3: how to break ``response`` into stream chunks when ``StubBackend.stream``
    # is exercised. Defaults to a single terminal chunk carrying the whole text.
    stream_chunks: tuple[str, ...] | None = None
    # F11.4: cycle through a sequence of completion responses (one per
    # successive match). The i-th call gets ``response_sequence[i]``;
    # after the sequence is exhausted, every further call falls back to
    # ``response``. Lets tests drive multi-turn intake against the same
    # SpecIntake matcher.
    response_sequence: tuple[str, ...] | None = None


# The default response set the counter demo + most CLI tests rely on. Each
# entry's ``needle`` is a substring of the corresponding agent's
# SYSTEM_PROMPT — this is what dispatches a call to the right canned text.
_COUNTER_ORACLE_PLAN_JSON = _json.dumps({
    "is_clocked": True,
    "step_summary": (
        "On each rising clk: if rst_n is low, q stays at 0; else if en is "
        "high, q increments by 1 modulo 256; else q holds its previous value."
    ),
    "reset_summary": (
        "rst_n is asynchronous active-low; while it is asserted, q is "
        "forced to 0 immediately."
    ),
    "key_state": ["q"],
    "worked_example": [
        {"cycle_index": 0, "stim": {"rst_n": 0, "en": 0}, "expected_output": {"q": 0}},
        {"cycle_index": 1, "stim": {"rst_n": 1, "en": 0}, "expected_output": {"q": 0}},
        {"cycle_index": 2, "stim": {"rst_n": 1, "en": 1}, "expected_output": {"q": 1}},
        {"cycle_index": 3, "stim": {"rst_n": 1, "en": 1}, "expected_output": {"q": 2}},
        {"cycle_index": 4, "stim": {"rst_n": 1, "en": 1}, "expected_output": {"q": 3}},
    ],
})


# F21.3: the physical-repair routing agent fires whenever SIGNOFF closes
# with a recoverable timing failure AND the router is wired. cli_stubs
# instantiates the agent unconditionally (it isn't gated by
# use_test_first_workflow), so EVERY matcher tuple needs to know how to
# respond. Default behaviour preserves today's "SIGNOFF failure escalates
# to HUMAN" semantics: the agent picks ESCALATE_HUMAN, the dispatcher
# routes to the human gate, and existing tests that exercise the
# SIGNOFF-fail path stay green.
_F21_3_PHYSICAL_REPAIR_ESCALATE: _PromptMatcher = _PromptMatcher(
    needle="You are a chip-design physical-flow repair router",
    response=(
        '{"route": "escalate_human", '
        '"reason": "stub backend: default escalation preserves pre-F21.3 semantics"}'
    ),
)


_M19_COUNTER_MATCHERS: tuple[_PromptMatcher, ...] = (
    _PromptMatcher(
        needle="You are a chip-spec contract extractor",
        response=COUNTER_CONTRACT_JSON,
    ),
    # F19.4b: plan stage canned response — counter-shaped per-cycle plan
    # whose worked_example matches the counter oracle Python in
    # ``COUNTER_ORACLE_PY``. Demo runs hit the plan stage first; the
    # JSON validates, the code stage's self-check passes, and the
    # OracleArtifact content hash is unchanged (plan lives in
    # ``rationale_notes`` which is in ``_NON_CONTENT_FIELDS``).
    _PromptMatcher(
        needle="You are a chip-design REFERENCE MODEL planner",
        response=_COUNTER_ORACLE_PLAN_JSON,
    ),
    _PromptMatcher(
        needle="You are a chip-design REFERENCE MODEL writer",
        response=COUNTER_ORACLE_PY,
    ),
    _PromptMatcher(
        needle="You are the EXTRACT stage of a three-stage assertion-generation",
        response=COUNTER_SIGNAL_MAP_JSON,
    ),
    _PromptMatcher(
        needle="You are the MAP stage of a three-stage assertion-generation",
        response=COUNTER_SIGNAL_DEFINITIONS_JSON,
    ),
    _PromptMatcher(
        needle="You are the TRANSLATE stage of a three-stage assertion-generation",
        response=COUNTER_ASSERTIONS_PY,
    ),
)


DEFAULT_RESPONSES: tuple[_PromptMatcher, ...] = (
    _PromptMatcher(
        needle="You normalise natural-language chip-module specs",
        response=COUNTER_SPEC_NORMALISED,
    ),
    _PromptMatcher(
        needle="You are a chip-design planner",
        response=COUNTER_PLAN_JSON,
    ),
    _PromptMatcher(
        needle="You are a synthesisable Verilog/SystemVerilog generator",
        response=COUNTER_RTL,
    ),
    _PromptMatcher(
        needle="You are a Verilog/SystemVerilog repair tool",
        response=COUNTER_RTL,
    ),
    _PromptMatcher(
        needle="You are a Verilog/SystemVerilog semantic-repair tool",
        response=COUNTER_RTL,
    ),
    _PromptMatcher(
        needle="You generate cocotb (Python) testbenches",
        response="# templated path covers the counter; this is the LLM fallback\n",
    ),
    *_M19_COUNTER_MATCHERS,
    _F21_3_PHYSICAL_REPAIR_ESCALATE,
)


# F10.5: the shift-register variant of the default matchers. Tests for
# ``specs/shift_register.md`` pass this tuple via ``StubBackend(matchers=...)``
# so the agents see the right shape for the design they're driving.
# F11.3: matchers for the chat REPL. The chat-persona system prompt is
# distinct from SpecIntakeAgent's; on ``/run`` the same backend has to
# serve the SpecIntake call too, so we include both matchers.
CHAT_RESPONSES: tuple[_PromptMatcher, ...] = (
    _PromptMatcher(
        needle="You are a chip-design intake assistant",
        response="What clock period and reset polarity do you want?",
        stream_chunks=(
            "What clock period ",
            "and reset polarity ",
            "do you want?",
        ),
    ),
    _PromptMatcher(
        needle="You normalise natural-language chip-module specs",
        response=COUNTER_SPEC_NORMALISED,
    ),
    _PromptMatcher(
        needle="You are a chip-design planner",
        response=COUNTER_PLAN_JSON,
    ),
    _PromptMatcher(
        needle="You are a synthesisable Verilog/SystemVerilog generator",
        response=COUNTER_RTL,
    ),
    _PromptMatcher(
        needle="You are a Verilog/SystemVerilog repair tool",
        response=COUNTER_RTL,
    ),
    _PromptMatcher(
        needle="You are a Verilog/SystemVerilog semantic-repair tool",
        response=COUNTER_RTL,
    ),
    _PromptMatcher(
        needle="You generate cocotb (Python) testbenches",
        response="# templated path covers the counter; this is the LLM fallback\n",
    ),
    *_M19_COUNTER_MATCHERS,
    _F21_3_PHYSICAL_REPAIR_ESCALATE,
)


SHIFT_REGISTER_RESPONSES: tuple[_PromptMatcher, ...] = (
    _PromptMatcher(
        needle="You normalise natural-language chip-module specs",
        response=SHIFT_REGISTER_SPEC_NORMALISED,
    ),
    _PromptMatcher(
        needle="You are a chip-design planner",
        response=SHIFT_REGISTER_PLAN_JSON,
    ),
    _PromptMatcher(
        needle="You are a synthesisable Verilog/SystemVerilog generator",
        response=SHIFT_REGISTER_RTL,
    ),
    _PromptMatcher(
        needle="You are a Verilog/SystemVerilog repair tool",
        response=SHIFT_REGISTER_RTL,
    ),
    _PromptMatcher(
        needle="You are a Verilog/SystemVerilog semantic-repair tool",
        response=SHIFT_REGISTER_RTL,
    ),
    _PromptMatcher(
        needle="You generate cocotb (Python) testbenches",
        response="# templated path covers the shift-register; this is the LLM fallback\n",
    ),
    *_M19_COUNTER_MATCHERS,
    _F21_3_PHYSICAL_REPAIR_ESCALATE,
)


# --------------------------------------------------------------------------- #
# F12.6 canned responses for the FSM / ALU / decoder spec shapes. Each
# tuple drives one spec end-to-end through the stub backend so the F12.6
# demo + closure-pinned tests don't need a live model.
# --------------------------------------------------------------------------- #
FSM_TRAFFIC_LIGHT_SPEC_NORMALISED = """\
* Ports:
  - clk: input, 1 bit (primary clock)
  - rst_n: input, 1 bit (asynchronous active-low reset)
  - tick: input, 1 bit (advance signal)
  - state: output, 2 bits (current encoded state)
* Reset: asynchronous active-low; state clears to 2'b00 (RED)
* Clock: target clock period 10 ns
* Target utilisation: 30%
* PDK: gf180mcuD
* Behaviour: three-state Moore FSM RED -> GREEN -> YELLOW -> RED, advances
  one state per asserted tick at posedge clk
"""


FSM_TRAFFIC_LIGHT_PLAN_JSON = """\
{
  "top_module_id": "fsm_traffic_light",
  "modules": [
    {
      "module_id": "fsm_traffic_light",
      "name": "fsm_traffic_light",
      "description": "3-state Moore traffic-light FSM with async active-low reset",
      "ports": [
        {"name": "clk",   "direction": "in",  "width": 1, "description": "primary clock"},
        {"name": "rst_n", "direction": "in",  "width": 1, "description": "async active-low reset"},
        {"name": "tick",  "direction": "in",  "width": 1, "description": "advance signal"},
        {"name": "state", "direction": "out", "width": 2, "description": "current encoded state"}
      ],
      "params": {},
      "depends_on": []
    }
  ],
  "rationale": "single-module Moore FSM demo"
}
"""


FSM_TRAFFIC_LIGHT_RTL = """\
module fsm_traffic_light (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        tick,
    output reg  [1:0]  state
);
    localparam [1:0] RED    = 2'b00;
    localparam [1:0] GREEN  = 2'b01;
    localparam [1:0] YELLOW = 2'b10;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            state <= RED;
        else if (tick) begin
            case (state)
                RED:    state <= GREEN;
                GREEN:  state <= YELLOW;
                YELLOW: state <= RED;
                default: state <= RED;
            endcase
        end
    end
endmodule
"""


ALU_4BIT_SPEC_NORMALISED = """\
* Ports:
  - a: input, 4 bits (first operand)
  - b: input, 4 bits (second operand)
  - op: input, 2 bits (operation selector)
  - result: output, 4 bits (computed result)
  - cout: output, 1 bit (carry-out, meaningful only for ADD)
* Reset: none (purely combinational)
* Clock: none (purely combinational); 10 ns STA reference
* Target utilisation: 30%
* PDK: gf180mcuD
* Behaviour: op == 00 -> a+b with cout; 01 -> a-b; 10 -> a&b; 11 -> a|b
"""


ALU_4BIT_PLAN_JSON = """\
{
  "top_module_id": "alu_4bit",
  "modules": [
    {
      "module_id": "alu_4bit",
      "name": "alu_4bit",
      "description": "4-bit combinational ALU with ADD/SUB/AND/OR ops",
      "ports": [
        {"name": "a",      "direction": "in",  "width": 4, "description": "first operand"},
        {"name": "b",      "direction": "in",  "width": 4, "description": "second operand"},
        {"name": "op",     "direction": "in",  "width": 2, "description": "op selector"},
        {"name": "result", "direction": "out", "width": 4, "description": "computed result"},
        {"name": "cout",   "direction": "out", "width": 1, "description": "carry-out"}
      ],
      "params": {},
      "depends_on": []
    }
  ],
  "rationale": "single-module combinational ALU demo"
}
"""


ALU_4BIT_RTL = """\
module alu_4bit (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire [1:0] op,
    output reg  [3:0] result,
    output reg        cout
);
    reg [4:0] sum_ext;
    always @* begin
        sum_ext = {1'b0, a} + {1'b0, b};
        case (op)
            2'b00: begin result = sum_ext[3:0]; cout = sum_ext[4]; end
            2'b01: begin result = a - b;        cout = 1'b0; end
            2'b10: begin result = a & b;        cout = 1'b0; end
            2'b11: begin result = a | b;        cout = 1'b0; end
            default: begin result = 4'b0;       cout = 1'b0; end
        endcase
    end
endmodule
"""


DECODER_3TO8_SPEC_NORMALISED = """\
* Ports:
  - sel: input, 3 bits (selector)
  - en_n: input, 1 bit (active-low enable)
  - y: output, 8 bits (one-hot decoded output)
* Reset: none (purely combinational)
* Clock: none (purely combinational); 10 ns STA reference
* Target utilisation: 30%
* PDK: gf180mcuD
* Behaviour: en_n high -> y is all zeros; en_n low -> y = (8'b1 << sel)
"""


DECODER_3TO8_PLAN_JSON = """\
{
  "top_module_id": "decoder_3to8",
  "modules": [
    {
      "module_id": "decoder_3to8",
      "name": "decoder_3to8",
      "description": "3-to-8 line decoder with active-low enable",
      "ports": [
        {"name": "sel",  "direction": "in",  "width": 3, "description": "selector"},
        {"name": "en_n", "direction": "in",  "width": 1, "description": "active-low enable"},
        {"name": "y",    "direction": "out", "width": 8, "description": "one-hot output"}
      ],
      "params": {},
      "depends_on": []
    }
  ],
  "rationale": "single-module combinational decoder demo"
}
"""


DECODER_3TO8_RTL = """\
module decoder_3to8 (
    input  wire [2:0] sel,
    input  wire       en_n,
    output reg  [7:0] y
);
    always @* begin
        if (en_n)
            y = 8'b0000_0000;
        else begin
            case (sel)
                3'd0: y = 8'b0000_0001;
                3'd1: y = 8'b0000_0010;
                3'd2: y = 8'b0000_0100;
                3'd3: y = 8'b0000_1000;
                3'd4: y = 8'b0001_0000;
                3'd5: y = 8'b0010_0000;
                3'd6: y = 8'b0100_0000;
                3'd7: y = 8'b1000_0000;
                default: y = 8'b0000_0000;
            endcase
        end
    end
endmodule
"""


def _spec_responses(
    *, spec_normalised: str, plan_json: str, rtl: str, slug: str,
) -> tuple[_PromptMatcher, ...]:
    """Build a six-matcher tuple for one of the F12.6 spec shapes.

    Same pattern as DEFAULT_RESPONSES / SHIFT_REGISTER_RESPONSES — one
    matcher per agent the spine hits between SPEC and SIGNOFF.
    """
    return (
        _PromptMatcher(
            needle="You normalise natural-language chip-module specs",
            response=spec_normalised,
        ),
        _PromptMatcher(
            needle="You are a chip-design planner",
            response=plan_json,
        ),
        _PromptMatcher(
            needle="You are a synthesisable Verilog/SystemVerilog generator",
            response=rtl,
        ),
        _PromptMatcher(
            needle="You are a Verilog/SystemVerilog repair tool",
            response=rtl,
        ),
        _PromptMatcher(
            needle="You are a Verilog/SystemVerilog semantic-repair tool",
            response=rtl,
        ),
        _PromptMatcher(
            needle="You generate cocotb (Python) testbenches",
            response=f"# templated path covers the {slug}; this is the LLM fallback\n",
        ),
        # F19.7 — reuse the counter fixtures for the M19 Phase 1 stages.
        # The gate's verdict on a non-counter module is informational
        # (and warning-only), so the fixtures need only be valid JSON /
        # syntactically valid Python — they do not need to match the
        # spec's port shape.
        *_M19_COUNTER_MATCHERS,
        # F21.3: SIGNOFF-failure recovery matcher (escalate by default).
        _F21_3_PHYSICAL_REPAIR_ESCALATE,
    )


FSM_TRAFFIC_LIGHT_RESPONSES: tuple[_PromptMatcher, ...] = _spec_responses(
    spec_normalised=FSM_TRAFFIC_LIGHT_SPEC_NORMALISED,
    plan_json=FSM_TRAFFIC_LIGHT_PLAN_JSON,
    rtl=FSM_TRAFFIC_LIGHT_RTL,
    slug="fsm-traffic-light",
)

ALU_4BIT_RESPONSES: tuple[_PromptMatcher, ...] = _spec_responses(
    spec_normalised=ALU_4BIT_SPEC_NORMALISED,
    plan_json=ALU_4BIT_PLAN_JSON,
    rtl=ALU_4BIT_RTL,
    slug="alu-4bit",
)

DECODER_3TO8_RESPONSES: tuple[_PromptMatcher, ...] = _spec_responses(
    spec_normalised=DECODER_3TO8_SPEC_NORMALISED,
    plan_json=DECODER_3TO8_PLAN_JSON,
    rtl=DECODER_3TO8_RTL,
    slug="decoder-3to8",
)


# --------------------------------------------------------------------------- #
# StubBackend — a :class:`CompletionBackend` that dispatches on the system
# prompt.
# --------------------------------------------------------------------------- #
@dataclass
class StubBackend:
    """Returns canned text per (system-prompt substring) and records every call."""

    matchers: tuple[_PromptMatcher, ...] = DEFAULT_RESPONSES
    calls: list[dict[str, object]] = field(default_factory=list)
    # F11.4: per-needle complete() call count for response cycling.
    _match_counts: dict[str, int] = field(default_factory=dict)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        seed: int | None,
        api_base: str | None,
    ) -> CompletionResult:
        system = next(
            (m["content"] for m in messages if m.get("role") == "system"),
            "",
        )
        prompt = next(
            (m["content"] for m in messages if m.get("role") == "user"),
            "",
        )
        self.calls.append({
            "model": model, "system": system, "prompt": prompt,
            "temperature": temperature, "seed": seed, "api_base": api_base,
        })
        for matcher in self.matchers:
            if matcher.needle in system:
                response = self._next_response(matcher)
                return CompletionResult(
                    text=response,
                    prompt_tokens=matcher.prompt_tokens,
                    completion_tokens=matcher.completion_tokens,
                    cost_usd=matcher.cost_usd,
                )
        raise AssertionError(
            f"StubBackend received an unrecognised system prompt; "
            f"first 200 chars: {system[:200]!r}",
        )

    def _next_response(self, matcher: _PromptMatcher) -> str:
        """Pick the next response for ``matcher`` (cycling if a sequence is set)."""
        seq = matcher.response_sequence
        if seq is None:
            return matcher.response
        idx = self._match_counts.get(matcher.needle, 0)
        self._match_counts[matcher.needle] = idx + 1
        if idx < len(seq):
            return seq[idx]
        return matcher.response

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        seed: int | None,
        api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        """Surface the matching response as a sequence of stream chunks.

        F11.3: when the matched :class:`_PromptMatcher` declares
        ``stream_chunks``, those are yielded verbatim (with the terminal
        chunk carrying usage). Otherwise the whole ``response`` is yielded
        as one terminal chunk.
        """
        system = next(
            (m["content"] for m in messages if m.get("role") == "system"),
            "",
        )
        prompt = next(
            (m["content"] for m in messages if m.get("role") == "user"),
            "",
        )
        self.calls.append({
            "mode": "stream",
            "model": model, "system": system, "prompt": prompt,
            "temperature": temperature, "seed": seed, "api_base": api_base,
        })
        for matcher in self.matchers:
            if matcher.needle in system:
                chunks = matcher.stream_chunks or (matcher.response,)
                for i, piece in enumerate(chunks):
                    is_last = i == len(chunks) - 1
                    if is_last:
                        yield CompletionStreamChunk(
                            delta=piece,
                            finish_reason="stop",
                            prompt_tokens=matcher.prompt_tokens,
                            completion_tokens=matcher.completion_tokens,
                            cost_usd=matcher.cost_usd,
                        )
                    else:
                        yield CompletionStreamChunk(delta=piece)
                return
        raise AssertionError(
            f"StubBackend received an unrecognised system prompt; "
            f"first 200 chars: {system[:200]!r}",
        )


# --------------------------------------------------------------------------- #
# Settings YAML + LiteLLMRouter builders.
# --------------------------------------------------------------------------- #
_ROUTING_YAML = """\
constraints:
  pdk: gf180mcuD
  std_cell_lib: gf180mcu_fd_sc_mcu7t5v0
  target_clock_ns: 10.0
  target_utilization: 0.5

routing:
  # F19.11: keep the demo tests on the M19 path. ``Settings`` defaults
  # this to ``false`` (pre-F19.7), so an explicit ``true`` here pins
  # demo behaviour to today's stack.
  use_test_first_workflow: true
  registry:
    stub-model:
      provider: stub
      model: deterministic
  loops:
    inner: {model: stub-model, temperature: 0.0, n: 1}
    outer: {model: stub-model, temperature: 0.0, n: 1}
  tasks:
    spec_intake:          {model: stub-model, temperature: 0.0, n: 1}
    plan:                 {model: stub-model, temperature: 0.0, n: 1}
    rtl_gen:              {model: stub-model, temperature: 0.0, n: 1}
    rtl_repair:           {model: stub-model, temperature: 0.0, n: 1}
    tb_gen:               {model: stub-model, temperature: 0.0, n: 1}
    diagnose:             {model: stub-model, temperature: 0.0, n: 1}
    # F19.7: M19 Phase 1 task bindings (contract / oracle / assertions).
    contract_extraction:  {model: stub-model, temperature: 0.0, n: 1}
    # F19.4b: oracle plan stage (semantic decomposition before code-gen).
    oracle_plan:          {model: stub-model, temperature: 0.0, n: 1}
    oracle_gen:           {model: stub-model, temperature: 0.0, n: 1}
    assertion_extract:    {model: stub-model, temperature: 0.0, n: 1}
    assertion_map:        {model: stub-model, temperature: 0.0, n: 1}
    assertion_translate:  {model: stub-model, temperature: 0.0, n: 1}
    # F19.9: adaptive reflection routing (RTL outer-loop recovery).
    reflection_routing:   {model: stub-model, temperature: 0.0, n: 1}
    # F20.1: cross-attempt repair rationale extraction.
    repair_rationale:     {model: stub-model, temperature: 0.0, n: 1}
    # F21.3: physical-repair routing (SIGNOFF timing-failure recovery).
    physical_repair_routing: {model: stub-model, temperature: 0.0, n: 1}
"""


def make_routing_config(tmp_path: Path, *, filename: str = "routing.yaml") -> Path:
    """Write a Settings YAML that names one ``stub-model`` for every task."""
    cfg = tmp_path / filename
    cfg.write_text(_ROUTING_YAML)
    return cfg


def make_test_router(
    *,
    config_path: Path | None = None,
    backend: StubBackend | None = None,
    matchers: Iterable[_PromptMatcher] | None = None,
) -> tuple[LiteLLMRouter, StubBackend]:
    """Build a real :class:`LiteLLMRouter` over a :class:`StubBackend`.

    ``config_path`` is the YAML that produced the :class:`Settings` whose
    ``routing`` block the router consumes. ``backend`` may be supplied so
    callers can assert on the recorded ``calls``; otherwise a fresh one is
    created with ``matchers`` (default :data:`DEFAULT_RESPONSES`).
    """
    if config_path is None:
        raise ValueError("config_path is required (write one with make_routing_config)")
    settings = Settings.from_yaml(config_path)
    used_backend = backend or StubBackend(
        matchers=tuple(matchers) if matchers else DEFAULT_RESPONSES,
    )
    router = LiteLLMRouter(
        routing=settings.routing,
        gateway=LiteLLMGateway(backend=used_backend),
    )
    return router, used_backend
