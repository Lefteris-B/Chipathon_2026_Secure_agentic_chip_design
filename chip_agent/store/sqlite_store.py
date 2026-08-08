"""Append-only, content-addressed artifact store (F1.2 + F1.3).

Index lives in SQLite (``Settings.paths.store_db``); artifact JSON and blob
bytes live under ``Settings.paths.runs_dir`` keyed by their content hash:

    <runs_dir>/<hex[:2]>/<hex>.json        # one Artifact subclass, serialised
    <runs_dir>/<hex[:2]>/<hex>             # opaque blob (RTL, netlist, VCD, ...)

Identical content dedupes — the JSON file is written once per content hash,
and ``put`` is idempotent for the same ``(artifact_id, content_hash)``. New
``(artifact_id, version)`` rows are minted only when an artifact's content
hash changes for that logical id. Status updates land in SQLite and overlay
the immutable on-disk JSON when artifacts are read back, so lifecycle moves
(DRAFT -> ACCEPTED -> SUPERSEDED) never invalidate the content hash.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from chip_agent.design_state import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ArtifactStatus,
    AssertionSpec,
    BlobRef,
    ContractArtifact,
    DesignPlan,
    DRCReport,
    FailureDiagnosis,
    GDSIIArtifact,
    HumanHint,
    LayoutArtifact,
    LintResult,
    LVSReport,
    MultiCornerSTAReport,
    NetlistArtifact,
    OracleArtifact,
    OracleVerificationArtifact,
    RepairAttempt,
    RTLArtifact,
    SecurityReport,
    SimulationResult,
    Spec,
    SynthesisReport,
    TestbenchArtifact,
    TimingReport,
)

__all__ = ["SqliteArtifactStore", "StoreError"]


class StoreError(RuntimeError):
    """Store-level failure: missing artifact, corrupt content, etc."""


# Map ArtifactKind -> concrete subclass for typed deserialisation.
_KIND_TO_CLASS: dict[ArtifactKind, type[Artifact]] = {
    ArtifactKind.SPEC: Spec,
    ArtifactKind.PLAN: DesignPlan,
    ArtifactKind.CONTRACT: ContractArtifact,
    ArtifactKind.ORACLE: OracleArtifact,
    ArtifactKind.ASSERTIONS: AssertionSpec,
    ArtifactKind.RTL: RTLArtifact,
    ArtifactKind.TESTBENCH: TestbenchArtifact,
    ArtifactKind.LINT: LintResult,
    ArtifactKind.SIM: SimulationResult,
    ArtifactKind.NETLIST: NetlistArtifact,
    ArtifactKind.SYNTH_REPORT: SynthesisReport,
    ArtifactKind.LAYOUT: LayoutArtifact,
    ArtifactKind.STA: TimingReport,
    ArtifactKind.MULTI_CORNER_STA: MultiCornerSTAReport,  # F21.2
    ArtifactKind.DRC: DRCReport,
    ArtifactKind.LVS: LVSReport,
    ArtifactKind.SECURITY: SecurityReport,
    ArtifactKind.DIAGNOSIS: FailureDiagnosis,
    ArtifactKind.ORACLE_VERIFICATION: OracleVerificationArtifact,  # F19.6
    ArtifactKind.REPAIR_ATTEMPT: RepairAttempt,  # F20.1
    ArtifactKind.HUMAN_HINT: HumanHint,  # F23.1
    ArtifactKind.GDSII: GDSIIArtifact,
}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id   TEXT    NOT NULL,
    version       INTEGER NOT NULL,
    content_hash  TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    design_id     TEXT    NOT NULL,
    module_id     TEXT,
    status        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (artifact_id, version)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_content_hash ON artifacts(content_hash);
CREATE INDEX IF NOT EXISTS idx_artifacts_design_kind  ON artifacts(design_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_id_hash ON artifacts(artifact_id, content_hash);
"""


