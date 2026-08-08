r"""F20.1 schema round-trip: RepairAttempt artifact.

Two narrow tests pin:

1. Persisting + reloading a ``RepairAttempt`` yields back every
   field bit-identical to the constructor input.
2. Two ``RepairAttempt``\s identical in every field except
   ``rationale`` have **different** content hashes — the rationale
   IS part of the artifact's identity (no ``_NON_CONTENT_FIELDS``
   override).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    Provenance,
    RepairAttempt,
    Stage,
)
from chip_agent.store import SqliteArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


def _ref(suffix: str, kind: ArtifactKind) -> ArtifactRef:
    """Synthetic ArtifactRef. The store doesn't dereference it — for
    these tests we only need the ref to be Pydantic-valid."""
    return ArtifactRef(
        artifact_id=f"d0.counter.{suffix}",
        version=1,
        kind=kind,
        content_hash=f"sha256:{'a' * 60}{suffix[-4:].zfill(4)}",
    )


def _make_attempt(
    *, rationale: str, attempt_index: int = 1,
) -> RepairAttempt:
    return RepairAttempt(
        artifact_id=(
            f"d0.counter.repair_attempt_{attempt_index}"
        ),
        design_id="d0",
        module_id="counter",
        attempt_index=attempt_index,
        previous_rtl_ref=_ref("rtl_prev", ArtifactKind.RTL),
        new_rtl_ref=_ref("rtl_new", ArtifactKind.RTL),
        diagnosis_ref=_ref("diag", ArtifactKind.DIAGNOSIS),
        rationale=rationale,
        provenance=Provenance(produced_by=Stage.RTL, agent="rtl_stage"),
    )


def test_repair_attempt_roundtrips_through_store(
    store: SqliteArtifactStore,
) -> None:
    """Persist + reload yields a structurally identical artifact."""
    original = _make_attempt(
        rationale="Tried inverting the reset; if this fails, examine en sampling.",
        attempt_index=2,
    )
    ref = store.put(original)
    loaded = store.get(ref)
    assert isinstance(loaded, RepairAttempt)
    assert loaded.attempt_index == 2
    assert loaded.previous_rtl_ref == original.previous_rtl_ref
    assert loaded.new_rtl_ref == original.new_rtl_ref
    assert loaded.diagnosis_ref == original.diagnosis_ref
    assert loaded.rationale == original.rationale
    assert loaded.kind is ArtifactKind.REPAIR_ATTEMPT


def test_repair_attempt_rationale_is_part_of_content_hash(
    store: SqliteArtifactStore,
) -> None:
    """Two attempts that differ ONLY in rationale text must produce
    different content hashes — the rationale carries information
    (the model's hypothesis) that the next iteration consumes.
    """
    a = _make_attempt(rationale="Hypothesis A.")
    b = _make_attempt(rationale="Hypothesis B.")
    assert a.compute_content_hash() != b.compute_content_hash()
