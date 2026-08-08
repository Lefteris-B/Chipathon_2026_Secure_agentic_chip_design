"""Unit tests for the ground-truth vector testbench builder."""

from __future__ import annotations

import pytest

from chip_agent.agents.vector_tb import (
    detect_serial_protocol,
    parse_test_vectors,
    render_vector_tb,
)
from chip_agent.design_state import ModuleDecl, Port


def _serial_module() -> ModuleDecl:
    return ModuleDecl(
        module_id="mod_present80",
        name="present80",
        description="bit-serial PRESENT-80",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="load_en", direction="in", width=1),
            Port(name="din", direction="in", width=1),
            Port(name="shift_out_en", direction="in", width=1),
            Port(name="dout", direction="out", width=1),
            Port(name="done", direction="out", width=1),
        ],
    )


_SPEC_TABLE = """
## Test vectors

| key                        | plaintext          | ciphertext         |
|----------------------------|--------------------|--------------------|
| `00000000000000000000`     | `0000000000000000` | `5579C1387B228445` |
| `FFFFFFFFFFFFFFFFFFFF`      | `0000000000000000` | `E72C46C0F5945049` |
"""


def test_detect_serial_protocol_resolves_roles_and_polarity() -> None:
    proto = detect_serial_protocol(_serial_module())
    assert proto is not None
    assert proto.clk == "clk"
    assert proto.rst == "rst_n"
    assert proto.rst_active_low is True
    assert proto.load_en == "load_en"
    assert proto.din == "din"
    assert proto.shift_out_en == "shift_out_en"
    assert proto.done == "done"
    assert proto.dout == "dout"


def test_detect_serial_protocol_rejects_non_serial_module() -> None:
    combinational = ModuleDecl(
        module_id="m", name="adder", description="",
        ports=[
            Port(name="a", direction="in", width=8),
            Port(name="b", direction="in", width=8),
            Port(name="y", direction="out", width=8),
        ],
    )
    assert detect_serial_protocol(combinational) is None


def test_parse_test_vectors_reads_widths_from_hex_nibbles() -> None:
    vectors = parse_test_vectors(_SPEC_TABLE)
    assert len(vectors) == 2
    first = vectors[0]
    assert first.inputs == (0x00000000000000000000, 0x0000000000000000)
    assert first.input_bits == (80, 64)  # 20 and 16 hex nibbles
    assert first.output == 0x5579C1387B228445
    assert first.output_bits == 64
    assert vectors[1].inputs[0] == 0xFFFFFFFFFFFFFFFFFFFF


def test_parse_test_vectors_empty_when_no_table() -> None:
    assert parse_test_vectors("no vectors here") == []


def test_render_vector_tb_embeds_ports_and_vectors() -> None:
    module = _serial_module()
    proto = detect_serial_protocol(module)
    assert proto is not None
    vectors = parse_test_vectors(_SPEC_TABLE)
    src = render_vector_tb(
        module=module, protocol=proto, vectors=vectors, max_wait=400,
    )
    assert "import cocotb" in src
    assert "dut.load_en" in src and "dut.din" in src and "dut.done" in src
    # active-low reset asserts 0, deasserts 1
    assert "dut.rst_n.value = 0" in src
    assert "dut.rst_n.value = 1" in src
    # ground-truth ciphertext is embedded
    assert 0x5579C1387B228445 in [v.output for v in vectors]
    assert "0x5579c1387b228445".upper()[2:].lower() in src.lower() or \
        str(0x5579C1387B228445) in src


@pytest.mark.parametrize("bad_reset_name", ["clk", "load_en"])
def test_detect_requires_a_reset(bad_reset_name: str) -> None:
    # A module with no recognizable reset port is not applicable.
    module = ModuleDecl(
        module_id="m", name="x", description="",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="load_en", direction="in", width=1),
            Port(name="din", direction="in", width=1),
            Port(name="dout", direction="out", width=1),
            Port(name="done", direction="out", width=1),
        ],
    )
    assert detect_serial_protocol(module) is None
