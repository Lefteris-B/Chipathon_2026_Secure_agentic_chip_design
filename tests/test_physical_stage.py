"""F6.2 acceptance: :class:`PhysicalStageDriver` retunes ``target_utilization``
on a forced-overcongested config.

The flow's hard signal is the ``PHYSICAL.CONGESTION`` violation on
:class:`PhysicalRun`. The driver runs in a bounded inner loop:

* on a congested failure, lower utilization by ``utilization_delta`` and
  retry within ``max_inner_attempts``;
* on a non-congestion failure, escalate immediately (no retune meaningful);
* on budget exhaustion or a no-op retune at the floor, escalate to ``OUTER``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.agents.physical_stage import (
    PhysicalStageDriver,
    PhysicalStageError,
    PhysicalStageOutcome,
)
from chip_agent.design_state import (
    EscalationLevel,
    LayoutArtifact,
    NetlistArtifact,
    Provenance,
    Stage,
    Violation,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.librelane import (
    LibreLanePhysicalService,
    PhysicalConfig,
    PhysicalRun,
)


# --------------------------------------------------------------------------- #
# Stub LibreLane service (the driver's seam)
# --------------------------------------------------------------------------- #
@dataclass
class StubLibreLaneService:
    """Stand-in for :class:`LibreLanePhysicalService` that returns canned
    :class:`PhysicalRun` results and records the configs it was called with."""

    runs: list[PhysicalRun]
    store: SqliteArtifactStore
    calls: list[PhysicalConfig] = field(default_factory=list)
    _idx: int = 0

    def run_flow(
        self, netlist: NetlistArtifact, *,
        config: PhysicalConfig, time_limit_s: int | None = None,
    ) -> tuple[LayoutArtifact, PhysicalRun]:
        self.calls.append(config)
        run = self.runs[self._idx]
        self._idx = min(self._idx + 1, len(self.runs) - 1)

        def_ref = self.store.put_blob(
            run.def_bytes, media_type="text/x-def",
        )
        layout = LayoutArtifact(
            artifact_id=f"{netlist.design_id}.{config.top_module}.layout",
            design_id=netlist.design_id,
            module_id=netlist.module_id,
            def_file=def_ref,
            stage_reached=run.stage_reached,
            die_area_um2=run.die_area_um2,
            utilization_pct=run.utilization_pct,
            provenance=Provenance(
                produced_by=Stage.PHYSICAL,
                inputs=[netlist.ref()],
                config={"target_utilization": config.target_utilization},
            ),
            metadata={"congestion_pct": run.congestion_pct},
        )
        return layout, run


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


def _netlist(store: SqliteArtifactStore, *, design_id: str = "d0") -> NetlistArtifact:
    blob = store.put_blob(b"// gate-level\n", media_type="text/x-verilog")
    art = NetlistArtifact(
        artifact_id=f"{design_id}.counter.netlist",
        design_id=design_id, module_id="counter",
        netlist=blob,
        std_cell_lib="sky130_fd_sc_hd",
        cell_count=42,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _pass_run() -> PhysicalRun:
    return PhysicalRun(
        passed=True, stage_reached="routed", congested=False,
        congestion_pct=1.0, utilization_pct=50.0,
        die_area_um2=12345.6,
        violations=[],
        metrics={"errors": 0.0},
        def_bytes=b"# DEF\nEND DESIGN\n",
    )


def _congested_run() -> PhysicalRun:
    return PhysicalRun(
        passed=False, stage_reached="routed", congested=True,
        congestion_pct=18.0, utilization_pct=80.0,
        die_area_um2=9876.0,
        violations=[Violation(
            code="PHYSICAL.CONGESTION", severity="error",
            message="route overflow above hard threshold",
        )],
        metrics={"errors": 1.0},
        def_bytes=b"# DEF\n",
    )


def _drc_failed_run() -> PhysicalRun:
    return PhysicalRun(
        passed=False, stage_reached="routed", congested=False,
        congestion_pct=1.0, utilization_pct=50.0,
        die_area_um2=9876.0,
        violations=[Violation(
            code="PHYSICAL.DRC", severity="error", message="3 DRC errors",
        )],
        metrics={"errors": 1.0},
        def_bytes=b"# DEF\n",
    )


# --------------------------------------------------------------------------- #
# Happy path: first attempt passes
# --------------------------------------------------------------------------- #
def test_first_attempt_passes_returns_clean_outcome(store: SqliteArtifactStore) -> None:
    svc = StubLibreLaneService(runs=[_pass_run()], store=store)
    driver = PhysicalStageDriver(
        service=svc,  # type: ignore[arg-type]
        store=store, design_id="d0",
    )
    cfg = PhysicalConfig(design_name="counter", top_module="counter",
                         target_utilization=0.55)
    outcome = driver.drive(_netlist(store), config=cfg)

    assert isinstance(outcome, PhysicalStageOutcome)
    assert outcome.passed
    assert outcome.escalate_to is None
    assert outcome.attempts == 1
    assert outcome.layout is not None and outcome.layout.stage_reached == "routed"
    assert svc.calls == [cfg]


# --------------------------------------------------------------------------- #
# AC: forced over-congested config triggers a retune attempt
# --------------------------------------------------------------------------- #
def test_congestion_triggers_retune_then_pass(store: SqliteArtifactStore) -> None:
    svc = StubLibreLaneService(runs=[_congested_run(), _pass_run()], store=store)
    driver = PhysicalStageDriver(
        service=svc,  # type: ignore[arg-type]
        store=store, design_id="d0",
        max_inner_attempts=3, utilization_delta=0.05,
    )
    cfg = PhysicalConfig(design_name="counter", top_module="counter",
                         target_utilization=0.80)

    outcome = driver.drive(_netlist(store), config=cfg)

    assert outcome.passed
    assert outcome.attempts == 2
    # The AC's retune attempt: utilization dropped between attempt 1 and 2.
    assert svc.calls[0].target_utilization == 0.80
    assert svc.calls[1].target_utilization == 0.75
    # Both runs are visible in the outcome history.
    assert len(outcome.runs) == 2
    assert outcome.runs[0].congested
    assert outcome.runs[1].passed
    assert outcome.final_config is not None
    assert outcome.final_config.target_utilization == 0.75


def test_repeated_congestion_exhausts_to_outer(store: SqliteArtifactStore) -> None:
    svc = StubLibreLaneService(
        runs=[_congested_run(), _congested_run(), _congested_run()],
        store=store,
    )
    driver = PhysicalStageDriver(
        service=svc,  # type: ignore[arg-type]
        store=store, design_id="d0",
        max_inner_attempts=3,
    )
    cfg = PhysicalConfig(design_name="counter", top_module="counter",
                         target_utilization=0.55)
    outcome = driver.drive(_netlist(store), config=cfg)

    assert not outcome.passed
    assert outcome.escalate_to is EscalationLevel.OUTER
    assert outcome.attempts == 3
    # The driver lowered utilization on each retune attempt.
    utils = [c.target_utilization for c in svc.calls]
    assert utils == [0.55, 0.50, 0.45]


def test_retune_floor_stops_loop_early(store: SqliteArtifactStore) -> None:
    # When the retune would be a no-op (utilization already at the floor),
    # the driver escalates instead of spending more budget.
    svc = StubLibreLaneService(
        runs=[_congested_run(), _congested_run()], store=store,
    )
    driver = PhysicalStageDriver(
        service=svc,  # type: ignore[arg-type]
        store=store, design_id="d0",
        max_inner_attempts=4, utilization_delta=0.05,
        floor_utilization=0.30,
    )
    cfg = PhysicalConfig(design_name="counter", top_module="counter",
                         target_utilization=0.30)
    outcome = driver.drive(_netlist(store), config=cfg)

    assert not outcome.passed
    assert outcome.escalate_to is EscalationLevel.OUTER
    # Only one attempt before the floor blocks any retune.
    assert outcome.attempts == 1
    assert svc.calls[0].target_utilization == 0.30


# --------------------------------------------------------------------------- #
# Non-congestion failures escalate without retune
# --------------------------------------------------------------------------- #
def test_non_congestion_failure_escalates_immediately(store: SqliteArtifactStore) -> None:
    svc = StubLibreLaneService(runs=[_drc_failed_run()], store=store)
    driver = PhysicalStageDriver(
        service=svc,  # type: ignore[arg-type]
        store=store, design_id="d0", max_inner_attempts=3,
    )
    cfg = PhysicalConfig(design_name="counter", top_module="counter",
                         target_utilization=0.55)
    outcome = driver.drive(_netlist(store), config=cfg)

    assert not outcome.passed
    assert outcome.escalate_to is EscalationLevel.OUTER
    assert outcome.attempts == 1  # no retune for DRC failure
    assert svc.calls == [cfg]


# --------------------------------------------------------------------------- #
# Config / contract validation
# --------------------------------------------------------------------------- #
def test_driver_rejects_mismatched_design_id(store: SqliteArtifactStore) -> None:
    svc = StubLibreLaneService(runs=[_pass_run()], store=store)
    driver = PhysicalStageDriver(
        service=svc,  # type: ignore[arg-type]
        store=store, design_id="d0",
    )
    netlist = _netlist(store, design_id="other")
    cfg = PhysicalConfig(design_name="counter", top_module="counter")
    with pytest.raises(PhysicalStageError):
        driver.drive(netlist, config=cfg)


def test_driver_rejects_bad_budget_and_deltas(store: SqliteArtifactStore) -> None:
    svc = StubLibreLaneService(runs=[_pass_run()], store=store)
    for kwargs in (
        {"max_inner_attempts": 0},
        {"utilization_delta": 0.0},
        {"utilization_delta": 1.5},
        {"floor_utilization": 0.0},
        {"floor_utilization": 1.5},
    ):
        with pytest.raises(PhysicalStageError):
            PhysicalStageDriver(
                service=svc,  # type: ignore[arg-type]
                store=store, design_id="d0", **kwargs,
            )


def test_real_service_protocol_acceptable(store: SqliteArtifactStore) -> None:
    # The driver's `service` field is typed as the concrete class; smoke-check
    # that a real one constructs (no flow run here, just attribute lookup).
    fake_sandbox = type("X", (), {"run": lambda *a, **k: None})()
    real_service = LibreLanePhysicalService(sandbox=fake_sandbox, store=store)  # type: ignore[arg-type]
    PhysicalStageDriver(service=real_service, store=store, design_id="d0")
