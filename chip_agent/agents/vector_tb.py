"""Ground-truth vector testbench for serial-load, completion-gated designs.

The F19.8 :class:`~chip_agent.agents.differential_tb.DifferentialTBBuilder`
replays a short per-cycle stimulus and checks the DUT against a model-generated
Python oracle. That approach is unsound for a high-latency block design (e.g. a
bit-serial block cipher) for two reasons the present80 run exposed:

* the differential window (a 4-6 cycle oracle worked-example) never reaches the
  design's completion — the ``proj_diff_tb_window_too_short`` vacuous pass; and
* the generated oracle can itself be wrong, so RTL-vs-oracle proves nothing.

For a design whose spec ships **published test vectors** (input -> known output)
the trustworthy reference is those vectors, not a model. This builder detects a
serial-load / completion-gated module, extracts ``(inputs..., output)`` vectors
from the spec, and renders a *transaction-level* cocotb testbench: load each
vector MSB-first, wait (bounded) for the completion port, shift the result out,
and assert it equals the spec's value. On mismatch it emits the canonical
``VEC|...`` token so :mod:`chip_agent.tools.trace` can extract structured fields.

Applicability is conservative: :meth:`VectorTBBuilder.build` returns ``None``
when the module shape or spec vectors can't be resolved, so the caller falls
back to the differential / LLM testbench unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from chip_agent.design_state import (
    DesignPlan,
    ModuleDecl,
    Provenance,
    Spec,
    Stage,
    TestbenchArtifact,
    ToolVersion,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = [
    "SerialProtocol",
    "TestVector",
    "VectorTBBuilder",
    "detect_serial_protocol",
    "parse_test_vectors",
    "render_vector_tb",
]


# Port-name heuristics (lowercased, matched exactly against ``port.name.lower()``).
_CLOCK_NAMES = frozenset({"clk", "clock"})
_ACTIVE_LOW_RESET = frozenset({"rst_n", "rstn", "reset_n", "resetn", "nreset"})
_ACTIVE_HIGH_RESET = frozenset({"rst", "reset"})
_LOAD_EN_NAMES = frozenset(
    {"load_en", "load", "load_valid", "valid_in", "in_valid", "start",
     "wr", "we", "write", "wr_en", "shift_in_en", "din_valid"},
)
_DIN_NAMES = frozenset(
    {"din", "data_in", "d_in", "serial_in", "sin", "mosi", "data", "d", "in"},
)
_SHIFT_OUT_NAMES = frozenset(
    {"shift_out_en", "shift_en", "unload", "unload_en", "rd", "rd_en", "read",
     "out_en", "so_en", "dout_en", "read_en"},
)
_DONE_NAMES = frozenset(
    {"done", "valid", "complete", "completed", "finish", "finished", "eot",
     "out_valid", "dout_valid", "ready", "result_valid"},
)
_DOUT_NAMES = frozenset(
    {"dout", "data_out", "d_out", "serial_out", "sout", "miso", "q", "do",
     "result"},
)


@dataclass(frozen=True)
class SerialProtocol:
    """Resolved port roles + reset polarity for a serial-load design."""

    clk: str
    rst: str
    rst_active_low: bool
    load_en: str
    din: str
    shift_out_en: str
    done: str
    dout: str


@dataclass(frozen=True)
class TestVector:
    """One ``(inputs -> output)`` vector; widths are in bits (hex nibbles * 4)."""

    inputs: tuple[int, ...]
    input_bits: tuple[int, ...]
    output: int
    output_bits: int


def _one_bit_input(module: ModuleDecl, names: frozenset[str]) -> str | None:
    for p in module.ports:
        if p.direction == "in" and p.width == 1 and p.name.lower() in names:
            return p.name
    return None


def _one_bit_output(module: ModuleDecl, names: frozenset[str]) -> str | None:
    for p in module.ports:
        if p.direction == "out" and p.width == 1 and p.name.lower() in names:
            return p.name
    return None


def detect_serial_protocol(module: ModuleDecl) -> SerialProtocol | None:
    """Resolve serial-load port roles, or ``None`` if the shape doesn't match.

    Requires a clock, a reset (polarity inferred from the name), a load-enable,
    a 1-bit serial data-in, a 1-bit serial data-out, and a completion output.
    A shift-out-enable is optional (falls back to the load-enable's absence).
    """
    clk = next(
        (p.name for p in module.ports
         if p.direction == "in" and p.name.lower() in _CLOCK_NAMES),
        None,
    )
    rst_low = next(
        (p.name for p in module.ports
         if p.direction == "in" and p.name.lower() in _ACTIVE_LOW_RESET),
        None,
    )
    rst_high = next(
        (p.name for p in module.ports
         if p.direction == "in" and p.name.lower() in _ACTIVE_HIGH_RESET),
        None,
    )
    load_en = _one_bit_input(module, _LOAD_EN_NAMES)
    din = _one_bit_input(module, _DIN_NAMES)
    shift_out = _one_bit_input(module, _SHIFT_OUT_NAMES)
    done = _one_bit_output(module, _DONE_NAMES)
    dout = _one_bit_output(module, _DOUT_NAMES)

    rst = rst_low or rst_high
    if not (clk and rst and load_en and din and done and dout):
        return None
    return SerialProtocol(
        clk=clk, rst=rst, rst_active_low=rst_low is not None,
        load_en=load_en, din=din,
        shift_out_en=shift_out or load_en,  # some designs gate unload on done only
        done=done, dout=dout,
    )


# A markdown table row of 3 hex cells: | key | plaintext | ciphertext |
_HEX_CELL = r"`?([0-9a-fA-F]{2,})`?"
_VECTOR_ROW = re.compile(
    rf"\|\s*{_HEX_CELL}\s*\|\s*{_HEX_CELL}\s*\|\s*{_HEX_CELL}\s*\|",
)


def parse_test_vectors(spec_text: str) -> list[TestVector]:
    """Extract ``(in0, in1, out)`` hex vectors from a spec markdown table.

    Targets the canonical 3-column ``| input_a | input_b | output |`` shape
    (e.g. the PRESENT ``key | plaintext | ciphertext`` table). Bit widths are
    derived from the hex-nibble count so a 20-nibble key reads as 80 bits.
    Header / separator rows (non-hex, or all-dashes) don't match the hex cell
    pattern and are skipped. Returns ``[]`` when no vector rows are found.
    """
    vectors: list[TestVector] = []
    for a, b, out in _VECTOR_ROW.findall(spec_text):
        try:
            ia, ib, io = int(a, 16), int(b, 16), int(out, 16)
        except ValueError:  # pragma: no cover - guarded by the regex
            continue
        vectors.append(
            TestVector(
                inputs=(ia, ib),
                input_bits=(len(a) * 4, len(b) * 4),
                output=io,
                output_bits=len(out) * 4,
            )
        )
    return vectors


_TB_TEMPLATE = '''\
"""Ground-truth vector testbench for {top} (generated by vector_tb).

Loads each spec test vector MSB-first over the serial interface, waits
(bounded) for the completion port, unloads the result, and asserts it equals
the spec's published value. Emits ``VEC|...`` on mismatch.
"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer

INPUT_BITS = {input_bits!r}
OUTPUT_BITS = {output_bits}
MAX_WAIT = {max_wait}
# (input_a, input_b, expected_output)
VECTORS = {vectors!r}


async def _shift_in(dut, value, nbits):
    for i in range(nbits - 1, -1, -1):          # MSB-first
        await FallingEdge(dut.{clk})
        dut.{load_en}.value = 1
        dut.{din}.value = (value >> i) & 1


@cocotb.test()
async def test_{test_id}_vectors(dut):
    cocotb.start_soon(Clock(dut.{clk}, 10, units="ns").start())
    for vec in VECTORS:
        inputs, exp = vec[:-1], vec[-1]
        # reset
        dut.{rst}.value = {rst_assert}
        dut.{load_en}.value = 0
        dut.{din}.value = 0
        dut.{shift_out_en}.value = 0
        await RisingEdge(dut.{clk})
        await RisingEdge(dut.{clk})
        dut.{rst}.value = {rst_deassert}

        # load each input field MSB-first, in table order
        for value, nbits in zip(inputs, INPUT_BITS):
            await _shift_in(dut, value, nbits)
        await FallingEdge(dut.{clk})
        dut.{load_en}.value = 0
        dut.{din}.value = 0

        # wait (bounded) for completion
        waited = 0
        while int(dut.{done}.value) == 0 and waited < MAX_WAIT:
            await RisingEdge(dut.{clk})
            waited += 1
        assert int(dut.{done}.value) == 1, (
            f"VEC|inputs={{[hex(v) for v in inputs]}}|signal={done}|"
            f"expected=1|actual=0 (never asserted within {{MAX_WAIT}} cycles)"
        )

        # unload the result MSB-first
        result = 0
        for _ in range(OUTPUT_BITS):
            await FallingEdge(dut.{clk})
            dut.{shift_out_en}.value = 1
            await Timer(1, units="ns")
            result = (result << 1) | (int(dut.{dout}.value) & 1)
            await RisingEdge(dut.{clk})
        dut.{shift_out_en}.value = 0

        assert result == exp, (
            f"VEC|inputs={{[hex(v) for v in inputs]}}|signal=output|"
            f"expected={{exp:0{{OUTPUT_BITS // 4}}x}}|actual={{result:0{{OUTPUT_BITS // 4}}x}}"
        )
'''


def render_vector_tb(
    *,
    module: ModuleDecl,
    protocol: SerialProtocol,
    vectors: list[TestVector],
    max_wait: int,
) -> str:
    """Render the transaction-level cocotb TB source."""
    input_bits = vectors[0].input_bits
    output_bits = vectors[0].output_bits
    tuples = [(*v.inputs, v.output) for v in vectors]
    test_id = re.sub(r"\W+", "_", module.name).strip("_") or "dut"
    return _TB_TEMPLATE.format(
        top=module.name,
        test_id=test_id,
        input_bits=input_bits,
        output_bits=output_bits,
        max_wait=max_wait,
        vectors=tuples,
        clk=protocol.clk,
        rst=protocol.rst,
        rst_assert=0 if protocol.rst_active_low else 1,
        rst_deassert=1 if protocol.rst_active_low else 0,
        load_en=protocol.load_en,
        din=protocol.din,
        shift_out_en=protocol.shift_out_en,
        done=protocol.done,
        dout=protocol.dout,
    )


class VectorTBBuilder:
    """Spec-vector -> transaction-level cocotb ``TestbenchArtifact``.

    ``build`` returns ``None`` when the module isn't a serial-load /
    completion-gated design or the spec has no parseable vectors, so the caller
    falls back to the differential / LLM testbench.
    """

    TOOL_VERSION = "f-vectb.1"

    def __init__(
        self,
        *,
        store: SqliteArtifactStore,
        design_id: str,
        latency_margin_cycles: int = 64,
        agent_name: str = "vector_tb_builder",
    ) -> None:
        self.store = store
        self.design_id = design_id
        self.latency_margin_cycles = latency_margin_cycles
        self.agent_name = agent_name

    def build(
        self, module: ModuleDecl, spec: Spec, plan: DesignPlan,
    ) -> TestbenchArtifact | None:
        protocol = detect_serial_protocol(module)
        if protocol is None:
            return None
        vectors = parse_test_vectors(spec.raw_text)
        if not vectors:
            return None
        # Require a consistent, load-order-matching vector shape: two input
        # fields whose combined width equals the serial load, one output.
        if any(len(v.input_bits) != 2 for v in vectors):
            return None

        # Bound the done wait: full serial load + a generous compute/margin.
        load_bits = sum(vectors[0].input_bits)
        max_wait = load_bits + self.latency_margin_cycles * 8

        source = render_vector_tb(
            module=module, protocol=protocol, vectors=vectors, max_wait=max_wait,
        )
        blob = self.store.put_blob(
            source.encode("utf-8"), media_type="text/x-python",
        )
        tb = TestbenchArtifact(
            artifact_id=f"{self.design_id}.{module.module_id}.tb",
            design_id=self.design_id,
            module_id=module.module_id,
            framework="cocotb",
            target_module=module.name,
            source=blob,
            metadata={"vector_count": str(len(vectors))},
            provenance=Provenance(
                produced_by=Stage.RTL,
                agent=self.agent_name,
                tool=ToolVersion(name="vector_tb", version=self.TOOL_VERSION),
                inputs=[spec.ref(), plan.ref()],
            ),
        )
        self.store.put(tb)
        return tb
