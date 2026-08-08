"""F1.1 acceptance: design_state schema imports clean and round-trips JSON.

Every Artifact subclass + DesignState must survive
``model_dump(mode='json') -> json.dumps -> json.loads -> model_validate``
without loss. Computed fields (``gate_ok``) recompute from the persisted
state. Content hashes are stable across equal-content artifacts and differ
when content fields differ.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    ArtifactStatus,
    AssertionSpec,
    BehaviorInvariant,
    BlobRef,
    ClockDomain,
    ContractArtifact,
    DesignConstraints,
    DesignPlan,
    DesignState,
    DRCReport,
    EscalationLevel,
    FailureDiagnosis,
    GDSIIArtifact,
    LayoutArtifact,
    LintResult,
    LVSReport,
    CornerTiming,
    ModelInvocation,
    ModuleDecl,
    ModuleState,
    MultiCornerSTAReport,
    PhysicalRepairRoute,
    PhysicalRepairRouteKind,
    TaskType,
    NetlistArtifact,
    OracleArtifact,
    OracleVerificationArtifact,
    Port,
    PortAssumption,
    Provenance,
    ResetSpec,
    RTLArtifact,
    SecurityReport,
    SimulationResult,
    Spec,
    Stage,
    StageState,
    StageStatus,
    StructuredInvariant,
    SynthesisReport,
    TestbenchArtifact,
    TimingReport,
    ToolVersion,
    Violation,
)


def _now() -> datetime:
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _spec_provenance() -> Provenance:
    return Provenance(produced_by=Stage.SPEC, started_at=_now(), ended_at=_now())


def _rtl_provenance(spec_ref: ArtifactRef) -> Provenance:
    return Provenance(
        produced_by=Stage.RTL,
        agent="rtl_specialist",
        model=ModelInvocation(provider="ollama", model="qwen2.5-coder:7b", temperature=0.8, seed=42),
        inputs=[spec_ref],
        config_hash="cfg-abc",
        seed=42,
        started_at=_now(),
        ended_at=_now(),
    )


def _spec(design_id: str = "d0") -> Spec:
    return Spec(
        artifact_id="d0.spec",
        design_id=design_id,
        raw_text="counter spec",
        normalized="A 4-bit synchronous counter with sync reset.",
        requirements=["4-bit", "clk/rst", "increments on rising edge"],
        constraints=DesignConstraints(pdk="sky130A", target_clock_ns=10.0),
        provenance=_spec_provenance(),
    )


def _rtl(spec_ref: ArtifactRef, *, design_id: str = "d0", artifact_id: str = "d0.counter.rtl") -> RTLArtifact:
    return RTLArtifact(
        artifact_id=artifact_id,
        design_id=design_id,
        module_id="counter",
        top_module="counter",
        source=BlobRef(path="ab/abcd", sha256="abcd", size_bytes=128, media_type="text/x-verilog"),
        provenance=_rtl_provenance(spec_ref),
    )


def _roundtrip(obj: object) -> object:
    """JSON-serialise via pydantic, deserialise, return the rehydrated model."""
    cls = type(obj)
    dumped = cls.model_dump(obj, mode="json")  # type: ignore[attr-defined]
    text = json.dumps(dumped)
    raw = json.loads(text)
    return cls.model_validate(raw)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Per-artifact round-trip coverage
# --------------------------------------------------------------------------- #
def test_spec_roundtrip() -> None:
    s = _spec()
    s.content_hash = s.compute_content_hash()
    again = _roundtrip(s)
    assert again == s


def test_design_plan_roundtrip() -> None:
    s = _spec()
    s.content_hash = s.compute_content_hash()
    plan = DesignPlan(
        artifact_id="d0.plan",
        design_id="d0",
        top_module_id="counter",
        modules=[
            ModuleDecl(
                module_id="counter",
                name="counter",
                description="4-bit counter",
                ports=[
                    Port(name="clk", direction="in"),
                    Port(name="rst", direction="in"),
                    Port(name="q", direction="out", width=4),
                ],
            )
        ],
        provenance=Provenance(produced_by=Stage.PLAN, inputs=[s.ref()]),
    )
    assert _roundtrip(plan) == plan


def test_rtl_artifact_roundtrip() -> None:
    s = _spec()
    s.content_hash = s.compute_content_hash()
    rtl = _rtl(s.ref())
    rtl.content_hash = rtl.compute_content_hash()
    assert _roundtrip(rtl) == rtl


def test_testbench_roundtrip() -> None:
    s = _spec()
    s.content_hash = s.compute_content_hash()
    tb = TestbenchArtifact(
        artifact_id="d0.counter.tb",
        design_id="d0",
        module_id="counter",
        target_module="counter",
        source=BlobRef(path="ab/abcd", sha256="abcd", size_bytes=256),
        provenance=Provenance(produced_by=Stage.RTL, inputs=[s.ref()]),
    )
    assert _roundtrip(tb) == tb


def test_netlist_roundtrip() -> None:
    n = NetlistArtifact(
        artifact_id="d0.top.netlist",
        design_id="d0",
        netlist=BlobRef(path="cd/cdef", sha256="cdef", size_bytes=512),
        std_cell_lib="sky130_fd_sc_hd",
        cell_count=42,
        area_um2=1234.5,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    assert _roundtrip(n) == n


def test_layout_roundtrip() -> None:
    layout = LayoutArtifact(
        artifact_id="d0.top.layout",
        design_id="d0",
        def_file=BlobRef(path="ef/eff0", sha256="eff0", size_bytes=4096),
        stage_reached="routed",
        die_area_um2=10000.0,
        utilization_pct=68.5,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    assert _roundtrip(layout) == layout


def test_gdsii_roundtrip() -> None:
    g = GDSIIArtifact(
        artifact_id="d0.top.gds",
        design_id="d0",
        gds=BlobRef(path="00/0011", sha256="0011", size_bytes=8192, media_type="application/octet-stream"),
        die_area_um2=10000.0,
        cell_count=420,
        provenance=Provenance(produced_by=Stage.GDSII),
    )
    assert _roundtrip(g) == g


@pytest.mark.parametrize(
    "ver",
    [
        LintResult(
            artifact_id="d0.counter.lint",
            design_id="d0",
            module_id="counter",
            passed=True,
            metrics={"warnings": 0.0},
            checker=ToolVersion(name="verible", version="0.0", container_digest="sha256:" + "a" * 64),
            provenance=Provenance(produced_by=Stage.RTL),
        ),
        SimulationResult(
            artifact_id="d0.counter.sim",
            design_id="d0",
            module_id="counter",
            passed=False,
            tests_total=10,
            tests_passed=9,
            coverage_pct=82.4,
            waveform=BlobRef(path="11/1122", sha256="1122", size_bytes=1024, media_type="application/vcd"),
            failing_assertions=["counter_tb.test_overflow:42"],
            violations=[
                Violation(code="ASSERT_FAIL", severity="error",
                          message="ack low at cycle 5, expected high",
                          location="counter_tb.sv:42")
            ],
            provenance=Provenance(produced_by=Stage.RTL),
        ),
        SynthesisReport(
            artifact_id="d0.top.synth_report",
            design_id="d0",
            passed=True,
            cell_count=42,
            area_um2=1234.5,
            inferred_latches=0,
            longest_path_ns=8.7,
            provenance=Provenance(produced_by=Stage.SYNTH),
        ),
        TimingReport(
            artifact_id="d0.top.sta",
            design_id="d0",
            passed=False,
            wns_ns=-1.2,
            tns_ns=-3.4,
            setup_violations=2,
            hold_violations=0,
            violations=[Violation(code="STA.SETUP", severity="error", message="setup violation on path X")],
            provenance=Provenance(produced_by=Stage.SIGNOFF),
        ),
        DRCReport(
            artifact_id="d0.top.drc",
            design_id="d0",
            passed=True,
            violation_count=0,
            provenance=Provenance(produced_by=Stage.SIGNOFF),
        ),
        LVSReport(
            artifact_id="d0.top.lvs",
            design_id="d0",
            passed=True,
            matched=True,
            mismatch_count=0,
            provenance=Provenance(produced_by=Stage.SIGNOFF),
        ),
        SecurityReport(
            artifact_id="d0.top.sec",
            design_id="d0",
            passed=True,
            checks_run=["always_on", "unreachable_fsm"],
            suspicious_structures=0,
            provenance=Provenance(produced_by=Stage.SIGNOFF),
        ),
    ],
    ids=["lint", "sim", "synth", "sta", "drc", "lvs", "security"],
)
def test_verification_artifact_roundtrip(ver: object) -> None:
    again = _roundtrip(ver)
    assert again == ver
    # gate_ok is computed; it must agree before and after the round-trip.
    assert again.gate_ok == ver.gate_ok  # type: ignore[attr-defined]


def test_failure_diagnosis_roundtrip() -> None:
    fd = FailureDiagnosis(
        artifact_id="d0.counter.diag",
        design_id="d0",
        module_id="counter",
        failing_signal="ack",
        cycle=5,
        expected="1",
        actual="0",
        suspected_cause="missing else branch in next-state logic",
        nl_summary="ack low at cycle 5; expected high after rst falls",
        target_stage=Stage.RTL,
        target_module="counter",
        provenance=Provenance(produced_by=Stage.RTL),
    )
    assert _roundtrip(fd) == fd


def test_failure_diagnosis_with_enriched_fields_roundtrips() -> None:
    """F20.6: an enriched diagnosis (all three new fields populated)
    survives JSON round-trip with no field drift."""
    fd = FailureDiagnosis(
        artifact_id="d0.counter.diag",
        design_id="d0",
        module_id="counter",
        failing_signal="q",
        cycle=4,
        expected="0x5",
        actual="0x4",
        nl_summary="q off by one at cycle 4",
        test_source=(
            "@cocotb.test()\n"
            "async def test_increment(dut):\n"
            "    assert int(dut.q.value) == cycle + 1\n"
        ),
        window_vcd_summary=(
            "Cycle 3: clk=1, q=0x3\n"
            "Cycle 4 (FAILURE): clk=1, q=0x4\n"
            "Cycle 5: clk=1, q=0x4\n"
        ),
        active_signals_at_failure_cycle={"clk": "1", "q": "0x4"},
        provenance=Provenance(produced_by=Stage.RTL),
    )
    again = _roundtrip(fd)
    assert again == fd


def test_failure_diagnosis_content_hash_changes_when_enriched_fields_differ() -> None:
    """F20.6: defensive — the three new fields MUST be part of the
    content hash. Two diagnoses identical except for one window line
    must hash differently; otherwise a stale cached diagnosis could
    masquerade as a fresh one and the outer-loop prompt would replay
    out-of-date context."""
    base_kwargs = dict(
        artifact_id="d0.counter.diag",
        design_id="d0",
        module_id="counter",
        failing_signal="q",
        cycle=4,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    a = FailureDiagnosis(
        **base_kwargs,
        window_vcd_summary="Cycle 4 (FAILURE): clk=1, q=0x4",
    )
    b = FailureDiagnosis(
        **base_kwargs,
        window_vcd_summary="Cycle 4 (FAILURE): clk=1, q=0x5",
    )
    assert a.compute_content_hash() != b.compute_content_hash()


# --------------------------------------------------------------------------- #
# DesignState round-trip
# --------------------------------------------------------------------------- #
def test_design_state_roundtrip() -> None:
    s = _spec()
    s.content_hash = s.compute_content_hash()
    rtl = _rtl(s.ref())
    rtl.content_hash = rtl.compute_content_hash()

    state = DesignState(
        design_id="d0",
        name="counter",
        spec=s.ref(),
        top_module_id="counter",
        modules={
            "counter": ModuleState(
                module_id="counter",
                name="counter",
                stages={
                    Stage.RTL: StageState(
                        stage=Stage.RTL,
                        status=StageStatus.PASSED,
                        head=rtl.ref(),
                        attempts=2,
                        escalation=EscalationLevel.INNER,
                    )
                },
            )
        },
        stages={Stage.SYNTH: StageState(stage=Stage.SYNTH)},
    )
    again = _roundtrip(state)
    assert again == state
    # Re-hydrated enums compare equal.
    assert again.modules["counter"].stages[Stage.RTL].escalation == EscalationLevel.INNER
    assert again.stages[Stage.SYNTH].status == StageStatus.PENDING


# --------------------------------------------------------------------------- #
# Content-hash stability
# --------------------------------------------------------------------------- #
def test_content_hash_is_stable_across_equal_content() -> None:
    a = _spec()
    b = _spec()
    # Lifecycle/provenance fields are NON_CONTENT and must not affect the hash.
    a.status = ArtifactStatus.DRAFT
    b.status = ArtifactStatus.ACCEPTED
    a.provenance = Provenance(produced_by=Stage.SPEC, notes="run-A")
    b.provenance = Provenance(produced_by=Stage.SPEC, notes="run-B")
    assert a.compute_content_hash() == b.compute_content_hash()


def test_content_hash_differs_when_content_differs() -> None:
    a = _spec()
    b = _spec()
    b.normalized = "A different normalized spec."
    assert a.compute_content_hash() != b.compute_content_hash()


def test_content_hash_excludes_version() -> None:
    a = _spec()
    b = _spec()
    a.version = 1
    b.version = 7
    assert a.compute_content_hash() == b.compute_content_hash()


# --------------------------------------------------------------------------- #
# F19.1 — ContractArtifact: schema round-trip + content-hash invariants.
#
# The test-first / oracle-driven workflow (M19) hinges on the contract being
# a stable, content-addressed artifact: F19.4 and F19.5 are independent
# router calls whose outputs are triangulated against it. Hash drift on
# lifecycle fields would force re-generation of oracle + assertions even
# when the contract content is unchanged — and would invalidate the demo
# manifest goldens on every run.
# --------------------------------------------------------------------------- #
def _contract(design_id: str = "d0", *, condition: str = "count_next == count + 1") -> ContractArtifact:
    """Fully-populated contract used as the canonical fixture."""
    return ContractArtifact(
        artifact_id="d0.counter.contract",
        design_id=design_id,
        module_id="counter",
        behavior_invariants=[
            BehaviorInvariant(
                name="increment_by_one",
                description="On every rising clk edge, count advances by one when en is high.",
                condition=condition,
            ),
            BehaviorInvariant(
                name="reset_clears_count",
                description="When rst is asserted, count returns to zero on the next clk edge.",
                condition="rst -> next(count) == 0",
            ),
        ],
        port_assumptions=[
            PortAssumption(port_name="clk", polarity="positive", notes="rising edge"),
            PortAssumption(
                port_name="count",
                expected_range=(0, 255),
                encoding="binary",
            ),
        ],
        clock_domains=[
            ClockDomain(name="clk", frequency_mhz=50.0, source="external"),
        ],
        reset=ResetSpec(
            name="rst",
            polarity="active_high",
            synchronicity="sync",
            affects=["count"],
        ),
        encoding={"is_pipelined": "false", "fsm_style": "binary"},
        ambiguity_notes=["spec did not specify behaviour at 8-bit overflow; assumed wrap-around"],
        provenance=Provenance(produced_by=Stage.PLAN, agent="contract_extractor"),
    )


def test_contract_roundtrip() -> None:
    """Mirrors test_layout_roundtrip: a fully-populated contract survives
    model_dump → json.dumps → json.loads → model_validate without loss."""
    c = _contract()
    c.content_hash = c.compute_content_hash()
    again = _roundtrip(c)
    assert again == c


def test_contract_content_hash_stable_across_equal_content() -> None:
    """Two contracts with identical content but differing lifecycle fields
    (status, provenance.notes, created_at) hash to the same value — the
    parent Artifact._NON_CONTENT_FIELDS exclusion is doing its job and
    ContractArtifact does not need a local override."""
    a = _contract()
    b = _contract()
    a.status = ArtifactStatus.DRAFT
    b.status = ArtifactStatus.ACCEPTED
    a.provenance = Provenance(produced_by=Stage.PLAN, notes="run-A")
    b.provenance = Provenance(produced_by=Stage.PLAN, notes="run-B")
    assert a.compute_content_hash() == b.compute_content_hash()


def test_contract_content_hash_changes_when_invariants_differ() -> None:
    """Defensive: a single character change inside a BehaviorInvariant
    condition MUST change the content hash. Pins that all four list-of-
    sub-model fields are part of the hashed payload, not accidentally
    excluded."""
    a = _contract()
    b = _contract(condition="count_next == count + 2")  # one-off-by-one bug
    assert a.compute_content_hash() != b.compute_content_hash()


def test_artifact_kind_contract_enum_value_is_contract() -> None:
    """Pin the StrEnum surface so a future renaming PR can't silently
    drift it — the value is what gets persisted in the SQLite kind column
    and what the store registry keys on."""
    assert ArtifactKind.CONTRACT == "contract"
    assert ArtifactKind.CONTRACT.value == "contract"


def test_contract_artifact_appears_in_store_registry() -> None:
    """The store deserialisation pipeline looks up ArtifactKind.CONTRACT
    in _KIND_TO_CLASS; without the registry entry, fetching a contract
    back from disk would fail with StoreError."""
    from chip_agent.store.sqlite_store import _KIND_TO_CLASS
    assert _KIND_TO_CLASS[ArtifactKind.CONTRACT] is ContractArtifact


def test_escalation_level_ordering_survives_json() -> None:
    # `__lt__` is overridden — make sure the rehydrated enum still orders.
    # F12.5 inserted EXHAUSTED between OUTER and HUMAN.
    s = StageState(stage=Stage.RTL, escalation=EscalationLevel.INNER)
    s2 = _roundtrip(s)
    assert s2.escalation < EscalationLevel.OUTER  # type: ignore[attr-defined]
    assert EscalationLevel.OUTER < EscalationLevel.EXHAUSTED
    assert EscalationLevel.EXHAUSTED < EscalationLevel.HUMAN
    assert EscalationLevel.INNER.escalated() == EscalationLevel.OUTER
    assert EscalationLevel.OUTER.escalated() == EscalationLevel.EXHAUSTED
    assert EscalationLevel.EXHAUSTED.escalated() == EscalationLevel.HUMAN
    assert EscalationLevel.HUMAN.escalated() == EscalationLevel.HUMAN  # terminal


# --------------------------------------------------------------------------- #
# F19.2 — OracleArtifact + AssertionSpec coverage.
#
# Mirrors the F19.1 ContractArtifact pattern: round-trip, content-hash
# stability across the carved-out rationale_notes field, content-hash
# divergence when the source blob changes, plus the AC-specific check
# that an oracle's BlobRef payload is exec'able as a Python module.
# --------------------------------------------------------------------------- #
_ORACLE_PY = b"def reference(stim):\n    return [c + 1 for c in stim]\n"
_ASSERTION_PY = b"def assert_positive(args):\n    stim, observed = args\n    return (all(o > 0 for o in observed), 'positive')\n"


def _oracle(
    *,
    source_sha: str = "0" * 64,
    source_bytes: int = len(_ORACLE_PY),
    rationale_notes: list[str] | None = None,
) -> OracleArtifact:
    """Fully-populated oracle used as the canonical fixture."""
    return OracleArtifact(
        artifact_id="d0.counter.oracle",
        design_id="d0",
        module_id="counter",
        source=BlobRef(
            path=f"{source_sha[:2]}/{source_sha}",
            sha256=source_sha,
            size_bytes=source_bytes,
            media_type="text/x-python",
        ),
        module_signature=[
            Port(name="clk", direction="in"),
            Port(name="rst", direction="in"),
            Port(name="count", direction="out", width=8),
        ],
        reference_fn_name="reference",
        rationale_notes=rationale_notes if rationale_notes is not None
            else ["resolved contract ambiguity: overflow wraps modulo 256"],
        provenance=Provenance(produced_by=Stage.RTL, agent="oracle_gen"),
    )


def _assertion_spec(
    *,
    callsite: str = "assert_positive",
    source_sha: str = "1" * 64,
    rationale_notes: list[str] | None = None,
) -> AssertionSpec:
    """Fully-populated assertion spec used as the canonical fixture."""
    return AssertionSpec(
        artifact_id="d0.counter.assertions",
        design_id="d0",
        module_id="counter",
        source=BlobRef(
            path=f"{source_sha[:2]}/{source_sha}",
            sha256=source_sha,
            size_bytes=len(_ASSERTION_PY),
            media_type="text/x-python",
        ),
        assertions=[
            StructuredInvariant(
                name="count_is_positive",
                callsite=callsite,
                description="Every observed count value is strictly positive.",
            ),
        ],
        rationale_notes=rationale_notes if rationale_notes is not None
            else ["asserts the contract's count_is_positive invariant"],
        provenance=Provenance(produced_by=Stage.RTL, agent="assertion_gen"),
    )


def test_oracle_artifact_roundtrip() -> None:
    """Mirrors test_contract_roundtrip: a fully-populated oracle survives
    model_dump → json.dumps → json.loads → model_validate without loss."""
    o = _oracle()
    o.content_hash = o.compute_content_hash()
    again = _roundtrip(o)
    assert again == o


def test_oracle_artifact_content_hash_stable_across_rationale_drift() -> None:
    """Two oracles identical except for rationale_notes must hash equal —
    the _NON_CONTENT_FIELDS override is doing its job. Without the
    exclusion, two F19.4 runs that emit identical reference code but
    different commentary would falsely refuse to dedupe."""
    a = _oracle(rationale_notes=["run-A picked wrap-around"])
    b = _oracle(rationale_notes=["run-B picked wrap-around (verbose)"])
    assert a.compute_content_hash() == b.compute_content_hash()


def test_oracle_artifact_content_hash_changes_when_source_differs() -> None:
    """Defensive: two oracles whose source BlobRef sha256 differs MUST
    hash differently. Pins that the source blob is part of the hashed
    payload, not accidentally excluded — losing this would let F19.6
    triangulate against a stale cached oracle."""
    a = _oracle(source_sha="a" * 64)
    b = _oracle(source_sha="b" * 64)
    assert a.compute_content_hash() != b.compute_content_hash()


def test_oracle_artifact_source_blob_can_be_execd_as_python_module(tmp_path) -> None:
    """F19.2 acceptance: write Python source to a content-addressed blob
    via the store, attach to OracleArtifact.source, fetch it back, compile
    + exec into a fresh namespace, look up the reference function by name,
    and call it. Production execution lives in F19.6's sandbox; this test
    only proves the wire format supports the AC."""
    from chip_agent.store import SqliteArtifactStore

    store = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "runs",
    )
    try:
        blob_ref = store.put_blob(_ORACLE_PY, media_type="text/x-python")
        oracle = OracleArtifact(
            artifact_id="d0.counter.oracle",
            design_id="d0",
            module_id="counter",
            source=blob_ref,
            module_signature=[Port(name="count", direction="out", width=8)],
            reference_fn_name="reference",
            provenance=Provenance(produced_by=Stage.RTL, agent="oracle_gen"),
        )

        source_bytes = store.get_blob(oracle.source)
        namespace: dict[str, object] = {}
        code = compile(source_bytes, "<oracle>", "exec")
        exec(code, namespace)

        reference = namespace[oracle.reference_fn_name]
        assert callable(reference)
        assert reference([0, 1, 2]) == [1, 2, 3]
    finally:
        store.close()


def test_assertion_spec_roundtrip() -> None:
    """Mirrors test_contract_roundtrip for AssertionSpec."""
    spec = _assertion_spec()
    spec.content_hash = spec.compute_content_hash()
    again = _roundtrip(spec)
    assert again == spec


def test_assertion_spec_content_hash_stable_across_rationale_drift() -> None:
    """Two assertion specs identical except for rationale_notes must hash
    equal — same _NON_CONTENT_FIELDS rationale as the oracle test above."""
    a = _assertion_spec(rationale_notes=["covers reset + increment paths"])
    b = _assertion_spec(rationale_notes=["covers reset + increment + wrap"])
    assert a.compute_content_hash() == b.compute_content_hash()


def test_assertion_spec_lists_at_least_one_invariant_in_full_example() -> None:
    """F19.2 AC shape check: the canonical fixture carries >=1 invariant.
    The runtime constraint that every emitted AssertionSpec must satisfy
    this lives in F19.5; here we only pin that the schema permits + the
    fixture honours it."""
    spec = _assertion_spec()
    assert len(spec.assertions) >= 1
    assert spec.assertions[0].callsite == "assert_positive"
    assert spec.assertions[0].name == "count_is_positive"


def test_artifact_kind_oracle_enum_value_is_oracle() -> None:
    """Pin the StrEnum surface so a future renaming PR can't silently
    drift the SQLite kind column or the store registry key."""
    assert ArtifactKind.ORACLE == "oracle"
    assert ArtifactKind.ORACLE.value == "oracle"


def test_artifact_kind_assertions_enum_value_is_assertions() -> None:
    """Same pin for ASSERTIONS."""
    assert ArtifactKind.ASSERTIONS == "assertions"
    assert ArtifactKind.ASSERTIONS.value == "assertions"


def test_oracle_and_assertion_artifacts_appear_in_store_registry() -> None:
    """Without the registry entries, get() on a freshly-fetched ORACLE or
    ASSERTIONS row would fail with StoreError."""
    from chip_agent.store.sqlite_store import _KIND_TO_CLASS
    assert _KIND_TO_CLASS[ArtifactKind.ORACLE] is OracleArtifact
    assert _KIND_TO_CLASS[ArtifactKind.ASSERTIONS] is AssertionSpec


# --------------------------------------------------------------------------- #
# F19.6 — OracleVerificationArtifact coverage.
#
# Mirror the F19.1 ContractArtifact pattern: round-trip, hash stability
# across the parent's _NON_CONTENT_FIELDS lifecycle exclusions, enum
# value pin, registry entry pin. The artifact has no diagnostic-only
# fields of its own; every typed field is part of identity.
# --------------------------------------------------------------------------- #
def _oracle_verification(
    *,
    passed: bool = True,
    assertions_passed: int = 3,
    extra_violations: list[Violation] | None = None,
) -> OracleVerificationArtifact:
    """Fully-populated verification artifact used as the canonical fixture."""
    oracle_ref = ArtifactRef(
        artifact_id="d0.counter.oracle",
        version=1,
        kind=ArtifactKind.ORACLE,
        content_hash="sha256:" + "a" * 64,
    )
    assertion_spec_ref = ArtifactRef(
        artifact_id="d0.counter.assertions",
        version=1,
        kind=ArtifactKind.ASSERTIONS,
        content_hash="sha256:" + "b" * 64,
    )
    return OracleVerificationArtifact(
        artifact_id="d0.counter.oracle_verification",
        design_id="d0",
        module_id="counter",
        passed=passed,
        oracle_ref=oracle_ref,
        assertion_spec_ref=assertion_spec_ref,
        stim_cycles=5,
        assertions_checked=3,
        assertions_passed=assertions_passed,
        metrics={"duration_s": 0.42},
        violations=list(extra_violations) if extra_violations else [],
        provenance=Provenance(
            produced_by=Stage.PLAN,
            agent="oracle_verifier",
            tool=ToolVersion(name="oracle_verifier", version="f19.6"),
            inputs=[oracle_ref, assertion_spec_ref],
        ),
    )


def test_oracle_verification_artifact_roundtrip() -> None:
    """Mirrors test_contract_roundtrip: a fully-populated verification
    survives model_dump → json.dumps → json.loads → model_validate."""
    v = _oracle_verification(
        passed=False,
        assertions_passed=2,
        extra_violations=[Violation(
            code="ORACLE.ASSERTION_DISAGREEMENT",
            severity="error",
            message="increment_by_one: expected q=3, got q=4",
            location="assert_increment_by_one",
            detail={"name": "increment_by_one", "callsite": "assert_increment_by_one"},
        )],
    )
    v.content_hash = v.compute_content_hash()
    again = _roundtrip(v)
    assert again == v


def test_oracle_verification_content_hash_stable_across_lifecycle_drift() -> None:
    """Two verifications identical except status / provenance.notes hash
    equal — parent Artifact._NON_CONTENT_FIELDS doing its job. Without
    this, a status mutation post-write would invalidate downstream
    cached references."""
    a = _oracle_verification()
    b = _oracle_verification()
    a.status = ArtifactStatus.DRAFT
    b.status = ArtifactStatus.ACCEPTED
    a.provenance = Provenance(produced_by=Stage.PLAN, notes="run-A")
    b.provenance = Provenance(produced_by=Stage.PLAN, notes="run-B")
    assert a.compute_content_hash() == b.compute_content_hash()


def test_artifact_kind_oracle_verification_enum_value() -> None:
    """Pin the StrEnum surface so a future renaming PR can't silently
    drift the SQLite kind column or the store registry key."""
    assert ArtifactKind.ORACLE_VERIFICATION == "oracle_verification"
    assert ArtifactKind.ORACLE_VERIFICATION.value == "oracle_verification"


def test_oracle_verification_artifact_appears_in_store_registry() -> None:
    """Without the registry entry, get() on a freshly-fetched
    ORACLE_VERIFICATION row would fail with StoreError."""
    from chip_agent.store.sqlite_store import _KIND_TO_CLASS
    assert (
        _KIND_TO_CLASS[ArtifactKind.ORACLE_VERIFICATION]
        is OracleVerificationArtifact
    )


# --------------------------------------------------------------------------- #
# F21.2 — MultiCornerSTAReport coverage.
#
# Mirrors the F19.6 OracleVerificationArtifact pattern: round-trip, content-
# hash stability across lifecycle drift, divergence on corner-data change,
# enum-surface pin, registry presence.
# --------------------------------------------------------------------------- #
def _multi_corner_sta(
    *,
    artifact_id: str = "d0.top.multi_sta",
    corners: list[CornerTiming] | None = None,
    passed: bool = True,
    violations: list[Violation] | None = None,
) -> MultiCornerSTAReport:
    return MultiCornerSTAReport(
        artifact_id=artifact_id,
        design_id="d0",
        corners=corners if corners is not None else [
            CornerTiming(corner="tt", wns_ns=0.5, tns_ns=0.0),
            CornerTiming(corner="ss", wns_ns=0.1, tns_ns=0.0),
            CornerTiming(corner="ff", wns_ns=1.1, tns_ns=0.0),
        ],
        passed=passed,
        violations=violations or [],
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def test_multi_corner_sta_roundtrip() -> None:
    """Mirrors test_oracle_verification_artifact_roundtrip: a fully-populated
    multi-corner STA report survives model_dump → JSON → reload."""
    m = _multi_corner_sta(
        passed=False,
        corners=[
            CornerTiming(corner="tt", wns_ns=0.5, tns_ns=0.0, power_metrics={"total": 1.2}),
            CornerTiming(corner="ss", wns_ns=-0.2, tns_ns=-0.4, setup_violations=2),
            CornerTiming(corner="ff", wns_ns=1.1, tns_ns=0.0, hold_violations=1),
        ],
        violations=[Violation(
            code="STA.SETUP_VIOLATION",
            severity="error",
            message="setup slack negative at corner=ss",
            location="corner=ss:path X",
            detail={"corner": "ss"},
        )],
    )
    m.content_hash = m.compute_content_hash()
    again = _roundtrip(m)
    assert again == m
    assert again.gate_ok == m.gate_ok  # type: ignore[attr-defined]


def test_multi_corner_sta_content_hash_stable_across_lifecycle_drift() -> None:
    """Two reports with identical corner data but differing status /
    provenance.notes hash equal — parent Artifact._NON_CONTENT_FIELDS does
    its job; no local override needed."""
    a = _multi_corner_sta()
    b = _multi_corner_sta()
    a.status = ArtifactStatus.DRAFT
    b.status = ArtifactStatus.ACCEPTED
    a.provenance = Provenance(produced_by=Stage.SIGNOFF, notes="run-A")
    b.provenance = Provenance(produced_by=Stage.SIGNOFF, notes="run-B")
    assert a.compute_content_hash() == b.compute_content_hash()


def test_multi_corner_sta_content_hash_changes_when_corner_data_differs() -> None:
    """Defensive: a single per-corner WNS change MUST change the hash.
    Pins that ``corners`` is part of the hashed payload, not accidentally
    excluded."""
    a = _multi_corner_sta()
    b = _multi_corner_sta(corners=[
        CornerTiming(corner="tt", wns_ns=0.5, tns_ns=0.0),
        CornerTiming(corner="ss", wns_ns=0.05, tns_ns=0.0),  # one-off-by-0.05
        CornerTiming(corner="ff", wns_ns=1.1, tns_ns=0.0),
    ])
    assert a.compute_content_hash() != b.compute_content_hash()


def test_multi_corner_sta_worst_slack_aggregation() -> None:
    """Pin the computed-field semantics: worst_wns / worst_tns return the
    minimum across corners with parsed (non-None) values; an empty list
    or all-None values yield None."""
    populated = _multi_corner_sta(corners=[
        CornerTiming(corner="tt", wns_ns=0.5, tns_ns=0.0),
        CornerTiming(corner="ss", wns_ns=-0.2, tns_ns=-0.4),
        CornerTiming(corner="ff", wns_ns=1.1, tns_ns=0.0),
    ])
    assert populated.worst_wns_ns == -0.2
    assert populated.worst_tns_ns == -0.4

    empty = _multi_corner_sta(corners=[])
    assert empty.worst_wns_ns is None
    assert empty.worst_tns_ns is None

    all_none = _multi_corner_sta(corners=[CornerTiming(corner="tt")])
    assert all_none.worst_wns_ns is None


def test_artifact_kind_multi_corner_sta_enum_value() -> None:
    """Pin the StrEnum surface so a future renaming PR can't silently
    drift the SQLite kind column or the store registry key."""
    assert ArtifactKind.MULTI_CORNER_STA == "multi_corner_sta"
    assert ArtifactKind.MULTI_CORNER_STA.value == "multi_corner_sta"


def test_multi_corner_sta_appears_in_store_registry() -> None:
    """Without the registry entry, get() on a freshly-fetched
    MULTI_CORNER_STA row would fail with StoreError."""
    from chip_agent.store.sqlite_store import _KIND_TO_CLASS
    assert _KIND_TO_CLASS[ArtifactKind.MULTI_CORNER_STA] is MultiCornerSTAReport


def test_physical_repair_route_kind_enum_surface() -> None:
    """F21.3: pin the four route values so a future renaming PR can't
    silently drift the audit-log payload or routing-config key."""
    assert PhysicalRepairRouteKind.LOWER_DENSITY == "lower_density"
    assert PhysicalRepairRouteKind.INCREASE_DELAY_OPTIMIZATION == "increase_delay_optimization"
    assert PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD == "relax_clock_period"
    assert PhysicalRepairRouteKind.ESCALATE_HUMAN == "escalate_human"


def test_physical_repair_route_is_frozen() -> None:
    """F21.3: PhysicalRepairRoute is frozen so the history list on
    DesignState stays hashable-by-comparison. Two routes with the same
    content compare equal — used by the dispatcher to detect "already
    tried this route" without an extra set/seen-list."""
    r = PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
        reason="ss corner WNS negative",
    )
    with pytest.raises(ValidationError):
        r.reason = "mutated"  # type: ignore[misc]
    assert r == PhysicalRepairRoute(
        kind=PhysicalRepairRouteKind.LOWER_DENSITY,
        reason="ss corner WNS negative",
    )


