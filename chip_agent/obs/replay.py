"""Reproducibility-replay primitives (F7.3).

CLAUDE.md's invariant: "Reproducibility is mandatory: record the pinned
IIC-OSIC-TOOLS image digest, model + seed + temperature, and config hash in
every artifact's provenance. A run must be replayable from its log."

This module captures the "log" half of that — a typed :class:`RunManifest`
that pins every input the producers need to reproduce the run, plus the
content-hash of every artifact the original run emitted. The producers
themselves are tool services + agents that already live in the codebase;
F7.3 doesn't re-execute them here. It ships the comparison + manifest
surface so a replayed run can be checked bit-for-bit against the
original:

* :func:`manifest_from_run` walks the DAG rooted at ``ref`` and emits a
  :class:`RunManifest` (tool digests, model invocations, seeds, config
  hashes, expected ``(artifact_id, content_hash)`` pairs).
* :func:`compare_to_manifest` reads each manifest entry from a store
  (the replay's store, or the same store after re-staging), recomputes
  the content hash, and emits a :class:`ReplayDiff` with
  matched / mismatched / missing buckets.

The F7.3 AC ("identical content hashes, modulo documented
nondeterministic tools") rides on the standing
:class:`~chip_agent.design_state.Artifact` invariant — content_hash
excludes lifecycle + provenance + ids, so identical content dedupes
across stores. :data:`NONDETERMINISTIC_KINDS` lists the kinds we
deliberately exempt (model-produced free text doesn't bit-reproduce);
those entries land in :attr:`ReplayDiff.skipped`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from chip_agent.design_state import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ModelInvocation,
    ToolVersion,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore, StoreError

__all__ = [
    "NONDETERMINISTIC_KINDS",
    "ManifestEntry",
    "ManifestModelInvocation",
    "ManifestToolPin",
    "ReplayDiff",
    "ReplayMismatch",
    "RunManifest",
    "compare_to_manifest",
    "manifest_from_run",
]


# Artifact kinds we deliberately exclude from bit-for-bit comparison.
# - DIAGNOSIS: carries model-generated free-text (``nl_summary``,
#   ``suspected_cause``) that depends on sampling.
NONDETERMINISTIC_KINDS: frozenset[ArtifactKind] = frozenset({
    ArtifactKind.DIAGNOSIS,
})


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
class ManifestToolPin(BaseModel):
    """One tool's reproducibility pin — name + version + image digest."""

    name: str
    version: str
    container_digest: str | None = None

    @classmethod
    def from_tool_version(cls, tv: ToolVersion) -> ManifestToolPin:
        return cls(
            name=tv.name, version=tv.version, container_digest=tv.container_digest,
        )


class ManifestModelInvocation(BaseModel):
    """Pinned model call: provider + model + seed + temperature.

    Cost / token counts ride along for the replay-cost diff but are NOT
    used in the equality check.
    """

    provider: str
    model: str
    temperature: float = 0.0
    top_p: float | None = None
    seed: int | None = None

    @classmethod
    def from_invocation(cls, mi: ModelInvocation) -> ManifestModelInvocation:
        return cls(
            provider=mi.provider, model=mi.model, temperature=mi.temperature,
            top_p=mi.top_p, seed=mi.seed,
        )


class ManifestEntry(BaseModel):
    """One artifact's reproducibility record.

    ``content_hash`` is the original hash the run produced; on replay,
    the same artifact (with the same id + version) must hash to the
    same value.
    """

    artifact_id: str
    version: int
    kind: ArtifactKind
    content_hash: str
    config_hash: str | None = None
    seed: int | None = None
    tool: ManifestToolPin | None = None
    model: ManifestModelInvocation | None = None

    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self.artifact_id, version=self.version,
            kind=self.kind, content_hash=self.content_hash,
        )


class RunManifest(BaseModel):
    """Pinned record of one run — everything a replay needs to verify."""

    model_config = ConfigDict(frozen=False)

    design_id: str
    root_ref: ArtifactRef
    entries: list[ManifestEntry] = Field(default_factory=list)

    def by_id(self) -> dict[str, ManifestEntry]:
        """Index entries by ``(artifact_id, version)``-keyed map."""
        return {f"{e.artifact_id}@v{e.version}": e for e in self.entries}


