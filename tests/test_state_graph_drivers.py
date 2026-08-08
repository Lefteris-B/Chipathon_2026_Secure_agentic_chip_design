"""F9.1 — real stage drivers wired into the LangGraph spine.

Covers the four AC strands:

* the F8.3 counter demo runs through the real spine nodes when the
  CLI hands ``build_design_graph`` a :class:`StageContext` (stubbed
  ``SandboxLike`` + ``ModelRouter``);
* RTL escalation (``escalate_to=HUMAN``) routes the spine through the
  human gate;
* the SIGNOFF conjunction blocks advance and surfaces the failing leg;
* the F5.1 back-compat path stays green when
  ``stage_context is None`` (placeholder ``_advance(stage)`` nodes).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.agents.rtl_stage import RTLStageOutcome
from chip_agent.cli_stubs import build_demo_stage_context
from chip_agent.design_state import (
    ArtifactKind,
    DesignConstraints,
    DesignPlan,
    DesignState,
    DesignStatus,
    LintResult,
    ModelRouter,
    ModuleDecl,
    ModuleState,
    Port,
    Provenance,
    Spec,
    Stage,
    StageStatus,
)
from chip_agent.graph.state_graph import (
    build_design_graph,
    open_sqlite_checkpointer,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.obs.tracing import NoopTracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import make_routing_config, make_test_router

HMAC_KEY = b"f9-1-test-hmac-key"
TOP = "counter"


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = make_routing_config(tmp_path)
    r, _ = make_test_router(config_path=cfg)
    return r


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteArtifactStore]:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


@pytest.fixture
def audit(tmp_path: Path) -> Iterator[SqliteAuditLog]:
    a = SqliteAuditLog(db_path=tmp_path / "audit.sqlite", hmac_key=HMAC_KEY)
    yield a
    a.close()


@dataclass
class _Spec:
    design_id: str = "d0"


def _make_spec(store: SqliteArtifactStore, *, design_id: str) -> Spec:
    spec = Spec(
        artifact_id=f"{design_id}.spec",
        design_id=design_id,
        raw_text="counter",
        normalized="counter",
        requirements=[],
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC, agent="test"),
    )
    store.put(spec)
    loaded = store.get_by_id(spec.artifact_id)
    assert isinstance(loaded, Spec)
    return loaded


def _make_plan(store: SqliteArtifactStore, *, design_id: str) -> DesignPlan:
    module = ModuleDecl(
        module_id=TOP,
        name=TOP,
        description="counter",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="en", direction="in", width=1),
            Port(name="q", direction="out", width=8),
        ],
    )
    plan = DesignPlan(
        artifact_id=f"{design_id}.plan",
        design_id=design_id,
        top_module_id=TOP,
        modules=[module],
        provenance=Provenance(produced_by=Stage.PLAN, agent="test"),
    )
    store.put(plan)
    loaded = store.get_by_id(plan.artifact_id)
    assert isinstance(loaded, DesignPlan)
    return loaded


def _initial(spec: Spec, plan: DesignPlan) -> DesignState:
    return DesignState(
        design_id=spec.design_id,
        name=TOP,
        constraints=spec.constraints,
        spec=spec.ref(),
        plan=plan.ref(),
        top_module_id=TOP,
        modules={TOP: ModuleState(module_id=TOP, name=TOP)},
    )


def _thread(name: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": name}}


def _as_state(raw: Any) -> DesignState:
    if isinstance(raw, DesignState):
        return raw
    return DesignState.model_validate(raw)


# --------------------------------------------------------------------------- #
# AC strand 1 — full spine drives every stage to head + completion
# --------------------------------------------------------------------------- #
def test_full_spine_with_drivers_walks_to_completed(
    tmp_path: Path, store: SqliteArtifactStore, audit: SqliteAuditLog,
    router: ModelRouter,
) -> None:
    spec = _make_spec(store, design_id="d0")
    plan = _make_plan(store, design_id="d0")
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id="d0", top_module=TOP, constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        graph.invoke(_initial(spec, plan), _thread("run"))
        final = _as_state(graph.invoke(None, _thread("run")))

    # Every head on the design has advanced.
    assert final.modules[TOP].stages[Stage.RTL].head is not None
    assert final.stages[Stage.SYNTH].head is not None
    assert final.stages[Stage.PHYSICAL].head is not None
    assert final.stages[Stage.SIGNOFF].status is StageStatus.PASSED
    assert final.stages[Stage.GDSII].head is not None
    assert final.stages[Stage.GDSII].head.kind is ArtifactKind.GDSII
    assert final.status is DesignStatus.COMPLETED


def test_full_spine_emits_one_audit_event_per_promoted_head(
    tmp_path: Path, store: SqliteArtifactStore, audit: SqliteAuditLog,
    router: ModelRouter,
) -> None:
    spec = _make_spec(store, design_id="d1")
    plan = _make_plan(store, design_id="d1")
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id="d1", top_module=TOP, constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        graph.invoke(_initial(spec, plan), _thread("run"))
        graph.invoke(None, _thread("run"))

    events = list(audit.events(design_id="d1"))
    promoted_stages = {
        e.payload.get("stage")
        for e in events
        if e.event_type is EventType.ARTIFACT_PROMOTED
    }
    # The CLI emits SPEC/PLAN itself; the spine nodes own RTL/SYNTH/PHYSICAL/GDSII.
    assert {
        Stage.RTL.value, Stage.SYNTH.value,
        Stage.PHYSICAL.value, Stage.GDSII.value,
    } <= promoted_stages


# --------------------------------------------------------------------------- #
# AC strand 2 — RTL escalation routes the spine to the human gate
# --------------------------------------------------------------------------- #
def test_rtl_failure_routes_through_human_gate(
    tmp_path: Path, store: SqliteArtifactStore, router: ModelRouter,
) -> None:
    spec = _make_spec(store, design_id="d2")
    plan = _make_plan(store, design_id="d2")
    ctx = build_demo_stage_context(
        store=store, audit_log=None, tracer=NoopTracer(),
        design_id="d2", top_module=TOP, constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    # Replace the RTL driver's drive_module with a function that returns
    # a HUMAN-escalation outcome straight away.
    def _human_escalation(plan, module_id, tb, **_kw):  # type: ignore[no-untyped-def]
        from chip_agent.design_state import EscalationLevel, RTLArtifact
        from chip_agent.tools.trace import build_failure_diagnosis  # noqa: F401
        # Build a bare RTL artifact + LintResult solely so the outcome shape
        # is legal; the handler reads `passed`/`escalate_to` and ignores
        # the per-leg detail when no head can be promoted.
        blob = store.put_blob(b"// pretend\n", media_type="text/x-verilog")
        rtl = RTLArtifact(
            artifact_id=f"d2.{module_id}.rtl",
            design_id="d2", module_id=module_id, top_module=module_id,
            source=blob,
            provenance=Provenance(produced_by=Stage.RTL),
        )
        store.put(rtl)
        rtl_loaded = store.get_by_id(rtl.artifact_id)
        assert isinstance(rtl_loaded, RTLArtifact)
        lint = LintResult(
            artifact_id=f"d2.{module_id}.lint",
            design_id="d2", module_id=module_id,
            passed=False,
            violations=[],
            provenance=Provenance(produced_by=Stage.RTL),
        )
        store.put(lint)
        lint_loaded = store.get_by_id(lint.artifact_id)
        assert isinstance(lint_loaded, LintResult)
        return RTLStageOutcome(
            passed=False,
            escalate_to=EscalationLevel.HUMAN,
            rtl=rtl_loaded, rtl_ref=rtl_loaded.ref(),
            lint=lint_loaded, elaborate=None, sim=None, diagnosis=None,
            inner_attempts=1, outer_attempts=0,
            versions=[1], last_failure=lint_loaded.ref(),
        )
    assert ctx.rtl_driver is not None
    ctx.rtl_driver.drive_module = _human_escalation  # type: ignore[method-assign]

    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        paused = _as_state(
            graph.invoke(_initial(spec, plan), _thread("run")),
        )

    # The RTL outcome's HUMAN escalation should route the spine into the
    # await_human pause without a promoted RTL head.
    assert paused.status is DesignStatus.AWAITING_HUMAN
    assert paused.modules[TOP].stages[Stage.RTL].head is None


# --------------------------------------------------------------------------- #
# AC strand 3 — SIGNOFF conjunction blocks advance and points at the failing leg
# --------------------------------------------------------------------------- #
def test_signoff_conjunction_failure_blocks_advance(
    tmp_path: Path, store: SqliteArtifactStore, router: ModelRouter,
) -> None:
    spec = _make_spec(store, design_id="d3")
    plan = _make_plan(store, design_id="d3")
    ctx = build_demo_stage_context(
        store=store, audit_log=None, tracer=NoopTracer(),
        design_id="d3", top_module=TOP, constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    # Swap the STA stub for a failing timing report; the conjunction must
    # close the gate and route through await_human without GDSII.
    assert ctx.signoff_driver is not None
    failing = _failing_timing(design_id="d3")
    ctx.signoff_driver.sta = _failing_sta_runner(failing)  # type: ignore[assignment]

    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        paused = _as_state(
            graph.invoke(_initial(spec, plan), _thread("run")),
        )

    assert paused.current_stage is Stage.SIGNOFF
    assert paused.status is DesignStatus.AWAITING_HUMAN
    signoff_ss = paused.stages[Stage.SIGNOFF]
    assert signoff_ss.status is StageStatus.FAILED
    assert signoff_ss.last_failure is not None
    assert signoff_ss.last_failure.kind is ArtifactKind.STA
    # GDSII never fired.
    assert Stage.GDSII not in paused.stages or paused.stages[Stage.GDSII].head is None


# --------------------------------------------------------------------------- #
# AC strand 4 — back-compat: stage_context=None keeps placeholder behaviour
# --------------------------------------------------------------------------- #
def test_back_compat_placeholder_path_when_no_context(tmp_path: Path) -> None:
    """F5.1's guarantee — the existing test_state_graph.py path stays green."""
    initial = DesignState(design_id="d-bc", name=TOP)
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver)  # no stage_context
        graph.invoke(initial, _thread("bc"))
        final = _as_state(graph.invoke(None, _thread("bc")))

    assert final.current_stage is Stage.GDSII
    assert final.status is DesignStatus.COMPLETED
    # No driver-side heads land — the placeholder path only mutates
    # current_stage + status.
    assert Stage.GDSII not in final.stages or final.stages[Stage.GDSII].head is None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _failing_timing(design_id: str):  # type: ignore[no-untyped-def]
    from chip_agent.design_state import TimingReport
    return TimingReport(
        artifact_id=f"{design_id}.{TOP}.timing",
        design_id=design_id, module_id=TOP,
        passed=False, wns_ns=-0.3, tns_ns=-1.5,
        setup_violations=2, hold_violations=0,
        metrics={"errors": 1.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


@dataclass
class _FailingSTARunner:
    report: Any
    calls: list[float] = field(default_factory=list)

    def check_timing(
        self, netlist, *, clock_period_ns, sdc_text=None,
        top_module=None, time_limit_s=None, sdf_bytes=None,
        netlist_bytes_override=None,
    ):  # type: ignore[no-untyped-def]
        self.calls.append(clock_period_ns)
        return self.report


def _failing_sta_runner(report):  # type: ignore[no-untyped-def]
    return _FailingSTARunner(report=report)


# --------------------------------------------------------------------------- #
# F18 — top_module name propagation into SIGNOFF + GDSII.
#
# Background: the planner labels every module with a logical handle —
# typically ``mod_<name>`` (``ModuleDecl.module_id``) — and a separate
# ``ModuleDecl.name`` carrying the actual Verilog identifier the RTL
# specialist writes into ``module <name>``. Yosys / Verilator / LibreLane /
# Magic / Netgen / OpenSTA all key off the Verilog name; passing the handle
# instead blows them up silently. The live counter run shipped to GDSII with
# all four signoff legs misnamed:
#   * STA failed: ``1398 mod_counter_8bit is not a verilog module``
#   * LVS failed: ``LVS.UNKNOWN: Netgen produced no match verdict``
#   * GDS blob was 130 bytes (HEADER/BGNLIB/STRNAME/ENDLIB skeleton with
#     zero cells written) because Magic's ``load mod_counter_8bit
#     -dereference`` found no such cell in the layout
#
# The fix plumbs ``StageContext.top_module_verilog_name`` (resolved by
# ``_resolve_verilog_top_name(plan)`` in cli.py) into both nodes.
# --------------------------------------------------------------------------- #
MOD_HANDLE = "mod_counter"   # planner handle
VLOG_NAME = "counter"        # Verilog module declaration name


def _make_plan_with_handle_vs_name(
    store: SqliteArtifactStore, *, design_id: str,
) -> DesignPlan:
    """A plan where ``module_id`` and ``name`` differ — the shape that
    bit live signoff. The planner used ``mod_counter`` as its handle but
    the RTL specialist wrote ``module counter`` into the source."""
    module = ModuleDecl(
        module_id=MOD_HANDLE,
        name=VLOG_NAME,
        description="counter",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="en", direction="in", width=1),
            Port(name="q", direction="out", width=8),
        ],
    )
    plan = DesignPlan(
        artifact_id=f"{design_id}.plan",
        design_id=design_id,
        top_module_id=MOD_HANDLE,
        modules=[module],
        provenance=Provenance(produced_by=Stage.PLAN, agent="test"),
    )
    store.put(plan)
    loaded = store.get_by_id(plan.artifact_id)
    assert isinstance(loaded, DesignPlan)
    return loaded


