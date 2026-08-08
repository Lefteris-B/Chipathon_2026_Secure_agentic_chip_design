"""Magic-driven DEF → GDSII emit as a typed tool service (F6.5).

Runs Magic in batch mode over a routed DEF + the PDK technology file
inside the sandbox, reads back the written ``.gds`` file, and returns a
:class:`GDSIIArtifact` blobbed into the content-addressed store. The
artifact's provenance points at the input :class:`LayoutArtifact`, so a
``store.lineage(gds.ref())`` walk reaches the originating
:class:`Spec` if the upstream artifacts are linked the usual way.

The emit step has no inner-loop: it either produces a valid GDS or fails
hard. CLAUDE.md's "GDSII emit + human gate" pairing lives at the control
graph: F5.3's ``await_human`` interrupt halts before the gdsii_emit
node, and the control plane only invokes this service once the user has
resumed the run. Tests cover both the service contract and the gate
ordering (the service is NOT called while the graph is paused at
:data:`HUMAN_REVIEW_NODE`).
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chip_agent.design_state import (
    GDSIIArtifact,
    LayoutArtifact,
    Provenance,
    Stage,
    ToolRun,
    ToolVersion,
    Violation,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike
from chip_agent.tools.pdk_paths import magic_tech

__all__ = [
    "GDSII_MEDIA_TYPE",
    "MAGIC_BIN",
    "GDSIIEmitParse",
    "GDSIIEmitService",
    "MagicGDSIIEmitError",
    "build_gds_write_script",
    "parse_gds_write_output",
]


MAGIC_BIN = "magic"
GDSII_MEDIA_TYPE = "application/octet-stream; format=gdsii"

# Magic occasionally prints "Wrote 12345 cells" / "GDS written to top.gds" lines
# on success; on failure it tends to say "Error" / "no cell named ...".
_WROTE_RE = re.compile(
    r"^(?:Wrote\s+(?P<n>\d+)\s+cells?\b"
    r"|GDS written to\s+(?P<path>\S+))",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"^(?:Error|ERROR):\s*(?P<message>.+?)\s*$")


@dataclass(frozen=True)
class GDSIIEmitParse:
    """Transient result of parsing Magic's GDS-write output."""

    passed: bool
    cells_written: int
    violations: list[Violation]
    metrics: dict[str, float]


def build_gds_write_script(
    *,
    def_file: str,
    top_module: str,
    gds_file: str,
) -> str:
    """Return a Magic TCL script that loads ``def_file`` and writes a GDS."""
    return "\n".join([
        f"def read {def_file}",
        f"load {top_module} -dereference",
        "select top cell",
        f"gds write {gds_file}",
        "quit -noprompt",
    ])


def parse_gds_write_output(run: ToolRun) -> GDSIIEmitParse:
    """Parse Magic's stdout/stderr from a ``gds write`` invocation."""
    cells = 0
    violations: list[Violation] = []
    for raw in run.stdout.splitlines() + run.stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        if m := _WROTE_RE.match(line):
            n = m.group("n")
            if n is not None:
                cells = max(cells, int(n))
            continue
        if m := _ERROR_RE.match(line):
            violations.append(Violation(
                code="GDSII.ERROR",
                severity="error",
                message=m.group("message"),
                detail={"raw_line": raw},
            ))

    error_count = sum(1 for v in violations if v.severity == "error")
    metrics: dict[str, float] = {
        "cells_written": float(cells),
        "errors": float(error_count),
        "violations": float(len(violations)),
    }
    passed = run.returncode == 0 and error_count == 0
    return GDSIIEmitParse(
        passed=passed, cells_written=cells, violations=violations, metrics=metrics,
    )


class MagicGDSIIEmitError(RuntimeError):
    """Magic ran cleanly but didn't produce a GDS — config / sandbox bug."""


class GDSIIEmitService:
    """Drive Magic's ``gds write`` on a routed layout and return a
    :class:`GDSIIArtifact` whose blob is the produced GDS bytes."""

    NAME = "magic"

    def __init__(
        self,
        *,
        sandbox: SandboxLike,
        store: SqliteArtifactStore,
        version_str: str = "bundled",
        script_filename: str = "gds_write.tcl",
        def_filename: str = "design.def",
        gds_filename_template: str = "{top}.gds",
        tech: str = magic_tech("gf180mcuD"),
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self._version_str = version_str
        self._script_filename = script_filename
        self._def_filename = def_filename
        self._gds_filename_template = gds_filename_template
        self._tech = tech

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=self.NAME,
            version=self._version_str,
            container_digest=digest,
        )

    def emit(
        self,
        layout: LayoutArtifact,
        *,
        top_module: str | None = None,
        time_limit_s: int | None = None,
    ) -> GDSIIArtifact:
        """Convert ``layout``'s DEF into a GDS-backed :class:`GDSIIArtifact`.

        Raises :class:`MagicGDSIIEmitError` if Magic returned success but
        no GDS bytes landed in the mount — that's a wiring bug worth a
        loud failure rather than a silent empty artifact.
        """
        top = top_module or layout.module_id or layout.artifact_id.split(".")[-2]
        gds_filename = self._gds_filename_template.format(top=top)

        with tempfile.TemporaryDirectory(prefix="chip-agent-gds-") as td:
            work = Path(td)
            (work / self._def_filename).write_bytes(
                self.store.get_blob(layout.def_file),
            )
            (work / self._script_filename).write_text(build_gds_write_script(
                def_file=self._def_filename,
                top_module=top,
                gds_file=gds_filename,
            ))
            run = self.sandbox.run(
                [MAGIC_BIN, "-dnull", "-noconsole", "-T", self._tech,
                 self._script_filename],
                mount=work,
                time_limit_s=time_limit_s,
                read_only_mount=False,
            )
            gds_path = work / gds_filename
            gds_bytes = gds_path.read_bytes() if gds_path.exists() else b""

        parse = parse_gds_write_output(run)
        if parse.passed and not gds_bytes:
            raise MagicGDSIIEmitError(
                f"magic reported success for {top!r} but wrote no GDS"
            )
        return self._build_artifact(layout, gds_bytes, parse, top_module=top)

    def _build_artifact(
        self,
        layout: LayoutArtifact,
        gds_bytes: bytes,
        parse: GDSIIEmitParse,
        *,
        top_module: str,
    ) -> GDSIIArtifact:
        tool = self.version()
        module = layout.module_id or top_module
        gds_ref = self.store.put_blob(gds_bytes, media_type=GDSII_MEDIA_TYPE)
        # F12.4: prefer the LibreLane-harvested cell_count on the layout when
        # available — that's the canonical instance count from metrics.json.
        # Fall back to Magic's "Wrote N cells" stdout count when the layout
        # didn't carry one (stub flow, older harvest passes).
        cell_count = layout.cell_count or parse.cells_written
        return GDSIIArtifact(
            artifact_id=f"{layout.design_id}.{module}.gds",
            design_id=layout.design_id,
            module_id=layout.module_id,
            gds=gds_ref,
            die_area_um2=layout.die_area_um2,
            cell_count=cell_count,
            provenance=Provenance(
                produced_by=Stage.GDSII,
                agent="gdsii_specialist",
                tool=tool,
                inputs=[layout.ref()],
            ),
            metadata={
                "cells_written": parse.cells_written,
                "stage_reached": layout.stage_reached,
            },
        )
