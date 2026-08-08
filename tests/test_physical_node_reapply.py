"""F21.3-D — PHYSICAL node reapplies the route history at entry.

The PHYSICAL node walks ``state.physical_repair_routes`` and applies
each delta to ``ctx.physical_config`` to produce the config for the
next attempt. Empty history → identity (byte-identical to pre-F21.3).

These tests bypass the full LangGraph spine: they import the private
``_make_physical_node`` closure directly and drive it with a recording
stub ``physical_driver`` so we can assert on the config passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.agents.physical_stage import PhysicalStageOutcome
from chip_agent.design_state import (
    DesignState,
    EscalationLevel,
    LayoutArtifact,
    NetlistArtifact,
    PhysicalRepairRoute,
    PhysicalRepairRouteKind,
    Provenance,
    Stage,
)
from chip_agent.tools.librelane import PhysicalRun
from chip_agent.graph.stage_context import StageContext
from chip_agent.graph.state_graph import _make_physical_node
from chip_agent.obs.tracing import NoopTracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.librelane import PhysicalConfig


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


def _stage_netlist(store: SqliteArtifactStore) -> NetlistArtifact:
    blob = store.put_blob(b"// gate-level\n", media_type="text/x-verilog")
    art = NetlistArtifact(
        artifact_id="d0.counter.netlist",
        design_id="d0", module_id="counter",
        netlist=blob, std_cell_lib="sky130_fd_sc_hd",
        cell_count=10,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


@dataclass
class _RecordingPhysicalDriver:
    """Records the PhysicalConfig the PHYSICAL node passed in. Returns a
    successful outcome so the node doesn't escalate to HUMAN before we
    can inspect what was sent."""
    store: SqliteArtifactStore
    seen_configs: list[PhysicalConfig] = field(default_factory=list)

    def drive(
        self, netlist: NetlistArtifact, *, config: PhysicalConfig,
        time_limit_s: int | None = None,
    ) -> PhysicalStageOutcome:
        self.seen_configs.append(config)
        # Mint a minimal LayoutArtifact so the node thinks we passed.
        def_blob = self.store.put_blob(b"# DEF\n", media_type="text/x-def")
        layout = LayoutArtifact(
            artifact_id=f"{netlist.design_id}.{netlist.module_id}.layout",
            design_id=netlist.design_id, module_id=netlist.module_id,
            def_file=def_blob, stage_reached="routed",
            provenance=Provenance(produced_by=Stage.PHYSICAL),
        )
        self.store.put(layout)
        return PhysicalStageOutcome(
            passed=True,
            escalate_to=None,
            layout=layout,
            layout_ref=layout.ref(),
            final_run=PhysicalRun(
                passed=True, stage_reached="routed",
                congested=False, congestion_pct=0.0,
                utilization_pct=50.0, die_area_um2=1000.0,
                violations=[], metrics={},
            ),
            attempts=1,
            final_config=config,
        )


def _build_ctx(
    store: SqliteArtifactStore,
    driver: _RecordingPhysicalDriver,
    base_config: PhysicalConfig,
) -> StageContext:
    return StageContext(
        store=store,
        tracer=NoopTracer(),
        physical_driver=driver,
        physical_config=base_config,
    )


def _initial_state(netlist: NetlistArtifact) -> DesignState:
    state = DesignState(design_id="d0", name="counter-demo")
    # Promote the netlist as the SYNTH head so load_netlist_head finds it.
    from chip_agent.design_state import StageState
    state.stages[Stage.SYNTH] = StageState(
        stage=Stage.SYNTH, head=netlist.ref(),
    )
    return state


def test_empty_route_history_is_identity(store: SqliteArtifactStore) -> None:
    """No routes → driver receives ctx.physical_config UNCHANGED. Pre-F21.3
    path is byte-identical."""
    netlist = _stage_netlist(store)
    base = PhysicalConfig(
        design_name="counter", top_module="counter", clock_period_ns=10.0,
    )
    driver = _RecordingPhysicalDriver(store=store)
    ctx = _build_ctx(store, driver, base)
    node = _make_physical_node(ctx)

    state = _initial_state(netlist)
    assert state.physical_repair_routes == []
    node(state)

    assert len(driver.seen_configs) == 1
    seen = driver.seen_configs[0]
    assert seen == base  # Frozen dataclass equality — every field matches.
    assert seen.clock_period_ns == 10.0
    assert seen.pl_target_density is None
    assert seen.synth_strategy is None


def test_one_route_in_history_applies_one_delta(store: SqliteArtifactStore) -> None:
    """A LOWER_DENSITY route on the history list → driver receives a
    config with pl_target_density populated (and target_utilization
    untouched)."""
    netlist = _stage_netlist(store)
    base = PhysicalConfig(
        design_name="counter", top_module="counter",
        clock_period_ns=10.0, target_utilization=0.5,
    )
    driver = _RecordingPhysicalDriver(store=store)
    ctx = _build_ctx(store, driver, base)
    node = _make_physical_node(ctx)

    state = _initial_state(netlist)
    state.physical_repair_routes.append(
        PhysicalRepairRoute(
            kind=PhysicalRepairRouteKind.LOWER_DENSITY,
            reason="first attempt: ss WNS negative",
        ),
    )
    node(state)

    seen = driver.seen_configs[0]
    assert seen.pl_target_density == 0.45  # 0.5 - 0.05
    assert seen.target_utilization == 0.5  # unchanged
    assert seen.clock_period_ns == 10.0  # unchanged


def test_two_routes_apply_in_order(store: SqliteArtifactStore) -> None:
    """History [RELAX_CLOCK_PERIOD, LOWER_DENSITY] → driver receives a
    config with BOTH knobs adjusted, in that order. Pins the dispatch
    invariant the F21.3 dispatcher relies on."""
    netlist = _stage_netlist(store)
    base = PhysicalConfig(
        design_name="counter", top_module="counter",
        clock_period_ns=10.0, target_utilization=0.5,
    )
    driver = _RecordingPhysicalDriver(store=store)
    ctx = _build_ctx(store, driver, base)
    node = _make_physical_node(ctx)

    state = _initial_state(netlist)
    state.physical_repair_routes.extend([
        PhysicalRepairRoute(
            kind=PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD,
            reason="attempt 1",
        ),
        PhysicalRepairRoute(
            kind=PhysicalRepairRouteKind.LOWER_DENSITY,
            reason="attempt 2",
        ),
    ])
    node(state)

    seen = driver.seen_configs[0]
    assert seen.clock_period_ns == 11.0  # 10.0 * 1.10
    assert seen.pl_target_density == 0.45  # 0.5 - 0.05


def test_ctx_physical_config_not_mutated(store: SqliteArtifactStore) -> None:
    """Defensive: the reapply loop must NOT mutate ``ctx.physical_config``.
    A second invocation of the node with the same history must produce
    the same delta-applied config — not a doubly-applied one."""
    netlist = _stage_netlist(store)
    base = PhysicalConfig(
        design_name="counter", top_module="counter",
        clock_period_ns=10.0, target_utilization=0.5,
    )
    driver = _RecordingPhysicalDriver(store=store)
    ctx = _build_ctx(store, driver, base)
    node = _make_physical_node(ctx)

    state = _initial_state(netlist)
    state.physical_repair_routes.append(
        PhysicalRepairRoute(kind=PhysicalRepairRouteKind.LOWER_DENSITY),
    )

    # Invoke twice with the same history — both invocations must see
    # pl_target_density=0.45, not 0.40 (which would indicate the ctx
    # config was mutated between calls).
    node(state)
    node(state)

    assert len(driver.seen_configs) == 2
    assert driver.seen_configs[0].pl_target_density == 0.45
    assert driver.seen_configs[1].pl_target_density == 0.45
    # Belt-and-braces: ctx still holds the original config.
    assert ctx.physical_config is base
    assert base.pl_target_density is None