def _initial_with_handle(spec: Spec, plan: DesignPlan) -> DesignState:
    return DesignState(
        design_id=spec.design_id,
        name=VLOG_NAME,
        constraints=spec.constraints,
        spec=spec.ref(),
        plan=plan.ref(),
        top_module_id=MOD_HANDLE,
        modules={MOD_HANDLE: ModuleState(module_id=MOD_HANDLE, name=VLOG_NAME)},
    )


@dataclass
class _SpyingSTA:
    """Records the top_module value SIGNOFF passes through."""
    inner: Any
    seen: list[str | None] = field(default_factory=list)

    def check_timing(self, netlist, **kwargs):  # type: ignore[no-untyped-def]
        self.seen.append(kwargs.get("top_module"))
        return self.inner.check_timing(netlist, **kwargs)


@dataclass
class _SpyingGDSDriver:
    """Records the top_module value GDSII passes through."""
    inner: Any
    seen: list[str | None] = field(default_factory=list)

    def drive(self, layout, *, top_module=None, time_limit_s=None):  # type: ignore[no-untyped-def]
        self.seen.append(top_module)
        return self.inner.drive(
            layout, top_module=top_module, time_limit_s=time_limit_s,
        )


def test_signoff_receives_verilog_name_not_planner_handle(
    tmp_path: Path, store: SqliteArtifactStore, audit: SqliteAuditLog,
    router: ModelRouter,
) -> None:
    """The signoff node must pass ``ModuleDecl.name`` (``counter``) to
    the STA leg — passing ``ModuleDecl.module_id`` (``mod_counter``) made
    OpenSTA's ``link_design`` reject the design with the verbatim error
    ``1398 mod_counter_8bit is not a verilog module``."""
    spec = _make_spec(store, design_id="d_signoff_name")
    plan = _make_plan_with_handle_vs_name(store, design_id="d_signoff_name")
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        # The CLI hands ``top_module`` already resolved to ``ModuleDecl.name``.
        design_id="d_signoff_name", top_module=VLOG_NAME,
        constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    assert ctx.top_module_verilog_name == VLOG_NAME

    assert ctx.signoff_driver is not None
    spying = _SpyingSTA(inner=ctx.signoff_driver.sta)
    ctx.signoff_driver.sta = spying  # type: ignore[assignment]

    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        graph.invoke(_initial_with_handle(spec, plan), _thread("sn"))
        graph.invoke(None, _thread("sn"))

    assert spying.seen, "STA leg was never called"
    assert all(seen == VLOG_NAME for seen in spying.seen), (
        f"STA leg received {spying.seen!r}; must be the Verilog name "
        f"{VLOG_NAME!r}, never the planner handle {MOD_HANDLE!r}."
    )


