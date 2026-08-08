"""F1.2 + F1.3 acceptance: SqliteArtifactStore round-trips, dedupes, lineage walks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chip_agent.design_state import (
    ArtifactKind,
    ArtifactStatus,
    AssertionSpec,
    BehaviorInvariant,
    BlobRef,
    ContractArtifact,
    DesignConstraints,
    OracleArtifact,
    OracleVerificationArtifact,
    Port,
    PortAssumption,
    Provenance,
    ResetSpec,
    RTLArtifact,
    SimulationResult,
    Spec,
    Stage,
    StructuredInvariant,
    Violation,
)
from chip_agent.store import SqliteArtifactStore, StoreError


def _now() -> datetime:
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "runs",
    )
    yield s
    s.close()


def _spec(*, design_id: str = "d0", artifact_id: str = "d0.spec", text: str = "counter") -> Spec:
    return Spec(
        artifact_id=artifact_id,
        design_id=design_id,
        raw_text=text,
        normalized=f"normalized: {text}",
        requirements=[text],
        constraints=DesignConstraints(pdk="sky130A"),
        provenance=Provenance(produced_by=Stage.SPEC, started_at=_now(), ended_at=_now()),
    )


def _rtl(spec: Spec, *, artifact_id: str = "d0.counter.rtl",
         source_text: str = "module counter; endmodule") -> RTLArtifact:
    return RTLArtifact(
        artifact_id=artifact_id,
        design_id=spec.design_id,
        module_id="counter",
        top_module="counter",
        source=BlobRef(
            path="00/0000",
            sha256="0" * 64,
            size_bytes=len(source_text),
            media_type="text/x-verilog",
        ),
        provenance=Provenance(produced_by=Stage.RTL, inputs=[spec.ref()]),
    )


# ----------------------------------------------------------------- core put/get
def test_put_round_trip(store: SqliteArtifactStore) -> None:
    s = _spec()
    ref = store.put(s)
    assert ref.artifact_id == "d0.spec"
    assert ref.version == 1
    assert ref.content_hash.startswith("sha256:")

    got = store.get(ref)
    assert isinstance(got, Spec)
    assert got.raw_text == s.raw_text
    assert got.content_hash == ref.content_hash


def test_put_is_idempotent_for_identical_content(store: SqliteArtifactStore) -> None:
    s1 = _spec()
    s2 = _spec()
    r1 = store.put(s1)
    r2 = store.put(s2)
    # Same content -> same hash -> same row; no new version row.
    assert r1 == r2
    assert store.history("d0.spec") == [store.get(r1)]


def test_put_modified_content_creates_new_version(store: SqliteArtifactStore) -> None:
    s1 = _spec()
    s2 = _spec(text="counter-v2")
    r1 = store.put(s1)
    r2 = store.put(s2)
    assert r1.version == 1
    assert r2.version == 2
    assert r1.content_hash != r2.content_hash

    hist = store.history("d0.spec")
    assert [a.version for a in hist] == [1, 2]


def test_put_same_content_different_logical_id_dedupes_body(
    store: SqliteArtifactStore, tmp_path: Path
) -> None:
    # Same Spec content under two artifact_ids: distinct rows, one JSON file.
    a = _spec(artifact_id="d0.spec.A")
    b = _spec(artifact_id="d0.spec.B")
    ra = store.put(a)
    rb = store.put(b)
    assert ra.content_hash == rb.content_hash
    assert ra.artifact_id != rb.artifact_id

    body_dir = tmp_path / "runs"
    hex_ = ra.content_hash.removeprefix("sha256:")
    body = body_dir / hex_[:2] / f"{hex_}.json"
    assert body.exists()
    # No second file with the same hash.
    same_hash_files = list((body_dir / hex_[:2]).glob(f"{hex_}*.json"))
    assert len(same_hash_files) == 1


def test_get_by_id_returns_latest_by_default(store: SqliteArtifactStore) -> None:
    store.put(_spec(text="v1"))
    store.put(_spec(text="v2"))
    latest = store.get_by_id("d0.spec")
    assert latest.version == 2
    assert isinstance(latest, Spec)
    assert latest.raw_text == "v2"


def test_get_by_id_specific_version(store: SqliteArtifactStore) -> None:
    store.put(_spec(text="v1"))
    store.put(_spec(text="v2"))
    v1 = store.get_by_id("d0.spec", version=1)
    assert v1.version == 1
    assert isinstance(v1, Spec)
    assert v1.raw_text == "v1"


def test_get_unknown_ref_raises(store: SqliteArtifactStore) -> None:
    from chip_agent.design_state import ArtifactRef
    with pytest.raises(StoreError):
        store.get(ArtifactRef(
            artifact_id="missing", version=1, kind=ArtifactKind.SPEC,
            content_hash="sha256:" + "0" * 64,
        ))
    with pytest.raises(StoreError):
        store.get_by_id("missing")


# ----------------------------------------------------------------- list / kind filter
def test_list_filters_by_design_and_kind(store: SqliteArtifactStore) -> None:
    s = _spec()
    spec_ref = store.put(s)
    rtl_ref = store.put(_rtl(s))
    other = _spec(design_id="d1", artifact_id="d1.spec")
    other_ref = store.put(other)

    all_d0 = store.list("d0")
    assert {r.artifact_id for r in all_d0} == {"d0.spec", "d0.counter.rtl"}
    specs_d0 = store.list("d0", kind=ArtifactKind.SPEC)
    assert specs_d0 == [spec_ref]
    rtls_d0 = store.list("d0", kind=ArtifactKind.RTL)
    assert rtls_d0 == [rtl_ref]
    only_d1 = store.list("d1")
    assert only_d1 == [other_ref]


# ----------------------------------------------------------------- blobs
def test_put_blob_round_trip_and_dedupes(
    store: SqliteArtifactStore, tmp_path: Path
) -> None:
    data = b"module counter; endmodule\n"
    ref1 = store.put_blob(data, media_type="text/x-verilog")
    ref2 = store.put_blob(data, media_type="text/x-verilog")
    assert ref1 == ref2  # same content -> same ref
    assert ref1.path.startswith(f"{ref1.sha256[:2]}/")
    assert ref1.size_bytes == len(data)

    got = store.get_blob(ref1)
    assert got == data

    on_disk = tmp_path / "runs" / ref1.path
    assert on_disk.exists()
    # Path scheme matches the AC: runs/<hex[:2]>/<hex>
    assert on_disk.parent.name == ref1.sha256[:2]
    assert on_disk.name == ref1.sha256


def test_get_blob_detects_corruption(
    store: SqliteArtifactStore, tmp_path: Path
) -> None:
    ref = store.put_blob(b"hello")
    (tmp_path / "runs" / ref.path).write_bytes(b"tampered")
    with pytest.raises(StoreError):
        store.get_blob(ref)


# ----------------------------------------------------------------- status
def test_set_status_updates_index_only(
    store: SqliteArtifactStore, tmp_path: Path
) -> None:
    s = _spec()
    ref = store.put(s)
    body_before = (
        tmp_path / "runs"
        / ref.content_hash.removeprefix("sha256:")[:2]
        / f"{ref.content_hash.removeprefix('sha256:')}.json"
    ).read_bytes()
    store.set_status(ref, ArtifactStatus.ACCEPTED)
    got = store.get(ref)
    assert got.status is ArtifactStatus.ACCEPTED
    body_after = (
        tmp_path / "runs"
        / ref.content_hash.removeprefix("sha256:")[:2]
        / f"{ref.content_hash.removeprefix('sha256:')}.json"
    ).read_bytes()
    # On-disk content unchanged: status lives in the SQLite row.
    assert body_before == body_after


def test_set_status_unknown_ref_raises(store: SqliteArtifactStore) -> None:
    from chip_agent.design_state import ArtifactRef
    with pytest.raises(StoreError):
        store.set_status(
            ArtifactRef(artifact_id="x", version=1, kind=ArtifactKind.SPEC,
                        content_hash="sha256:" + "0" * 64),
            ArtifactStatus.ACCEPTED,
        )


# ----------------------------------------------------------------- F1.3 lineage
def test_lineage_walks_inputs_oldest_first(store: SqliteArtifactStore) -> None:
    spec = _spec()
    spec_ref = store.put(spec)

    rtl = _rtl(spec)
    rtl_ref = store.put(rtl)

    sim = SimulationResult(
        artifact_id="d0.counter.sim",
        design_id="d0",
        module_id="counter",
        passed=False,
        tests_total=10,
        tests_passed=9,
        violations=[Violation(code="ASSERT_FAIL", severity="error", message="x")],
        provenance=Provenance(produced_by=Stage.RTL, inputs=[rtl_ref]),
    )
    sim_ref = store.put(sim)

    chain = store.lineage(sim_ref)
    ids = [a.artifact_id for a in chain]
    # Spec -> RTL -> Sim, inputs before consumers.
    assert ids == ["d0.spec", "d0.counter.rtl", "d0.counter.sim"]
    assert [a.kind for a in chain] == [
        ArtifactKind.SPEC, ArtifactKind.RTL, ArtifactKind.SIM,
    ]
    assert chain[0].ref() == spec_ref


def test_lineage_handles_diamond_dependency(store: SqliteArtifactStore) -> None:
    spec = _spec()
    store.put(spec)
    rtl_a = _rtl(spec, artifact_id="d0.modA.rtl")
    rtl_b = _rtl(spec, artifact_id="d0.modB.rtl", source_text="module b; endmodule")
    a_ref = store.put(rtl_a)
    b_ref = store.put(rtl_b)
    # An integrating artifact that consumes both; spec sits at the diamond root.
    sim = SimulationResult(
        artifact_id="d0.top.sim",
        design_id="d0",
        passed=True,
        tests_total=1,
        tests_passed=1,
        provenance=Provenance(produced_by=Stage.RTL, inputs=[a_ref, b_ref]),
    )
    sim_ref = store.put(sim)

    chain = store.lineage(sim_ref)
    ids = [a.artifact_id for a in chain]
    # Spec appears exactly once even though both RTLs reference it.
    assert ids.count("d0.spec") == 1
    # Sim is last; spec is first.
    assert ids[0] == "d0.spec"
    assert ids[-1] == "d0.top.sim"


def test_lineage_single_root(store: SqliteArtifactStore) -> None:
    spec = _spec()
    ref = store.put(spec)
    assert store.lineage(ref) == [store.get(ref)]


# ---------------------------------------------------------------- F19.1
def test_contract_artifact_round_trips_through_store(
    store: SqliteArtifactStore,
) -> None:
    """End-to-end: ContractArtifact survives put → get with structural and
    content-hash equality. Confirms the F19.1 registry wiring
    (_KIND_TO_CLASS) plus the sqlite kind column round-trip.
    """
    contract = ContractArtifact(
        artifact_id="d0.counter.contract",
        design_id="d0",
        module_id="counter",
        behavior_invariants=[
            BehaviorInvariant(
                name="increment_by_one",
                description="On rising clk, count advances by 1 when en is high.",
                condition="en -> next(count) == count + 1",
            ),
        ],
        port_assumptions=[
            PortAssumption(port_name="clk", polarity="positive"),
        ],
        reset=ResetSpec(
            name="rst", polarity="active_high", synchronicity="sync",
            affects=["count"],
        ),
        encoding={"is_pipelined": "false"},
        ambiguity_notes=["spec did not specify overflow; assumed wrap"],
        provenance=Provenance(produced_by=Stage.PLAN, agent="contract_extractor"),
    )

    ref = store.put(contract)
    assert ref.kind is ArtifactKind.CONTRACT
    assert ref.content_hash.startswith("sha256:")

    got = store.get(ref)
    assert isinstance(got, ContractArtifact)
    assert got.behavior_invariants == contract.behavior_invariants
    assert got.reset == contract.reset
    assert got.encoding == contract.encoding
    assert got.ambiguity_notes == contract.ambiguity_notes
    assert got.content_hash == ref.content_hash


# ---------------------------------------------------------------- F19.2
_ORACLE_PY = b"def reference(stim):\n    return [c + 1 for c in stim]\n"
_ASSERTION_PY = b"def assert_positive(args):\n    stim, observed = args\n    return (all(o > 0 for o in observed), 'positive')\n"


def test_oracle_artifact_round_trips_through_store(
    store: SqliteArtifactStore,
) -> None:
    """End-to-end: OracleArtifact survives put → get with structural and
    content-hash equality. Uses a real blob written via put_blob so the
    BlobRef the artifact carries actually points at on-disk Python source
    (the F19.6 triangulation gate's read path)."""
    blob_ref = store.put_blob(_ORACLE_PY, media_type="text/x-python")
    oracle = OracleArtifact(
        artifact_id="d0.counter.oracle",
        design_id="d0",
        module_id="counter",
        source=blob_ref,
        module_signature=[
            Port(name="clk", direction="in"),
            Port(name="count", direction="out", width=8),
        ],
        reference_fn_name="reference",
        rationale_notes=["resolved overflow ambiguity: wrap modulo 256"],
        provenance=Provenance(produced_by=Stage.RTL, agent="oracle_gen"),
    )

    ref = store.put(oracle)
    assert ref.kind is ArtifactKind.ORACLE
    assert ref.content_hash.startswith("sha256:")

    got = store.get(ref)
    assert isinstance(got, OracleArtifact)
    assert got.source == oracle.source
    assert got.module_signature == oracle.module_signature
    assert got.reference_fn_name == oracle.reference_fn_name
    assert got.rationale_notes == oracle.rationale_notes
    assert got.content_hash == ref.content_hash

    # The blob the artifact references really exists and round-trips.
    assert store.get_blob(got.source) == _ORACLE_PY


def test_assertion_spec_round_trips_through_store(
    store: SqliteArtifactStore,
) -> None:
    """End-to-end mirror of the OracleArtifact test for AssertionSpec.
    Confirms the registry maps ASSERTIONS to AssertionSpec and that the
    StructuredInvariant list survives the SQLite JSON round-trip."""
    blob_ref = store.put_blob(_ASSERTION_PY, media_type="text/x-python")
    spec = AssertionSpec(
        artifact_id="d0.counter.assertions",
        design_id="d0",
        module_id="counter",
        source=blob_ref,
        assertions=[
            StructuredInvariant(
                name="count_is_positive",
                callsite="assert_positive",
                description="Every observed count value is strictly positive.",
            ),
        ],
        rationale_notes=["asserts the contract's count_is_positive invariant"],
        provenance=Provenance(produced_by=Stage.RTL, agent="assertion_gen"),
    )

    ref = store.put(spec)
    assert ref.kind is ArtifactKind.ASSERTIONS
    assert ref.content_hash.startswith("sha256:")

    got = store.get(ref)
    assert isinstance(got, AssertionSpec)
    assert got.source == spec.source
    assert got.assertions == spec.assertions
    assert got.rationale_notes == spec.rationale_notes
    assert got.content_hash == ref.content_hash

    assert store.get_blob(got.source) == _ASSERTION_PY


# ---------------------------------------------------------------- F19.6
def test_oracle_verification_artifact_round_trips_through_store(
    store: SqliteArtifactStore,
) -> None:
    """End-to-end: OracleVerificationArtifact survives put → get with
    structural and content-hash equality. Confirms the F19.6 registry
    wiring (_KIND_TO_CLASS) plus the SQLite kind column round-trip.
    """
    from chip_agent.design_state import ArtifactRef, ToolVersion

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
    artifact = OracleVerificationArtifact(
        artifact_id="d0.counter.oracle_verification",
        design_id="d0",
        module_id="counter",
        passed=True,
        oracle_ref=oracle_ref,
        assertion_spec_ref=assertion_spec_ref,
        stim_cycles=5,
        assertions_checked=3,
        assertions_passed=3,
        checker=ToolVersion(name="oracle_verifier", version="f19.6"),
        provenance=Provenance(
            produced_by=Stage.PLAN,
            agent="oracle_verifier",
            tool=ToolVersion(name="oracle_verifier", version="f19.6"),
            inputs=[oracle_ref, assertion_spec_ref],
        ),
    )

    ref = store.put(artifact)
    assert ref.kind is ArtifactKind.ORACLE_VERIFICATION
    assert ref.content_hash.startswith("sha256:")

    got = store.get(ref)
    assert isinstance(got, OracleVerificationArtifact)
    assert got.oracle_ref == artifact.oracle_ref
    assert got.assertion_spec_ref == artifact.assertion_spec_ref
    assert got.stim_cycles == 5
    assert got.assertions_passed == 3
    assert got.passed is True
    assert got.gate_ok is True
    assert got.content_hash == ref.content_hash
