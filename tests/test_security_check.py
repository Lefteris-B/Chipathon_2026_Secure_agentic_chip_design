"""F6.4 acceptance: structural security checks over a synth netlist.

The flagship test is the **injected always-on backdoor net** fixture: a
gate-level netlist with ``assign backdoor = 1'b1;`` is fed through
:func:`parse_netlist_security` and the service. Both must surface a
``SECURITY.ALWAYS_ON_NET`` violation, and the resulting
:class:`SecurityReport` must close its ``gate_ok`` so the signoff
conjunction blocks the design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chip_agent.design_state import (
    ArtifactKind,
    NetlistArtifact,
    Provenance,
    Stage,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.security_check import (
    ALWAYS_OFF_CHECK,
    ALWAYS_ON_CHECK,
    SUSPICIOUS_NAME_CHECK,
    SUSPICIOUS_NAMES,
    SecurityCheckService,
    SecurityParse,
    parse_netlist_security,
)

# --------------------------------------------------------------------------- #
# Netlist fixtures
# --------------------------------------------------------------------------- #
_CLEAN_NETLIST = """\
module counter (
    input clk,
    input rst_n,
    output [3:0] q
);
    wire [3:0] q_next;
    sky130_fd_sc_hd__dfxtp_2 \\q_reg[0] (.D(q_next[0]), .Q(q[0]), .CLK(clk));
    sky130_fd_sc_hd__dfxtp_2 \\q_reg[1] (.D(q_next[1]), .Q(q[1]), .CLK(clk));
endmodule
"""


_BACKDOOR_NETLIST = """\
module counter (
    input clk,
    input rst_n,
    output [3:0] q,
    output secure_mode_out
);
    wire [3:0] q_next;
    wire backdoor;
    assign backdoor = 1'b1;
    assign secure_mode_out = backdoor;

    sky130_fd_sc_hd__dfxtp_2 \\q_reg[0] (.D(q_next[0]), .Q(q[0]), .CLK(clk));
endmodule
"""


_PULLDOWN_NETLIST = """\
module counter (input clk, output [3:0] q);
    wire [3:0] q_next;
    assign q_next[3] = 1'b0;
    sky130_fd_sc_hd__dfxtp_2 \\q_reg[3] (.D(q_next[3]), .Q(q[3]), .CLK(clk));
