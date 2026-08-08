"""F6.5 acceptance: GDS emitted only after approval; provenance links
GDS → layout → … → spec.

Two AC strands:

1. **Provenance chain.** Build a complete chain (Spec → Plan → RTL →
   Netlist → Layout → GDSII), each artifact's ``provenance.inputs``
   linking to the upstream ref, then drive
   :class:`GDSIIStageDriver`. ``store.lineage(gds_ref)`` must walk back
   to the originating :class:`Spec`.

2. **Human gate.** A LangGraph state graph with an ``await_human`` node
   + ``interrupt_after`` must NOT invoke the GDSII driver until the
   user resumes. The test asserts the driver's call counter stays at 0
   while the graph is paused, then ticks to 1 on resume.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph

from chip_agent.agents.gdsii_stage import (
    GDSIIStageDriver,
    GDSIIStageError,
    GDSIIStageOutcome,
)
from chip_agent.design_state import (
    ArtifactKind,
    BlobRef,
    DesignConstraints,
    DesignPlan,
    DesignState,
    DesignStatus,
    GDSIIArtifact,
    LayoutArtifact,
    ModuleDecl,
    NetlistArtifact,
    Provenance,
    RTLArtifact,
    Spec,
    Stage,
    ToolRun,
)
from chip_agent.graph.state_graph import (
    HUMAN_REVIEW_NODE,
    open_sqlite_checkpointer,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.gdsii_emit import GDSIIEmitService


# --------------------------------------------------------------------------- #
# Stub sandbox shared by the unit + provenance tests
# --------------------------------------------------------------------------- #
@dataclass
class StubSandbox:
    tool_run: ToolRun
    side_effect: Callable[[Path], None] | None = None
    calls: list[Path] = field(default_factory=list)

    def run(
        self, cmd: list[str], mount: Path | str, *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun:
        mp = Path(mount)
        self.calls.append(mp)
        if self.side_effect is not None:
            self.side_effect(mp)
        return self.tool_run


def _drop_gds(*, top: str, content: bytes) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        (mount / f"{top}.gds").write_bytes(content)
    return _impl


def _ok_run() -> ToolRun:
    return ToolRun(
        returncode=0, stdout="Wrote 17 cells\n",
        stderr="", artifacts_dir="/tmp", duration_s=0.1,
    )


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Smallest path: driver returns a typed outcome
# --------------------------------------------------------------------------- #
def _bare_layout(store: SqliteArtifactStore) -> LayoutArtifact:
    blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    art = LayoutArtifact(
        artifact_id="d0.counter.layout",
        design_id="d0", module_id="counter",
        def_file=blob, stage_reached="routed",
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def test_driver_returns_typed_outcome(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_ok_run(),
        side_effect=_drop_gds(top="counter", content=b"GDS-bytes"),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    driver = GDSIIStageDriver(service=svc, store=store, design_id="d0")

    outcome = driver.drive(_bare_layout(store))
    assert isinstance(outcome, GDSIIStageOutcome)
    assert outcome.gds.kind is ArtifactKind.GDSII
    assert outcome.gds_ref.artifact_id == "d0.counter.gds"


def test_driver_rejects_design_id_mismatch(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_ok_run(),
        side_effect=_drop_gds(top="counter", content=b"GDS-bytes"),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    driver = GDSIIStageDriver(service=svc, store=store, design_id="other")
    with pytest.raises(GDSIIStageError):
        driver.drive(_bare_layout(store))


def test_driver_rejects_empty_design_id(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(tool_run=_ok_run())
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    with pytest.raises(GDSIIStageError):
        GDSIIStageDriver(service=svc, store=store, design_id="")


# --------------------------------------------------------------------------- #
# AC: provenance walks GDS → layout → netlist → RTL → plan → spec
# --------------------------------------------------------------------------- #
def _chain(
    store: SqliteArtifactStore,
) -> tuple[Spec, DesignPlan, RTLArtifact, NetlistArtifact, LayoutArtifact]:
    """Stage a full upstream chain with provenance links wired explicitly."""
    spec = Spec(
        artifact_id="d0.spec", design_id="d0",
        raw_text="a 4-bit counter",
        normalized="counter",
        constraints=DesignConstraints(pdk="sky130A"),
        provenance=Provenance(produced_by=Stage.SPEC),
    )
    store.put(spec)
    spec = store.get_by_id(spec.artifact_id)  # type: ignore[assignment]

    plan = DesignPlan(
        artifact_id="d0.plan", design_id="d0",
        top_module_id="counter",
        modules=[ModuleDecl(module_id="counter", name="counter",
                            description="4-bit counter")],
        provenance=Provenance(produced_by=Stage.PLAN, inputs=[spec.ref()]),
    )
    store.put(plan)
    plan = store.get_by_id(plan.artifact_id)  # type: ignore[assignment]

    rtl_blob: BlobRef = store.put_blob(
        b"module counter; endmodule\n", media_type="text/x-verilog",
    )
    rtl = RTLArtifact(
        artifact_id="d0.counter.rtl", design_id="d0", module_id="counter",
        top_module="counter", source=rtl_blob,
        provenance=Provenance(produced_by=Stage.RTL, inputs=[plan.ref()]),
    )
    store.put(rtl)
    rtl = store.get_by_id(rtl.artifact_id)  # type: ignore[assignment]

    nl_blob = store.put_blob(b"// gate-level\n", media_type="text/x-verilog")
    netlist = NetlistArtifact(
        artifact_id="d0.counter.netlist", design_id="d0", module_id="counter",
        netlist=nl_blob, std_cell_lib="sky130_fd_sc_hd", cell_count=42,
        provenance=Provenance(produced_by=Stage.SYNTH, inputs=[rtl.ref()]),
    )
    store.put(netlist)
    netlist = store.get_by_id(netlist.artifact_id)  # type: ignore[assignment]

    def_blob = store.put_blob(b"# DEF\n", media_type="text/x-def")
    layout = LayoutArtifact(
        artifact_id="d0.counter.layout", design_id="d0", module_id="counter",
        def_file=def_blob, stage_reached="routed", die_area_um2=12345.6,
        provenance=Provenance(produced_by=Stage.PHYSICAL, inputs=[netlist.ref()]),
    )
    store.put(layout)
    layout = store.get_by_id(layout.artifact_id)  # type: ignore[assignment]

    return spec, plan, rtl, netlist, layout


def test_provenance_walks_gds_to_spec(store: SqliteArtifactStore) -> None:
    # The F6.5 AC: a full provenance chain reaches Spec.
    _spec, _plan, _rtl, _netlist, layout = _chain(store)
    sandbox = StubSandbox(
        tool_run=_ok_run(),
        side_effect=_drop_gds(top="counter", content=b"GDS-bytes"),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    driver = GDSIIStageDriver(service=svc, store=store, design_id="d0")

    outcome = driver.drive(layout)
    assert isinstance(outcome.gds, GDSIIArtifact)
    # Direct edge: GDS -> layout.
    assert outcome.gds.provenance.inputs == [layout.ref()]

    # Walk transitively — Spec must show up in lineage.
    lineage = store.lineage(outcome.gds_ref)
    ids = {a.artifact_id for a in lineage}
    assert "d0.spec" in ids
    assert "d0.plan" in ids
    assert "d0.counter.rtl" in ids
    assert "d0.counter.netlist" in ids
    assert "d0.counter.layout" in ids
    assert "d0.counter.gds" in ids
    # Spec is the root: it appears before plan in post-order.
    spec_idx = next(i for i, a in enumerate(lineage) if a.artifact_id == "d0.spec")
    plan_idx = next(i for i, a in enumerate(lineage) if a.artifact_id == "d0.plan")
    assert spec_idx < plan_idx


# --------------------------------------------------------------------------- #
# AC: GDS emitted only after approval — F5.3's await_human ordering applies
# --------------------------------------------------------------------------- #
def test_graph_does_not_emit_gds_until_human_resume(
    store: SqliteArtifactStore, tmp_path: Path,
) -> None:
    """Build a state graph whose gdsii_emit node calls our driver. The
    ``interrupt_after=[await_human]`` setup must hold the driver back
    until the user resumes."""
    _spec, _plan, _rtl, _netlist, layout = _chain(store)
    sandbox = StubSandbox(
        tool_run=_ok_run(),
        side_effect=_drop_gds(top="counter", content=b"GDS-bytes"),
    )
    svc = GDSIIEmitService(sandbox=sandbox, store=store)
    driver = GDSIIStageDriver(service=svc, store=store, design_id="d0")

    driver_calls: list[LayoutArtifact] = []

    def _await(state: DesignState) -> dict[str, Any]:
        return {"status": DesignStatus.AWAITING_HUMAN}

    def _emit(state: DesignState) -> dict[str, Any]:
        driver_calls.append(layout)
        outcome = driver.drive(layout)
        return {
            "current_stage": Stage.GDSII,
            "status": DesignStatus.COMPLETED,
            "stages": {**state.stages},  # passthrough — no real promotion here
            "_gds_ref": outcome.gds_ref.artifact_id,
        }

    graph: StateGraph[DesignState, Any, DesignState, DesignState] = StateGraph(
        DesignState,
    )
    graph.add_node(HUMAN_REVIEW_NODE, _await)
    graph.add_node("gdsii_emit", _emit)
    graph.add_edge(START, HUMAN_REVIEW_NODE)
    graph.add_edge(HUMAN_REVIEW_NODE, "gdsii_emit")
    graph.add_edge("gdsii_emit", END)

    config = {"configurable": {"thread_id": "f6.5-gate"}}
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        compiled = graph.compile(
            checkpointer=saver, interrupt_after=[HUMAN_REVIEW_NODE],
        )
        # First invoke: must halt at await_human BEFORE the gdsii driver fires.
        halted = compiled.invoke(DesignState(design_id="d0", name="counter"),
                                 config)
        if isinstance(halted, dict):
            halted = DesignState.model_validate(halted)
        assert halted.status is DesignStatus.AWAITING_HUMAN
        assert driver_calls == []  # driver has NOT run yet

        # Resume — gdsii_emit runs, driver ticks.
        final = compiled.invoke(None, config)
        if isinstance(final, dict):
            final = DesignState.model_validate(final)
        assert final.status is DesignStatus.COMPLETED
        assert len(driver_calls) == 1