def _hex_of(content_hash: str) -> str:
    """Strip the ``sha256:`` prefix and return the bare hex digest."""
    if not content_hash.startswith("sha256:"):
        raise StoreError(f"unexpected content_hash format: {content_hash!r}")
    return content_hash.removeprefix("sha256:")


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (tmpfile + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        # Cleanup the temp file on any failure before propagating.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


class SqliteArtifactStore:
    """Concrete :class:`chip_agent.design_state.ArtifactStore`.

    Single-process; one SQLite connection per instance. Thread-safe for
    serialised callers (the store itself does not fan out work).
    """

    def __init__(self, *, db_path: Path | str, content_dir: Path | str) -> None:
        self.db_path = Path(db_path)
        self.content_dir = Path(content_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteArtifactStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ----------------------------------------------------------------- paths
    def _artifact_path(self, content_hash: str) -> Path:
        hex_ = _hex_of(content_hash)
        return self.content_dir / hex_[:2] / f"{hex_}.json"

    def _blob_path(self, sha256_hex: str) -> Path:
        return self.content_dir / sha256_hex[:2] / sha256_hex

    # ----------------------------------------------------------------- put
    def put(self, artifact: Artifact) -> ArtifactRef:
        """Persist ``artifact``; return a ref. Idempotent for equal content.

        * Same ``artifact_id`` + same content_hash -> returns the existing ref.
        * Same ``artifact_id`` + new content_hash  -> new version row.
        * New ``artifact_id`` (same content)       -> new row, JSON dedupes
                                                      on disk.
        """
        content_hash = artifact.compute_content_hash()
        artifact.content_hash = content_hash

        with self._tx() as cur:
            existing = cur.execute(
                "SELECT version FROM artifacts "
                "WHERE artifact_id = ? AND content_hash = ?",
                (artifact.artifact_id, content_hash),
            ).fetchone()
            if existing is not None:
                artifact.version = existing["version"]
                # Body should already be on disk; write once if somehow missing.
                self._write_artifact_body(artifact)
                return artifact.ref()

            max_row = cur.execute(
                "SELECT COALESCE(MAX(version), 0) AS m FROM artifacts "
                "WHERE artifact_id = ?",
                (artifact.artifact_id,),
            ).fetchone()
            artifact.version = int(max_row["m"]) + 1

            self._write_artifact_body(artifact)
            cur.execute(
                "INSERT INTO artifacts "
                "(artifact_id, version, content_hash, kind, design_id, "
                " module_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.version,
                    content_hash,
                    artifact.kind.value,
                    artifact.design_id,
                    artifact.module_id,
                    artifact.status.value,
                    artifact.created_at.isoformat(),
                ),
            )
        return artifact.ref()

    def _write_artifact_body(self, artifact: Artifact) -> None:
        """Write the JSON body once per content_hash; subsequent puts no-op."""
        path = self._artifact_path(artifact.content_hash)
        if path.exists():
            return
        payload = artifact.model_dump(mode="json")
        _atomic_write(path, json.dumps(payload, sort_keys=True).encode())

    # ----------------------------------------------------------------- get
    def get(self, ref: ArtifactRef) -> Artifact:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND version = ?",
            (ref.artifact_id, ref.version),
        ).fetchone()
        if row is None:
            raise StoreError(
                f"no artifact found for ref ({ref.artifact_id} v{ref.version})"
            )
        return self._load_artifact_row(row)

    def get_by_id(
        self, artifact_id: str, version: int | None = None
    ) -> Artifact:
        if version is None:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (artifact_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
        if row is None:
            raise StoreError(
                f"no artifact found for id {artifact_id!r}"
                + (f" v{version}" if version is not None else "")
            )
        return self._load_artifact_row(row)

    def history(self, artifact_id: str) -> list[Artifact]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? ORDER BY version ASC",
            (artifact_id,),
        ).fetchall()
        return [self._load_artifact_row(r) for r in rows]

    # ----------------------------------------------------------------- lineage (F1.3)
    def lineage(self, ref: ArtifactRef) -> list[Artifact]:
        """Walk ``provenance.inputs`` transitively; return DAG oldest -> newest.

        Cycle-safe by content-hash bookkeeping (artifacts are append-only
        and content-addressed so a true cycle can't exist, but defensive
        checks keep a corrupt store from looping forever).
        """
        visited: dict[str, Artifact] = {}
        order: list[str] = []

        def _walk(r: ArtifactRef) -> None:
            key = f"{r.artifact_id}@v{r.version}"
            if key in visited:
                return
            art = self.get(r)
            visited[key] = art
            for inp in art.provenance.inputs:
                _walk(inp)
            order.append(key)  # post-order = inputs before consumer

        _walk(ref)
        return [visited[k] for k in order]

    # ----------------------------------------------------------------- blobs
    def put_blob(
        self, data: bytes, *, media_type: str = "application/octet-stream"
    ) -> BlobRef:
        """Content-address ``data`` into the store; return a :class:`BlobRef`."""
        sha = hashlib.sha256(data).hexdigest()
        path = self._blob_path(sha)
        if not path.exists():
            _atomic_write(path, data)
        return BlobRef(
            path=f"{sha[:2]}/{sha}",
            sha256=sha,
            size_bytes=len(data),
            media_type=media_type,
        )

    def get_blob(self, ref: BlobRef) -> bytes:
        path = self.content_dir / ref.path
        if not path.exists():
            raise StoreError(f"blob missing on disk: {ref.path}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise StoreError(f"blob {ref.path} sha256 mismatch")
        return data

    # ----------------------------------------------------------------- status
    def set_status(self, ref: ArtifactRef, status: ArtifactStatus) -> None:
        """Update lifecycle status in the index; on-disk JSON is unchanged."""
        n = self._conn.execute(
            "UPDATE artifacts SET status = ? "
            "WHERE artifact_id = ? AND version = ?",
            (status.value, ref.artifact_id, ref.version),
        ).rowcount
        if n == 0:
            raise StoreError(
                f"set_status: no row for {ref.artifact_id} v{ref.version}"
            )

    # ----------------------------------------------------------------- internals
    def _load_artifact_row(self, row: sqlite3.Row) -> Artifact:
        kind = ArtifactKind(row["kind"])
        cls = _KIND_TO_CLASS.get(kind)
        if cls is None:
            raise StoreError(f"unknown artifact kind in row: {row['kind']!r}")
        path = self._artifact_path(row["content_hash"])
        if not path.exists():
            raise StoreError(f"artifact body missing on disk: {path}")
        data = json.loads(path.read_bytes())
        # SQLite carries the authoritative lifecycle status; overlay it.
        data["status"] = row["status"]
        # Ensure (id, version, hash) reflect the row (not whatever was on disk).
        data["artifact_id"] = row["artifact_id"]
        data["version"] = row["version"]
        data["content_hash"] = row["content_hash"]
        return cls.model_validate(data)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        """BEGIN / COMMIT a transaction; rollback on error."""
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            yield cur
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise

    # ----------------------------------------------------------------- helpers
    def all_refs(self, design_id: str | None = None) -> Iterable[ArtifactRef]:
        """Diagnostic helper: yield every ref in the store."""
        if design_id is None:
            rows = self._conn.execute(
                "SELECT artifact_id, version, kind, content_hash FROM artifacts"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT artifact_id, version, kind, content_hash FROM artifacts "
                "WHERE design_id = ?",
                (design_id,),
            ).fetchall()
        for r in rows:
            yield ArtifactRef(
                artifact_id=r["artifact_id"],
                version=r["version"],
                kind=ArtifactKind(r["kind"]),
                content_hash=r["content_hash"],
            )

    # ``list`` shadows the builtin name inside the class scope; defined last so
    # the ``list[Artifact]`` annotations earlier in the class still resolve to
    # the builtin during type-checking.
    def list(
        self,
        design_id: str,
        kind: ArtifactKind | None = None,
    ) -> list[ArtifactRef]:
        if kind is None:
            rows = self._conn.execute(
                "SELECT artifact_id, version, kind, content_hash FROM artifacts "
                "WHERE design_id = ? ORDER BY artifact_id, version",
                (design_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT artifact_id, version, kind, content_hash FROM artifacts "
                "WHERE design_id = ? AND kind = ? ORDER BY artifact_id, version",
                (design_id, kind.value),
            ).fetchall()
        return [
            ArtifactRef(
                artifact_id=r["artifact_id"],
                version=r["version"],
                kind=ArtifactKind(r["kind"]),
                content_hash=r["content_hash"],
            )
            for r in rows
        ]