endmodule
"""


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_clean_netlist_passes() -> None:
    p = parse_netlist_security(_CLEAN_NETLIST)
    assert isinstance(p, SecurityParse)
    assert p.passed
    assert p.violations == []
    assert p.suspicious_structures == 0
    assert set(p.checks_run) == {
        ALWAYS_ON_CHECK, ALWAYS_OFF_CHECK, SUSPICIOUS_NAME_CHECK,
    }


def test_backdoor_always_on_net_is_flagged() -> None:
    # AC: an injected always-on backdoor net surfaces as a typed violation
    # AND the report's gate closes.
    p = parse_netlist_security(_BACKDOOR_NETLIST)
    always_on = [v for v in p.violations if v.code == "SECURITY.ALWAYS_ON_NET"]
    assert len(always_on) >= 1
    assert any(v.location == "backdoor" for v in always_on)
    assert always_on[0].severity == "error"
    assert not p.passed


def test_suspicious_name_alone_flags_even_without_assign() -> None:
    netlist = (
        "module counter (input clk, output backdoor_disable_n);\n"
        "  assign backdoor_disable_n = clk;\n"
        "endmodule\n"
    )
    p = parse_netlist_security(netlist)
    # `backdoor_disable_n` matches the "backdoor" suspicious-name pattern.
    assert any(v.code == "SECURITY.SUSPICIOUS_NAME" for v in p.violations)
    assert not p.passed


def test_always_off_is_advisory_only() -> None:
    # Tied-low pulldowns are common — they should not close the gate by
    # themselves.
    p = parse_netlist_security(_PULLDOWN_NETLIST)
    advisory = [v for v in p.violations if v.code == "SECURITY.ALWAYS_OFF_NET"]
    assert advisory
    assert all(v.severity == "warning" for v in advisory)
    assert p.passed  # warnings alone don't close the gate


def test_hex_constant_one_also_flagged() -> None:
    p = parse_netlist_security(
        "module m;\n  assign sneaky = 1'h1;\nendmodule\n"
    )
    assert any(v.code == "SECURITY.ALWAYS_ON_NET" for v in p.violations)


def test_bit_select_always_on_is_flagged() -> None:
    p = parse_netlist_security(
        "module m;\n  assign data[3] = 1'b1;\nendmodule\n"
    )
    flagged = [v for v in p.violations if v.code == "SECURITY.ALWAYS_ON_NET"]
    assert flagged
    assert flagged[0].location == "data[3]"


def test_word_boundary_avoids_false_positives() -> None:
    # `feedback` contains "back" but not "backdoor" / "debug_force" etc.;
    # the suspicious-name pattern must word-boundary properly.
    p = parse_netlist_security(
        "module m (input feedback); endmodule\n"
    )
    assert not any(
        v.code == "SECURITY.SUSPICIOUS_NAME" for v in p.violations
    )


def test_suspicious_names_dedupe_per_pattern() -> None:
    # The same identifier appearing twice shouldn't double-count.
    netlist = (
        "module m;\n"
        "  wire backdoor;\n"
        "  assign backdoor = 1'b1;\n"
        "  assign other = backdoor;\n"
        "endmodule\n"
    )
    p = parse_netlist_security(netlist)
    name_hits = [v for v in p.violations if v.code == "SECURITY.SUSPICIOUS_NAME"]
    # Only one violation per *identifier*, even though `backdoor` appears 3x.
    backdoor_hits = [v for v in name_hits if v.location and "backdoor" in v.location.lower()]
    assert len(backdoor_hits) == 1


def test_check_subset_runs_only_requested_checks() -> None:
    p = parse_netlist_security(
        _BACKDOOR_NETLIST, checks=(ALWAYS_ON_CHECK,),
    )
    assert p.checks_run == [ALWAYS_ON_CHECK]
    # Suspicious-name violations should NOT show up — the pattern wasn't run.
    assert not any(v.code == "SECURITY.SUSPICIOUS_NAME" for v in p.violations)
    # But the always-on net still gets flagged.
    assert any(v.code == "SECURITY.ALWAYS_ON_NET" for v in p.violations)


def test_all_suspicious_names_are_word_boundary_matched() -> None:
    # Smoke-check each pattern flags an identifier containing it.
    for name in SUSPICIOUS_NAMES:
        netlist = f"module m (input my_{name}_signal); endmodule\n"
        p = parse_netlist_security(netlist)
        assert any(
            v.code == "SECURITY.SUSPICIOUS_NAME" and name in (v.location or "").lower()
            for v in p.violations
        ), f"pattern {name!r} should match in {netlist!r}"


# --------------------------------------------------------------------------- #
# Service-level AC tests
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


def _stage_netlist(
    store: SqliteArtifactStore, *, source: str,
    design_id: str = "d0", top: str = "counter",
) -> NetlistArtifact:
    blob = store.put_blob(source.encode(), media_type="text/x-verilog")
    art = NetlistArtifact(
        artifact_id=f"{design_id}.{top}.netlist",
        design_id=design_id, module_id=top,
        netlist=blob, std_cell_lib="sky130_fd_sc_hd",
        cell_count=10,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_service_returns_security_report(store: SqliteArtifactStore) -> None:
    netlist = _stage_netlist(store, source=_CLEAN_NETLIST)
    svc = SecurityCheckService(store=store)
    report = svc.check_security(netlist)
    assert report.kind is ArtifactKind.SECURITY
    assert report.gate_ok
    assert report.checks_run  # the parser populated this


def test_service_flags_injected_backdoor_net(store: SqliteArtifactStore) -> None:
    # The F6.4 AC at the service level. An always-on net is flagged as a
    # SECURITY.ALWAYS_ON_NET violation and the report's gate is closed.
    netlist = _stage_netlist(store, source=_BACKDOOR_NETLIST)
    svc = SecurityCheckService(store=store)
    report = svc.check_security(netlist)

    assert not report.gate_ok
    assert not report.passed
    codes = {v.code for v in report.violations}
    assert "SECURITY.ALWAYS_ON_NET" in codes
    # `backdoor` is both an always-on net AND a suspicious name — both fire.
    assert "SECURITY.SUSPICIOUS_NAME" in codes
    assert report.suspicious_structures >= 1
    assert report.provenance.inputs == [netlist.ref()]


def test_service_records_layout_in_provenance_when_supplied(
    store: SqliteArtifactStore,
) -> None:
    from chip_agent.design_state import LayoutArtifact
    netlist = _stage_netlist(store, source=_CLEAN_NETLIST)
    def_blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    layout = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=def_blob, stage_reached="routed",
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(layout)
    layout = store.get_by_id(layout.artifact_id)  # type: ignore[assignment]

    svc = SecurityCheckService(store=store)
    report = svc.check_security(netlist, layout=layout)
    refs = {r.artifact_id for r in report.provenance.inputs}
    assert "d0.counter.netlist" in refs
    assert "d0.counter.layout" in refs