# --------------------------------------------------------------------------- #
# Diff types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReplayMismatch:
    """One artifact whose replayed content_hash diverged from the manifest."""

    artifact_id: str
    version: int
    expected: str
    actual: str
    reason: str = "content_hash_mismatch"


@dataclass(frozen=True)
class ReplayDiff:
    """Result of :func:`compare_to_manifest`."""

    matched: list[ManifestEntry]
    mismatched: list[ReplayMismatch]
    missing: list[ManifestEntry]
    skipped: list[ManifestEntry]

    @property
    def reproducible(self) -> bool:
        """True iff every comparable entry matched and none were missing."""
        return not self.mismatched and not self.missing


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def manifest_from_run(
    store: SqliteArtifactStore,
    *,
    design_id: str,
    root_ref: ArtifactRef,
) -> RunManifest:
    """Walk the DAG rooted at ``root_ref`` and snapshot a :class:`RunManifest`.

    The lineage walk is the same one the audit log uses — every artifact
    transitively reachable through ``provenance.inputs`` is recorded with
    its pinned tool / model / seed and its content hash.
    """
    if not design_id:
        raise ValueError("design_id must be non-empty")
    lineage = store.lineage(root_ref)
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for art in lineage:
        key = f"{art.artifact_id}@v{art.version}"
        if key in seen:
            continue
        seen.add(key)
        entries.append(_entry_from_artifact(art))
    return RunManifest(
        design_id=design_id,
        root_ref=root_ref,
        entries=entries,
    )


def compare_to_manifest(
    store: SqliteArtifactStore,
    manifest: RunManifest,
    *,
    nondeterministic_kinds: Iterable[ArtifactKind] = NONDETERMINISTIC_KINDS,
) -> ReplayDiff:
    """Re-read each manifest entry from ``store`` and compare content hashes.

    Three buckets:

    * **matched** — the recomputed content hash equals the manifest's.
    * **mismatched** — the artifact exists but its content hash diverged.
      Either the artifact was edited after the manifest was taken, or a
      replay produced different bytes for the "same" logical artifact.
    * **missing** — the artifact wasn't found in ``store``. A replay that
      can't even stage one of the originals fails the reproducibility
      contract.
    * **skipped** — artifacts whose ``kind`` is in
      :data:`NONDETERMINISTIC_KINDS` (model-produced free text). Exempt
      from the equality check; counted only.
    """
    nondet = set(nondeterministic_kinds)
    matched: list[ManifestEntry] = []
    mismatched: list[ReplayMismatch] = []
    missing: list[ManifestEntry] = []
    skipped: list[ManifestEntry] = []

    for entry in manifest.entries:
        if entry.kind in nondet:
            skipped.append(entry)
            continue
        try:
            art = store.get_by_id(entry.artifact_id, version=entry.version)
        except StoreError:
            missing.append(entry)
            continue
        recomputed = art.compute_content_hash()
        if recomputed == entry.content_hash:
            matched.append(entry)
        else:
            mismatched.append(ReplayMismatch(
                artifact_id=entry.artifact_id,
                version=entry.version,
                expected=entry.content_hash,
                actual=recomputed,
            ))

    return ReplayDiff(
        matched=matched, mismatched=mismatched,
        missing=missing, skipped=skipped,
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _entry_from_artifact(art: Artifact) -> ManifestEntry:
    tool = art.provenance.tool
    model = art.provenance.model
    return ManifestEntry(
        artifact_id=art.artifact_id,
        version=art.version,
        kind=art.kind,
        content_hash=art.content_hash,
        config_hash=art.provenance.config_hash,
        seed=art.provenance.seed,
        tool=ManifestToolPin.from_tool_version(tool) if tool is not None else None,
        model=(
            ManifestModelInvocation.from_invocation(model)
            if model is not None else None
        ),
    )
