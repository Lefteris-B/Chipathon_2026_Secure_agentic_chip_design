"""F6.3 acceptance: :class:`SignoffStageDriver` conjoins STA + DRC + LVS.

* A clean design passes all three legs and produces a passing outcome
  (the F6.3 AC "clean design passes all four"; security is F6.4).
* A timing-failing design yields a :class:`TimingReport` with
  ``wns_ns < 0`` and the conjoined gate closes — even when DRC and LVS
  both pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.agents.signoff_stage import (
    SignoffStageDriver,
    SignoffStageError,
    SignoffStageOutcome,
)
from chip_agent.design_state import (
    DRCReport,
    LayoutArtifact,
    LVSReport,
    NetlistArtifact,
    Provenance,
    SecurityReport,
    Stage,
    TimingReport,
    Violation,
)
from chip_agent.store import SqliteArtifactStore


# --------------------------------------------------------------------------- #
# Stub services (the driver's seams)
# --------------------------------------------------------------------------- #
@dataclass
class StubSTA:
    report: TimingReport
    calls: list[float] = field(default_factory=list)
    sdf_calls: list[bytes | None] = field(default_factory=list)
    netlist_override_calls: list[bytes | None] = field(default_factory=list)

    def check_timing(
        self, netlist: NetlistArtifact, *,
        clock_period_ns: float,
        sdc_text: str | None = None,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        sdf_bytes: bytes | None = None,
        netlist_bytes_override: bytes | None = None,
    ) -> TimingReport:
        self.calls.append(clock_period_ns)
        self.sdf_calls.append(sdf_bytes)
        self.netlist_override_calls.append(netlist_bytes_override)
        return self.report


@dataclass
class StubDRC:
    report: DRCReport
    calls: list[LayoutArtifact] = field(default_factory=list)
    librelane_calls: list[bytes | None] = field(default_factory=list)

    def check_drc(
        self, layout: LayoutArtifact, *,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        librelane_report_bytes: bytes | None = None,
    ) -> DRCReport:
        self.calls.append(layout)
        self.librelane_calls.append(librelane_report_bytes)
        return self.report


@dataclass
class StubLVS:
    report: LVSReport
    calls: list[tuple[NetlistArtifact, LayoutArtifact, bytes]] = field(default_factory=list)
    netlist_override_calls: list[bytes | None] = field(default_factory=list)

    def check_lvs(
        self, netlist: NetlistArtifact, layout: LayoutArtifact, *,
        layout_netlist_bytes: bytes,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        netlist_bytes_override: bytes | None = None,
    ) -> LVSReport:
        self.calls.append((netlist, layout, layout_netlist_bytes))
        self.netlist_override_calls.append(netlist_bytes_override)
        return self.report


@dataclass
class StubSecurity:
    report: SecurityReport
    calls: list[NetlistArtifact] = field(default_factory=list)

    def check_security(
        self, netlist: NetlistArtifact, *,
        layout: LayoutArtifact | None = None,
        top_module: str | None = None,
    ) -> SecurityReport:
        self.calls.append(netlist)
        return self.report


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


def _netlist(store: SqliteArtifactStore, *, design_id: str = "d0") -> NetlistArtifact:
    blob = store.put_blob(b"// gate-level\n", media_type="text/x-verilog")
    art = NetlistArtifact(
        artifact_id=f"{design_id}.counter.netlist",
        design_id=design_id, module_id="counter",
        netlist=blob, std_cell_lib="sky130_fd_sc_hd",
        cell_count=10,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _layout(store: SqliteArtifactStore, *, design_id: str = "d0") -> LayoutArtifact:
    blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    art = LayoutArtifact(
        artifact_id=f"{design_id}.counter.layout",
        design_id=design_id, module_id="counter",
        def_file=blob, stage_reached="routed",
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _ok_timing() -> TimingReport:
    return TimingReport(
        artifact_id="d0.counter.timing",
        design_id="d0", module_id="counter",
        passed=True, wns_ns=0.5, tns_ns=0.0,
        setup_violations=0, hold_violations=0,
        metrics={"errors": 0.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _bad_timing() -> TimingReport:
    return TimingReport(
        artifact_id="d0.counter.timing",
        design_id="d0", module_id="counter",
        passed=False, wns_ns=-0.85, tns_ns=-3.2,
        setup_violations=1, hold_violations=0,
        violations=[Violation(
            code="STA.SETUP_VIOLATION", severity="error",
            message="WNS -0.85 ns is negative",
            detail={"wns_ns": -0.85},
        )],
        metrics={"errors": 1.0, "wns_ns": -0.85},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _ok_drc() -> DRCReport:
    return DRCReport(
        artifact_id="d0.counter.drc",
        design_id="d0", module_id="counter",
        passed=True, violation_count=0,
        metrics={"errors": 0.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _bad_drc() -> DRCReport:
    return DRCReport(
        artifact_id="d0.counter.drc",
        design_id="d0", module_id="counter",
        passed=False, violation_count=3,
        violations=[Violation(code="DRC.RULE", severity="error",
                              message="3 DRC errors")],
        metrics={"errors": 1.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _ok_lvs() -> LVSReport:
    return LVSReport(
        artifact_id="d0.counter.lvs",
        design_id="d0", module_id="counter",
        passed=True, matched=True, mismatch_count=0,
        metrics={"errors": 0.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _bad_lvs() -> LVSReport:
    return LVSReport(
        artifact_id="d0.counter.lvs",
        design_id="d0", module_id="counter",
        passed=False, matched=False, mismatch_count=2,
        violations=[Violation(code="LVS.MISMATCH", severity="error",
                              message="2 device mismatches")],
        metrics={"errors": 1.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _ok_security() -> SecurityReport:
    return SecurityReport(
        artifact_id="d0.counter.security",
        design_id="d0", module_id="counter",
        passed=True, suspicious_structures=0,
        checks_run=["always_on_nets", "suspicious_names"],
        metrics={"errors": 0.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _bad_security() -> SecurityReport:
    return SecurityReport(
        artifact_id="d0.counter.security",
        design_id="d0", module_id="counter",
        passed=False, suspicious_structures=1,
        checks_run=["always_on_nets", "suspicious_names"],
        violations=[Violation(
            code="SECURITY.ALWAYS_ON_NET", severity="error",
            message="net 'backdoor' is unconditionally driven high",
            location="backdoor",
        )],
        metrics={"errors": 1.0, "always_on_nets": 1.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


# --------------------------------------------------------------------------- #
# AC: clean design passes
# --------------------------------------------------------------------------- #
def test_clean_design_passes_all_four_legs(store: SqliteArtifactStore) -> None:
    sta = StubSTA(report=_ok_timing())
    drc = StubDRC(report=_ok_drc())
    lvs = StubLVS(report=_ok_lvs())
    security = StubSecurity(report=_ok_security())
    driver = SignoffStageDriver(sta=sta, drc=drc, lvs=lvs, security=security,
                                store=store, design_id="d0")

    outcome = driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0,
        layout_netlist_bytes=b"* extracted\n",
    )
    assert isinstance(outcome, SignoffStageOutcome)
    assert outcome.passed
    assert outcome.failing_codes() == []
    # All four reports persisted to the store, refs returned.
    assert outcome.timing_ref.artifact_id == "d0.counter.timing"
    assert outcome.drc_ref.artifact_id == "d0.counter.drc"
    assert outcome.lvs_ref.artifact_id == "d0.counter.lvs"
    assert outcome.security_ref.artifact_id == "d0.counter.security"


# --------------------------------------------------------------------------- #
# AC: timing-failing design blocks the gate even when DRC + LVS pass
# --------------------------------------------------------------------------- #
def test_negative_wns_closes_signoff_gate(store: SqliteArtifactStore) -> None:
    sta = StubSTA(report=_bad_timing())
    drc = StubDRC(report=_ok_drc())
    lvs = StubLVS(report=_ok_lvs())
    security = StubSecurity(report=_ok_security())
    driver = SignoffStageDriver(sta=sta, drc=drc, lvs=lvs, security=security,
                                store=store, design_id="d0")

    outcome = driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=2.0,
        layout_netlist_bytes=b"* extracted\n",
    )
    assert not outcome.passed
    assert outcome.timing.wns_ns is not None
    assert outcome.timing.wns_ns < 0.0
    assert not outcome.timing.gate_ok
    # The driver records WHICH leg closed the gate.
    assert outcome.failing_codes() == ["STA"]
    # DRC + LVS + security still ran and persisted — provenance is preserved.
    assert outcome.drc.gate_ok
    assert outcome.lvs.gate_ok
    assert outcome.security.gate_ok


# --------------------------------------------------------------------------- #
# Each individual leg can close the gate alone
# --------------------------------------------------------------------------- #
def test_drc_failure_alone_closes_gate(store: SqliteArtifactStore) -> None:
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_bad_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    outcome = driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0, layout_netlist_bytes=b"* ext\n",
    )
    assert not outcome.passed
    assert outcome.failing_codes() == ["DRC"]


def test_lvs_failure_alone_closes_gate(store: SqliteArtifactStore) -> None:
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_bad_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    outcome = driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0, layout_netlist_bytes=b"* ext\n",
    )
    assert not outcome.passed
    assert outcome.failing_codes() == ["LVS"]


def test_security_failure_alone_closes_gate(store: SqliteArtifactStore) -> None:
    # F6.4 AC: the security leg participates in the conjunction. A clean STA
    # + DRC + LVS combined with a backdoor-flagged SecurityReport still
    # closes the signoff gate.
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_bad_security()),
        store=store, design_id="d0",
    )
    outcome = driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0, layout_netlist_bytes=b"* ext\n",
    )
    assert not outcome.passed
    assert outcome.failing_codes() == ["SECURITY"]
    assert any(v.code == "SECURITY.ALWAYS_ON_NET"
               for v in outcome.security.violations)


def test_multiple_failures_show_in_order(store: SqliteArtifactStore) -> None:
    driver = SignoffStageDriver(
        sta=StubSTA(report=_bad_timing()),
        drc=StubDRC(report=_bad_drc()),
        lvs=StubLVS(report=_bad_lvs()),
        security=StubSecurity(report=_bad_security()),
        store=store, design_id="d0",
    )
    outcome = driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0, layout_netlist_bytes=b"* ext\n",
    )
    assert not outcome.passed
    assert outcome.failing_codes() == ["STA", "DRC", "LVS", "SECURITY"]


# --------------------------------------------------------------------------- #
# Plumbing: clock period + layout netlist bytes reach the right legs
# --------------------------------------------------------------------------- #
def test_clock_period_flows_to_sta_only(store: SqliteArtifactStore) -> None:
    sta = StubSTA(report=_ok_timing())
    drc = StubDRC(report=_ok_drc())
    lvs = StubLVS(report=_ok_lvs())
    security = StubSecurity(report=_ok_security())
    driver = SignoffStageDriver(sta=sta, drc=drc, lvs=lvs, security=security,
                                store=store, design_id="d0")
    driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=3.14, layout_netlist_bytes=b"* ext\n",
    )
    assert sta.calls == [3.14]
    # LVS got the extracted layout netlist bytes verbatim.
    assert lvs.calls[0][2] == b"* ext\n"
    # Security got the netlist (layout is passed but checks run on netlist text).
    assert len(security.calls) == 1


# --------------------------------------------------------------------------- #
# Contract: design_id must match both inputs
# --------------------------------------------------------------------------- #
def test_driver_rejects_design_id_mismatch_on_netlist(store: SqliteArtifactStore) -> None:
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    bad_netlist = _netlist(store, design_id="other")
    with pytest.raises(SignoffStageError):
        driver.drive(
            bad_netlist, _layout(store),
            clock_period_ns=5.0, layout_netlist_bytes=b"* ext\n",
        )


def test_driver_rejects_empty_design_id(store: SqliteArtifactStore) -> None:
    with pytest.raises(SignoffStageError):
        SignoffStageDriver(
            sta=StubSTA(report=_ok_timing()),
            drc=StubDRC(report=_ok_drc()),
            lvs=StubLVS(report=_ok_lvs()),
            security=StubSecurity(report=_ok_security()),
            store=store, design_id="",
        )


# --------------------------------------------------------------------------- #
# F12.1: when ``layout.librelane_sdf`` is set, the STA leg receives the
# blob bytes; when it's None, the STA leg sees ``sdf_bytes=None``.
# --------------------------------------------------------------------------- #
def _layout_with_sdf(
    store: SqliteArtifactStore, *, sdf_payload: bytes,
) -> LayoutArtifact:
    """A LayoutArtifact whose ``librelane_sdf`` ref points at ``sdf_payload``."""
    def_blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    sdf_blob = store.put_blob(sdf_payload, media_type="text/x-sdf")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=def_blob, stage_reached="routed",
        librelane_sdf=sdf_blob,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_sta_leg_receives_sdf_bytes_when_layout_carries_one(
    store: SqliteArtifactStore,
) -> None:
    sta = StubSTA(report=_ok_timing())
    driver = SignoffStageDriver(
        sta=sta,
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    sdf_payload = b"(DELAYFILE (SDFVERSION \"3.0\"))\n"
    layout = _layout_with_sdf(store, sdf_payload=sdf_payload)

    driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    assert sta.sdf_calls == [sdf_payload]


def test_sta_leg_receives_none_when_layout_has_no_sdf(
    store: SqliteArtifactStore,
) -> None:
    sta = StubSTA(report=_ok_timing())
    driver = SignoffStageDriver(
        sta=sta,
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )

    driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    assert sta.sdf_calls == [None]


# --------------------------------------------------------------------------- #
# F12.2: ``layout.librelane_drc_report`` flows into the DRC leg.
# --------------------------------------------------------------------------- #
def _layout_with_drc_report(
    store: SqliteArtifactStore, *, drc_report: bytes,
) -> LayoutArtifact:
    def_blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    rpt_blob = store.put_blob(drc_report, media_type="text/plain")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=def_blob, stage_reached="routed",
        librelane_drc_report=rpt_blob,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_drc_leg_receives_librelane_report_when_layout_carries_one(
    store: SqliteArtifactStore,
) -> None:
    drc = StubDRC(report=_ok_drc())
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=drc,
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    report_bytes = b"Total DRC errors found: 0\n"
    layout = _layout_with_drc_report(store, drc_report=report_bytes)

    driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    assert drc.librelane_calls == [report_bytes]


def test_drc_leg_receives_none_when_layout_has_no_drc_report(
    store: SqliteArtifactStore,
) -> None:
    drc = StubDRC(report=_ok_drc())
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=drc,
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    assert drc.librelane_calls == [None]


# --------------------------------------------------------------------------- #
# F12.3: LVS sources its layout netlist from ``layout.librelane_layout_spice``
# when present; falls back to the explicit ``layout_netlist_bytes`` arg
# otherwise; raises ``SignoffStageError`` when neither is available.
# --------------------------------------------------------------------------- #
def _layout_with_spice(
    store: SqliteArtifactStore, *, spice: bytes,
) -> LayoutArtifact:
    def_blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    spice_blob = store.put_blob(spice, media_type="text/x-spice")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=def_blob, stage_reached="routed",
        librelane_layout_spice=spice_blob,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_lvs_prefers_librelane_spice_over_explicit_bytes(
    store: SqliteArtifactStore,
) -> None:
    lvs = StubLVS(report=_ok_lvs())
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=lvs,
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    canonical = b"* real LibreLane extract\n.SUBCKT counter clk q\n.ENDS\n"
    layout = _layout_with_spice(store, spice=canonical)

    driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0,
        # Explicit bytes should be IGNORED when layout carries the spice ref.
        layout_netlist_bytes=b"* explicit bytes - should be ignored\n",
    )
    assert lvs.calls[0][2] == canonical


def test_lvs_falls_back_to_explicit_bytes_when_no_spice_ref(
    store: SqliteArtifactStore,
) -> None:
    """Older layouts (pre-F12.3) carry no spice ref; the explicit arg is
    still accepted as a fallback so the F11.x graph path keeps working."""
    lvs = StubLVS(report=_ok_lvs())
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=lvs,
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    explicit = b"* explicit bytes from graph context\n"
    driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0,
        layout_netlist_bytes=explicit,
    )
    assert lvs.calls[0][2] == explicit


def test_lvs_missing_both_sources_raises_signoff_error(
    store: SqliteArtifactStore,
) -> None:
    """No layout.librelane_layout_spice AND no explicit bytes = error.

    F12.3 closes the F11.7 "LVS silently passes on missing data" loophole.
    """
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    with pytest.raises(SignoffStageError, match="LVS requires"):
        driver.drive(
            _netlist(store), _layout(store),
            clock_period_ns=5.0,
            layout_netlist_bytes=b"",  # explicit empty
        )


# --------------------------------------------------------------------------- #
# F13.1: SIGNOFF prefers ``layout.librelane_mapped_netlist`` over the F6.x
# intermediate ``NetlistArtifact.netlist`` for both STA and LVS legs.
# --------------------------------------------------------------------------- #
def _layout_with_mapped_netlist(
    store: SqliteArtifactStore, *, mapped: bytes,
) -> LayoutArtifact:
    def_blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    spice_blob = store.put_blob(b"* spice\n", media_type="text/x-spice")
    mapped_blob = store.put_blob(mapped, media_type="text/x-verilog")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=def_blob, stage_reached="routed",
        librelane_layout_spice=spice_blob,
        librelane_mapped_netlist=mapped_blob,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_sta_leg_receives_mapped_netlist_when_layout_carries_one(
    store: SqliteArtifactStore,
) -> None:
    sta = StubSTA(report=_ok_timing())
    driver = SignoffStageDriver(
        sta=sta,
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    mapped = b"module counter; sky130_fd_sc_hd__dfrtp_2 _0_ (...); endmodule\n"
    layout = _layout_with_mapped_netlist(store, mapped=mapped)

    driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    assert sta.netlist_override_calls == [mapped]


def test_lvs_leg_receives_mapped_netlist_when_layout_carries_one(
    store: SqliteArtifactStore,
) -> None:
    lvs = StubLVS(report=_ok_lvs())
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=lvs,
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    mapped = b"module counter; sky130_fd_sc_hd__dfrtp_2 _0_ (...); endmodule\n"
    layout = _layout_with_mapped_netlist(store, mapped=mapped)

    driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    assert lvs.netlist_override_calls == [mapped]


def test_both_legs_receive_none_when_layout_has_no_mapped_netlist(
    store: SqliteArtifactStore,
) -> None:
    """Pre-F13.1 layouts (and the stub flow) leave the override as ``None``
    so the runners fall back to ``NetlistArtifact.netlist``."""
    sta = StubSTA(report=_ok_timing())
    lvs = StubLVS(report=_ok_lvs())
    driver = SignoffStageDriver(
        sta=sta,
        drc=StubDRC(report=_ok_drc()),
        lvs=lvs,
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    assert sta.netlist_override_calls == [None]
    assert lvs.netlist_override_calls == [None]


# --------------------------------------------------------------------------- #
# F13.4-B: SIGNOFF feeds the powered netlist to LVS while STA keeps reading
# the unpowered mapped netlist. Closes the LVS.UNKNOWN gap on live runs.
# --------------------------------------------------------------------------- #
def _layout_with_split_netlists(
    store: SqliteArtifactStore,
    *,
    mapped: bytes,
    powered: bytes,
) -> LayoutArtifact:
    """Build a LayoutArtifact carrying both mapped + powered netlists."""
    def_blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    spice_blob = store.put_blob(b"* spice\n", media_type="text/x-spice")
    mapped_blob = store.put_blob(mapped, media_type="text/x-verilog")
    powered_blob = store.put_blob(powered, media_type="text/x-verilog")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=def_blob, stage_reached="routed",
        librelane_layout_spice=spice_blob,
        librelane_mapped_netlist=mapped_blob,
        librelane_powered_netlist=powered_blob,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_lvs_receives_powered_netlist_when_layout_carries_one(
    store: SqliteArtifactStore,
) -> None:
    """F13.4-B: when both PNL and mapped are present, LVS gets the PNL."""
    sta = StubSTA(report=_ok_timing())
    lvs = StubLVS(report=_ok_lvs())
    driver = SignoffStageDriver(
        sta=sta,
        drc=StubDRC(report=_ok_drc()),
        lvs=lvs,
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    mapped = b"module counter (clk, rst_n, q); endmodule\n"
    powered = (
        b"module counter (VPWR, VGND, clk, q\\[0\\] , rst_n);\n"
        b"  input VPWR; input VGND; input clk; output q\\[0\\] ; input rst_n;\n"
        b"endmodule\n"
    )
    layout = _layout_with_split_netlists(store, mapped=mapped, powered=powered)

    driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    # LVS got the powered netlist; STA still got the unpowered mapped netlist.
    assert lvs.netlist_override_calls == [powered]
    assert sta.netlist_override_calls == [mapped]


def test_lvs_falls_back_to_mapped_netlist_when_no_powered_netlist(
    store: SqliteArtifactStore,
) -> None:
    """F13.4-B fallback: pre-PNL layouts give LVS the mapped netlist
    instead of None, so older harvest passes keep producing a verdict."""
    sta = StubSTA(report=_ok_timing())
    lvs = StubLVS(report=_ok_lvs())
    driver = SignoffStageDriver(
        sta=sta,
        drc=StubDRC(report=_ok_drc()),
        lvs=lvs,
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    mapped = b"module counter (clk, rst_n, q); endmodule\n"
    layout = _layout_with_mapped_netlist(store, mapped=mapped)

    driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )
    # Both legs get the mapped netlist — the PNL-absent fallback.
    assert lvs.netlist_override_calls == [mapped]
    assert sta.netlist_override_calls == [mapped]


# --------------------------------------------------------------------------- #
# F21.2-G/H — multi-corner SIGNOFF dispatch + handler integration.
#
# Dispatch: layout carries librelane_per_corner_timing → SIGNOFF builds a
# MultiCornerSTAReport instead of calling OpenSTAService; gate conjunction
# reads multi-corner gate_ok. Absent → today's single-corner path is
# byte-identical. Segfault fallback (#6227) surfaces on the outcome so
# the state-graph node can emit the audit event.
# --------------------------------------------------------------------------- #
from chip_agent.graph.signoff_handler import apply_signoff_outcome
from chip_agent.design_state import (
    DesignState,
    StageStatus,
    ArtifactKind,
)


def _layout_with_per_corner_blobs(
    store: SqliteArtifactStore,
    *,
    corner_reports: dict[str, bytes],
    power_reports: dict[str, bytes] | None = None,
    multi_corner_fallback: bool = False,
) -> LayoutArtifact:
    def_blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    spice_blob = store.put_blob(b"* spice\n", media_type="text/x-spice")
    per_corner = {
        corner: store.put_blob(b, media_type="text/plain")
        for corner, b in corner_reports.items()
    }
    per_corner_power = (
        {
            corner: store.put_blob(b, media_type="text/plain")
            for corner, b in (power_reports or {}).items()
        }
        if power_reports else None
    )
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=def_blob, stage_reached="routed",
        librelane_layout_spice=spice_blob,
        librelane_per_corner_timing=per_corner,
        librelane_per_corner_power=per_corner_power,
        metadata={
            "multi_corner_fallback": "true" if multi_corner_fallback else "false",
        },
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_signoff_dispatch_uses_multi_corner_path_when_layout_has_blobs(
    store: SqliteArtifactStore,
) -> None:
    """The layout carries per-corner timing → SIGNOFF parses those bytes
    into a MultiCornerSTAReport and does NOT call the STA service."""
    sta = StubSTA(report=_ok_timing())
    driver = SignoffStageDriver(
        sta=sta,
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    layout = _layout_with_per_corner_blobs(
        store,
        corner_reports={
            "tt": b"wns 0.5\ntns 0.0\n",
            "ss": b"wns 0.1\ntns 0.0\n",
            "ff": b"wns 1.1\ntns 0.0\n",
        },
    )

    outcome = driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )

    # Single-corner path was skipped — STA service not invoked.
    assert sta.calls == []
    # Multi-corner artifact populated; classic timing ref is None.
    assert outcome.timing is None
    assert outcome.timing_ref is None
    assert outcome.multi_corner_timing is not None
    assert outcome.multi_corner_timing_ref is not None
    assert outcome.multi_corner_timing.gate_ok
    assert outcome.passed
    # The persisted ref maps back to the MULTI_CORNER_STA kind.
    assert outcome.multi_corner_timing_ref.kind == ArtifactKind.MULTI_CORNER_STA


def test_signoff_dispatch_falls_back_to_single_corner_when_no_blobs(
    store: SqliteArtifactStore,
) -> None:
    """No per-corner blobs → today's OpenSTA path; outcome shape unchanged
    from pre-F21.2."""
    sta = StubSTA(report=_ok_timing())
    driver = SignoffStageDriver(
        sta=sta,
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )

    outcome = driver.drive(
        _netlist(store), _layout(store),
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )

    # Single-corner path fired; multi-corner stayed None.
    assert sta.calls == [5.0]
    assert outcome.timing is not None
    assert outcome.timing_ref is not None
    assert outcome.multi_corner_timing is None
    assert outcome.multi_corner_timing_ref is None
    assert outcome.multi_corner_fallback is False
    assert outcome.passed


def test_signoff_gate_uses_worst_corner_gate_ok(
    store: SqliteArtifactStore,
) -> None:
    """One bad corner closes the SIGNOFF gate even if the other two are clean."""
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),  # unreachable
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    layout = _layout_with_per_corner_blobs(
        store,
        corner_reports={
            "tt": b"wns 0.5\ntns 0.0\n",
            "ss": b"wns -0.2\ntns -0.4\n",  # WNS negative
            "ff": b"wns 1.1\ntns 0.0\n",
        },
    )

    outcome = driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )

    assert outcome.multi_corner_timing is not None
    assert outcome.multi_corner_timing.worst_wns_ns == -0.2
    assert not outcome.passed
    assert "STA" in outcome.failing_codes()


def test_signoff_outcome_surfaces_multi_corner_fallback_flag(
    store: SqliteArtifactStore,
) -> None:
    """Layout metadata marks the OpenROAD #6227 fallback; outcome carries
    the flag so the state-graph node can emit the audit event."""
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    # No per-corner blobs (segfall short-circuited harvest), but the flag
    # is stamped — SIGNOFF takes the single-corner path AND surfaces the
    # fallback so the audit log gets the event.
    blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    spice = store.put_blob(b"* spice\n", media_type="text/x-spice")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout", design_id="d0", module_id="counter",
        def_file=blob, stage_reached="routed",
        librelane_layout_spice=spice,
        metadata={"multi_corner_fallback": "true"},
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    layout = store.get_by_id(art.artifact_id)
    assert layout is not None

    outcome = driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )

    assert outcome.multi_corner_fallback is True
    assert outcome.timing is not None  # single-corner path fired
    assert outcome.multi_corner_timing is None


def test_handler_appends_multi_corner_timing_ref_to_ss_results(
    store: SqliteArtifactStore,
) -> None:
    """F21.2-H: when the dispatch produced a MultiCornerSTAReport, the
    handler appends THAT ref to ss.results (not the absent timing_ref)."""
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    layout = _layout_with_per_corner_blobs(
        store,
        corner_reports={"tt": b"wns 0.5\ntns 0.0\n"},
    )
    outcome = driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )

    state = DesignState(design_id="d0", name="counter-demo")
    ss = apply_signoff_outcome(state, outcome, store=store)

    # ss.results carries the multi-corner ref + DRC/LVS/security.
    assert outcome.multi_corner_timing_ref in ss.results
    assert outcome.drc_ref in ss.results
    assert outcome.lvs_ref in ss.results
    assert outcome.security_ref in ss.results
    # No phantom timing_ref of the single-corner kind.
    assert outcome.timing_ref is None
    assert ss.status == StageStatus.PASSED


def test_handler_first_failing_ref_points_to_multi_corner_artifact(
    store: SqliteArtifactStore,
) -> None:
    """When the multi-corner gate closes, ss.last_failure points at the
    MULTI_CORNER_STA ref so cross-stage feedback has the right
    attribution-shaped pointer."""
    driver = SignoffStageDriver(
        sta=StubSTA(report=_ok_timing()),
        drc=StubDRC(report=_ok_drc()),
        lvs=StubLVS(report=_ok_lvs()),
        security=StubSecurity(report=_ok_security()),
        store=store, design_id="d0",
    )
    layout = _layout_with_per_corner_blobs(
        store,
        corner_reports={
            "tt": b"wns 0.5\ntns 0.0\n",
            "ss": b"wns -0.2\ntns -0.4\n",
        },
    )
    outcome = driver.drive(
        _netlist(store), layout,
        clock_period_ns=5.0, layout_netlist_bytes=b"* extracted\n",
    )

    state = DesignState(design_id="d0", name="counter-demo")
    ss = apply_signoff_outcome(state, outcome, store=store)

    assert ss.status == StageStatus.FAILED
    assert ss.last_failure == outcome.multi_corner_timing_ref