def test_physical_repair_route_roundtrips_through_design_state() -> None:
    """The route list rides on the LangGraph checkpoint via DesignState;
    pin the JSON round-trip so a checkpoint reload reconstructs it."""
    ds = DesignState(
        design_id="d0",
        name="counter",
        physical_repair_routes=[
            PhysicalRepairRoute(
                kind=PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD,
                reason="attempt 1: bump clock 10%",
            ),
            PhysicalRepairRoute(
                kind=PhysicalRepairRouteKind.LOWER_DENSITY,
                reason="attempt 2: ss corner still tight",
            ),
        ],
    )
    again = _roundtrip(ds)
    assert again.physical_repair_routes == ds.physical_repair_routes
    assert again.physical_repair_routes[0].kind == PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD


def test_design_state_physical_repair_routes_defaults_empty() -> None:
    """Pre-F21.3 path: no routes → byte-identical state shape."""
    ds = DesignState(design_id="d0", name="counter")
    assert ds.physical_repair_routes == []


def test_task_type_physical_repair_routing_value() -> None:
    """F21.3: routing-config bindings + stub matchers key on this value."""
    assert TaskType.PHYSICAL_REPAIR_ROUTING == "physical_repair_routing"


def test_event_type_physical_repair_routed_value() -> None:
    """F21.3: the audit event the dispatcher emits per route."""
    from chip_agent.obs.audit_log import EventType
    assert EventType.PHYSICAL_REPAIR_ROUTED == "physical_repair_routed"