def test_gdsii_receives_verilog_name_not_layout_module_id(
    tmp_path: Path, store: SqliteArtifactStore, audit: SqliteAuditLog,
    router: ModelRouter,
) -> None:
    """The GDSII node must pass ``ModuleDecl.name`` (``counter``) to
    Magic's ``load <top> -dereference``. The pre-fix code defaulted to
    ``layout.module_id`` (``mod_counter``), which is the synth artifact's
    planner handle — Magic found no such cell and wrote a 130-byte
    zero-cell GDS skeleton (``metadata.cells_written: 0``)."""
    spec = _make_spec(store, design_id="d_gds_name")
    plan = _make_plan_with_handle_vs_name(store, design_id="d_gds_name")
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id="d_gds_name", top_module=VLOG_NAME,
        constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    assert ctx.gdsii_driver is not None
    spying = _SpyingGDSDriver(inner=ctx.gdsii_driver)
    object.__setattr__(ctx, "gdsii_driver", spying)  # frozen dataclass

    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        graph.invoke(_initial_with_handle(spec, plan), _thread("gn"))
        graph.invoke(None, _thread("gn"))

    assert spying.seen, "GDSII driver was never called"
    assert all(seen == VLOG_NAME for seen in spying.seen), (
        f"GDSII driver received {spying.seen!r}; must be the Verilog name "
        f"{VLOG_NAME!r}, never the planner handle {MOD_HANDLE!r}."
    )


def test_top_module_verilog_name_falls_back_when_context_unset(
    tmp_path: Path, store: SqliteArtifactStore,
) -> None:
    """Defensive: tests that drop ``stage_context`` entirely (F5.1
    back-compat) must keep walking through the placeholder nodes. Pins
    the ``or layout.module_id`` fallback in ``_make_gdsii_node``."""
    initial = DesignState(design_id="d-fb", name=VLOG_NAME)
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver)
        graph.invoke(initial, _thread("fb"))
        final = _as_state(graph.invoke(None, _thread("fb")))

    assert final.status is DesignStatus.COMPLETED
