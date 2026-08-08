r"""F23.1 schema round-trip: HumanHint artifact.

Pins:

1. Persist + reload yields every content field bit-identical, typed as
   :class:`HumanHint` (store kind→class dispatch is registered).
2. ``raw_transcript`` is EXCLUDED from the content hash — two hints with
   identical distilled guidance but differently-worded chats dedupe.
3. ``summary`` IS part of the content hash — different guidance is a
   different artifact.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    HumanHint,
    HumanHintKind,
    Provenance,
    ReflectionRouteKind,
    Stage,
)
from chip_agent.store import SqliteArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteArtifactStore]:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


def _make_hint(
    *,
    summary: str = "The final addRoundKey (post-whitening XOR) is missing.",
    raw_transcript: str = "operator: you never XOR the 32nd subkey at the end",
    hint_kind: HumanHintKind = HumanHintKind.POINT_AT_BUG,
) -> HumanHint:
    return HumanHint(
        artifact_id="d0.present80.hint",
        design_id="d0",
        module_id="present80",
        hint_kind=hint_kind,
        target_stage=Stage.RTL,
        summary=summary,
        suggested_route=ReflectionRouteKind.REGEN_CURRENT_RTL,
        references=[
            ArtifactRef(
                artifact_id="d0.present80.diagnosis",
                version=1,
                kind=ArtifactKind.DIAGNOSIS,
                content_hash=f"sha256:{'a' * 64}",
            ),
        ],
        raw_transcript=raw_transcript,
        provenance=Provenance(produced_by=Stage.RTL, agent="human_hint_distill"),
    )


def test_human_hint_roundtrips_through_store(store: SqliteArtifactStore) -> None:
    original = _make_hint()
    ref = store.put(original)
    loaded = store.get(ref)
    assert isinstance(loaded, HumanHint)
    assert loaded.kind is ArtifactKind.HUMAN_HINT
    assert loaded.hint_kind is HumanHintKind.POINT_AT_BUG
    assert loaded.target_stage is Stage.RTL
    assert loaded.summary == original.summary
    assert loaded.suggested_route is ReflectionRouteKind.REGEN_CURRENT_RTL
    assert loaded.references == original.references
    assert loaded.raw_transcript == original.raw_transcript


def test_raw_transcript_excluded_from_content_hash() -> None:
    """Same distilled guidance, different chat wording -> same hash."""
    a = _make_hint(raw_transcript="short note")
    b = _make_hint(raw_transcript="a much longer, differently worded chat log")
    assert a.compute_content_hash() == b.compute_content_hash()


def test_summary_is_part_of_content_hash() -> None:
    a = _make_hint(summary="Guidance A.")
    b = _make_hint(summary="Guidance B.")
    assert a.compute_content_hash() != b.compute_content_hash()