def test_layout_per_corner_fields_excluded_from_content_hash() -> None:
    """F21.2-C: ``librelane_per_corner_timing`` and
    ``librelane_per_corner_power`` are analysis OUTPUT over the layout,
    not part of its identity. Two LayoutArtifacts with identical DEF +
    metrics but differing per-corner harvest payloads must produce the
    SAME content hash so demo goldens stay byte-identical across the
    F21.2 schema bump."""
    def_blob = BlobRef(media_type="application/octet-stream", path="d.def", sha256="0" * 64, size_bytes=42)
    tt_blob = BlobRef(media_type="text/plain", path="sta_tt.rpt", sha256="1" * 64, size_bytes=10)
    ss_blob = BlobRef(media_type="text/plain", path="sta_ss.rpt", sha256="2" * 64, size_bytes=10)

    a = LayoutArtifact(
        artifact_id="d0.top.layout",
        design_id="d0",
        def_file=def_blob,
        die_area_um2=1000.0,
        cell_count=42,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    b = LayoutArtifact(
        artifact_id="d0.top.layout",
        design_id="d0",
        def_file=def_blob,
        die_area_um2=1000.0,
        cell_count=42,
        librelane_per_corner_timing={"tt": tt_blob, "ss": ss_blob},
        librelane_per_corner_power={"tt": tt_blob},
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    assert a.compute_content_hash() == b.compute_content_hash()
