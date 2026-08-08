"""F6.5 — GDSII emit service: parser + runner over a stub sandbox."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import (
    ArtifactKind,
    LayoutArtifact,
    Provenance,
    Stage,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.gdsii_emit import (
    GDSII_MEDIA_TYPE,
    MAGIC_BIN,
    GDSIIEmitParse,
    GDSIIEmitService,
    MagicGDSIIEmitError,
    build_gds_write_script,
    parse_gds_write_output,
)


def _run(*, rc: int = 0, stdout: str = "", stderr: str = "") -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.1,
    )


# --------------------------------------------------------------------------- #
# build_gds_write_script
# --------------------------------------------------------------------------- #
def test_script_loads_def_and_writes_gds() -> None:
    s = build_gds_write_script(
        def_file="design.def", top_module="counter", gds_file="counter.gds",
    )
    assert "def read design.def" in s
    assert "load counter -dereference" in s
    assert "gds write counter.gds" in s
    assert s.index("def read") < s.index("gds write")


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
def test_clean_run_passes() -> None:
    p = parse_gds_write_output(_run(stdout="Wrote 42 cells\n"))
    assert isinstance(p, GDSIIEmitParse)
    assert p.passed
    assert p.cells_written == 42
    assert p.violations == []


def test_error_line_closes_gate() -> None:
    log = "Error: no cell named 'counter'\n"
    p = parse_gds_write_output(_run(stderr=log, rc=1))
    assert not p.passed
    assert any(v.code == "GDSII.ERROR" for v in p.violations)


def test_nonzero_returncode_alone_fails() -> None:
    p = parse_gds_write_output(_run(stdout="Wrote 0 cells\n", rc=1))
    assert not p.passed


def test_gds_written_line_acknowledged() -> None:
    # An alternative summary shape: "GDS written to counter.gds".
    p = parse_gds_write_output(_run(stdout="GDS written to counter.gds\n"))
    assert p.passed


# --------------------------------------------------------------------------- #
# Runner wiring
# --------------------------------------------------------------------------- #
@dataclass
class StubSandbox:
    tool_run: ToolRun
    side_effect: Callable[[Path], None] | None = None
    calls: list[tuple[list[str], Path, dict[str, bytes]]] = field(default_factory=list)

    def run(
        self, cmd: list[str], mount: Path | str, *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun:
        mp = Path(mount)
        staged = {p.name: p.read_bytes() for p in mp.iterdir() if p.is_file()}
        self.calls.append((list(cmd), mp, staged))
        if self.side_effect is not None:
            self.side_effect(mp)
        return self.tool_run


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


def _layout(
    store: SqliteArtifactStore, *,
    design_id: str = "d0", top: str = "counter",
) -> LayoutArtifact:
    blob = store.put_blob(b"# DEF\nEND DESIGN\n", media_type="text/x-def")
    art = LayoutArtifact(
        artifact_id=f"{design_id}.{top}.layout",
        design_id=design_id, module_id=top,
        def_file=blob, stage_reached="routed",
        die_area_um2=12345.6,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _drop_gds(*, top: str, content: bytes) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        (mount / f"{top}.gds").write_bytes(content)
    return _impl


def test_runner_emits_gdsii_artifact(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Wrote 42 cells\nGDS written to counter.gds\n"),
        side_effect=_drop_gds(top="counter", content=b"\x00GDSII-bytes\x00"),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    layout = _layout(store)

    gds = svc.emit(layout)
    assert gds.kind is ArtifactKind.GDSII
    assert gds.cell_count == 42
    assert gds.die_area_um2 == 12345.6
    assert store.get_blob(gds.gds) == b"\x00GDSII-bytes\x00"
    assert gds.gds.media_type == GDSII_MEDIA_TYPE
    # Provenance links to the layout — the F6.5 AC.
    assert gds.provenance.inputs == [layout.ref()]


def test_runner_command_shape(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_run(stdout="Wrote 1 cells\n"),
        side_effect=_drop_gds(top="counter", content=b"GDS\n"),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    svc.emit(_layout(store))

    cmd, _mount, staged = sandbox.calls[0]
    assert cmd[0] == MAGIC_BIN
    assert "-T" in cmd and "gf180mcuD" in cmd
    assert cmd[-1] == "gds_write.tcl"
    assert "design.def" in staged
    assert "gds_write.tcl" in staged


def test_runner_raises_on_pass_without_gds(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(tool_run=_run(stdout="Wrote 0 cells\n"))  # no side_effect
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    with pytest.raises(MagicGDSIIEmitError):
        svc.emit(_layout(store))


def test_runner_returns_empty_gds_on_failure(store: SqliteArtifactStore) -> None:
    # Magic failed loudly — the service still completes (so provenance for the
    # failed attempt is preserved) and the GDS blob ends up empty.
    sandbox = StubSandbox(
        tool_run=_run(stderr="Error: no cell\n", rc=1),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    gds = svc.emit(_layout(store))
    assert gds.gds.size_bytes == 0
    assert gds.cell_count == 0


# --------------------------------------------------------------------------- #
# F12.4 — GDSII inherits cell_count from the layout when the layout carries
# the LibreLane-harvested value. Otherwise falls back to Magic's stdout.
# --------------------------------------------------------------------------- #
def _layout_with_cell_count(
    store: SqliteArtifactStore, *, cell_count: int,
) -> LayoutArtifact:
    blob = store.put_blob(b"# DEF\nEND DESIGN\n", media_type="text/x-def")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=blob, stage_reached="routed",
        die_area_um2=12345.6,
        cell_count=cell_count,
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_gdsii_prefers_layout_cell_count_over_magic_stdout(
    store: SqliteArtifactStore,
) -> None:
    """F12.4: when the layout carries a LibreLane-harvested cell_count,
    it wins over Magic's "Wrote N cells" stdout count — that's the
    canonical instance count from metrics.json."""
    sandbox = StubSandbox(
        tool_run=_run(stdout="Wrote 1 cells\n"),
        side_effect=_drop_gds(top="counter", content=b"GDS\n"),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    gds = svc.emit(_layout_with_cell_count(store, cell_count=245))
    assert gds.cell_count == 245  # the layout's count wins


def test_gdsii_falls_back_to_magic_stdout_when_layout_has_zero_cells(
    store: SqliteArtifactStore,
) -> None:
    """When the layout carries cell_count=0 (older harvest, missing
    metric), the GDS falls back to Magic's "Wrote N cells" stdout."""
    sandbox = StubSandbox(
        tool_run=_run(stdout="Wrote 17 cells\n"),
        side_effect=_drop_gds(top="counter", content=b"GDS\n"),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    gds = svc.emit(_layout(store))  # _layout has cell_count=0 default
    assert gds.cell_count == 17  # Magic's count fills in
