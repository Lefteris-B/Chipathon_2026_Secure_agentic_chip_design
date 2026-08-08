"""F19.7 — wiring tests for the M19 Phase 1 stages.

The four nodes (CONTRACT, ORACLE, ASSERTIONS, ORACLE_VERIFICATION) live
between PLAN and RTL on the LangGraph spine. Each is per-module and
single-shot; the OracleVerification gate is warning-only — a failing
verdict is recorded but does NOT route the spine to ``AWAITING_HUMAN``.

The end-to-end driver tests in :mod:`tests.test_state_graph_drivers`
already confirm the stages run from a real ``StageContext`` and produce
the expected artifacts. This file pins the narrow wiring invariants the
F19.7 AC names directly so a future refactor can't silently regress
them (enum order, node-name map, placeholder fallback, warning-only
verdict, ramp-stim helper shape).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from chip_agent.agents.stim_ramp import build_ramp_stim
from chip_agent.cli_stubs import build_demo_stage_context
from chip_agent.design_state import (
    ArtifactKind,
    DesignConstraints,
    DesignPlan,
    DesignState,
    DesignStatus,
    ModelRouter,
    ModuleDecl,
    ModuleState,
    Port,
    Provenance,
    Spec,
    Stage,
    StageStatus,
)
from chip_agent.graph.stage_context import StageContext
from chip_agent.graph.state_graph import (
    _NODE_NAMES,
    STAGE_ORDER,
    _node_for_stage,
    build_design_graph,
    open_sqlite_checkpointer,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.obs.tracing import NoopTracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import make_routing_config, make_test_router

HMAC_KEY = b"f19-7-test-hmac-key"
TOP = "counter"


# --------------------------------------------------------------------------- #
# Fixtures (mirror the F9.1 driver tests)
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


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = make_routing_config(tmp_path)
    r, _ = make_test_router(config_path=cfg)
    return r


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


def _thread(name: str) -> dict[str, object]:
    return {"configurable": {"thread_id": name}}


# --------------------------------------------------------------------------- #
# 1) STAGE_ORDER + _NODE_NAMES (the static contract of the spine)
# --------------------------------------------------------------------------- #
def test_stage_order_includes_four_new_m19_stages() -> None:
    """The four M19 Phase 1 stages sit between PLAN and RTL in that order."""
    pi = STAGE_ORDER.index(Stage.PLAN)
    ri = STAGE_ORDER.index(Stage.RTL)
    assert STAGE_ORDER[pi + 1 : ri] == [
        Stage.CONTRACT,
        Stage.ORACLE,
        Stage.ASSERTIONS,
        Stage.ORACLE_VERIFICATION,
    ]


def test_node_names_map_includes_four_new_entries() -> None:
    assert _NODE_NAMES[Stage.CONTRACT] == "contract"
    assert _NODE_NAMES[Stage.ORACLE] == "oracle"
    assert _NODE_NAMES[Stage.ASSERTIONS] == "assertions"
    assert _NODE_NAMES[Stage.ORACLE_VERIFICATION] == "oracle_verification"


# --------------------------------------------------------------------------- #
# 2) Placeholder fallback when ``stage_context is None``
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stage",
    [
        Stage.CONTRACT,
        Stage.ORACLE,
        Stage.ASSERTIONS,
        Stage.ORACLE_VERIFICATION,
    ],
)
def test_placeholder_node_used_when_context_is_none(stage: Stage) -> None:
    """F5.1 back-compat — no context => placeholder ``_advance(stage)``."""
    node = _node_for_stage(stage, None, terminal=False)
    assert node.__name__ == f"_advance_to_{stage.value}"


# --------------------------------------------------------------------------- #
# 3) Each new node falls back to a placeholder when its agent field is None
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stage",
    [
        Stage.CONTRACT,
        Stage.ORACLE,
        Stage.ASSERTIONS,
        Stage.ORACLE_VERIFICATION,
    ],
)
def test_node_skips_when_agent_field_is_none(
    stage: Stage, store: SqliteArtifactStore,
) -> None:
    """Driver-mode w/o the matching agent => just advance current_stage.

    F19.7 lets a partial :class:`StageContext` (e.g. signoff-only test
    fixture) skip the M19 nodes without crashing. Mirrors how
    ``rtl_driver=None`` already lets the RTL node short-circuit.
    """
    ctx = StageContext(store=store, tracer=NoopTracer())
    node = _node_for_stage(stage, ctx, terminal=False)
    # The placeholder closure returns just ``{"current_stage": stage}``.
    state = DesignState(design_id="d-x", name=TOP)
    out = node(state)
    assert out == {"current_stage": stage}


# --------------------------------------------------------------------------- #
# 4) Full spine produces all four M19 artifact kinds per module
# --------------------------------------------------------------------------- #
def test_full_spine_promotes_all_four_m19_heads(
    tmp_path: Path,
    store: SqliteArtifactStore,
    audit: SqliteAuditLog,
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
        raw_final = graph.invoke(None, _thread("run"))
    final = (
        raw_final
        if isinstance(raw_final, DesignState)
        else DesignState.model_validate(raw_final)
    )

    module_stages = final.modules[TOP].stages
    for stage in (
        Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS,
        Stage.ORACLE_VERIFICATION,
    ):
        ss = module_stages[stage]
        assert ss.head is not None, f"{stage.value} head was not promoted"
        assert ss.status is StageStatus.PASSED


def test_full_spine_emits_audit_event_per_m19_promotion(
    tmp_path: Path,
    store: SqliteArtifactStore,
    audit: SqliteAuditLog,
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

    promoted = {
        e.payload.get("stage")
        for e in audit.events(design_id="d1")
        if e.event_type is EventType.ARTIFACT_PROMOTED
    }
    assert {
        Stage.CONTRACT.value, Stage.ORACLE.value,
        Stage.ASSERTIONS.value, Stage.ORACLE_VERIFICATION.value,
    } <= promoted


# --------------------------------------------------------------------------- #
# 5) Warning-only behaviour: a failing verdict does NOT escalate
# --------------------------------------------------------------------------- #
def test_oracle_verification_warning_only_does_not_block_rtl(
    tmp_path: Path,
    store: SqliteArtifactStore,
    audit: SqliteAuditLog,
    router: ModelRouter,
) -> None:
    """Even though the canned oracle + assertions disagree with the
    counter RTL on the ramp stim (the ramp doesn't cover the 255->0
    wrap), the verdict is informational — the spine still reaches
    SYNTH / PHYSICAL / SIGNOFF / GDSII.
    """
    spec = _make_spec(store, design_id="d2")
    plan = _make_plan(store, design_id="d2")
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id="d2", top_module=TOP, constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        graph.invoke(_initial(spec, plan), _thread("run"))
        raw_final = graph.invoke(None, _thread("run"))
    final = (
        raw_final
        if isinstance(raw_final, DesignState)
        else DesignState.model_validate(raw_final)
    )

    verdict_ref = final.modules[TOP].stages[Stage.ORACLE_VERIFICATION].head
    assert verdict_ref is not None
    assert verdict_ref.kind is ArtifactKind.ORACLE_VERIFICATION
    # The spine still drove through to GDSII regardless of the verdict.
    assert final.status is DesignStatus.COMPLETED
    assert final.stages[Stage.GDSII].head is not None


# --------------------------------------------------------------------------- #
# 6) Deterministic ramp-stim helper
# --------------------------------------------------------------------------- #
def _counter_module() -> ModuleDecl:
    return ModuleDecl(
        module_id=TOP, name=TOP, description="counter",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="en", direction="in", width=1),
            Port(name="q", direction="out", width=8),
        ],
    )


def test_build_ramp_stim_has_correct_reset_polarity() -> None:
    stim = build_ramp_stim(_counter_module())
    # Cycle 0 asserts active-low reset; cycle 1 releases it.
    assert stim[0]["rst_n"] == 0
    assert stim[1]["rst_n"] == 1
    # The clock is pinned to 1 across every cycle.
    assert all(s["clk"] == 1 for s in stim)
    # Output ports never appear in stim.
    assert all("q" not in s for s in stim)


def test_build_ramp_stim_is_reproducible() -> None:
    assert build_ramp_stim(_counter_module()) == build_ramp_stim(_counter_module())


def test_build_ramp_stim_active_high_reset_polarity() -> None:
    module = ModuleDecl(
        module_id="m", name="m", description="m",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst", direction="in", width=1),
            Port(name="data", direction="in", width=4),
            Port(name="out", direction="out", width=4),
        ],
    )
    stim = build_ramp_stim(module)
    # Active-high reset asserts at cycle 0 then deasserts.
    assert stim[0]["rst"] == 1
    assert stim[1]["rst"] == 0
    # 4-bit ramp wraps modulo 16. cycle 2 -> 1, cycle 3 -> 2 ...
    assert stim[2]["data"] == 1
    assert stim[3]["data"] == 2
    assert stim[4]["data"] == 3


def test_build_ramp_stim_rejects_short_cycles() -> None:
    with pytest.raises(ValueError, match="cycles must be >= 2"):
        build_ramp_stim(_counter_module(), cycles=1)


# --------------------------------------------------------------------------- #
# 7) StageContext accepts the four optional fields
# --------------------------------------------------------------------------- #
def test_stage_context_accepts_four_new_optional_agent_fields(
    store: SqliteArtifactStore,
) -> None:
    """Constructing with only the four new fields unset should default to None."""
    ctx = StageContext(store=store)
    assert ctx.contract_extractor is None
    assert ctx.oracle_gen is None
    assert ctx.assertion_gen is None
    assert ctx.oracle_verifier is None


# --------------------------------------------------------------------------- #
# 8) F19.8 dispatch — differential TB when M19 heads present, LLM fallback otherwise
# --------------------------------------------------------------------------- #
def test_tb_for_module_uses_diff_tb_when_m19_heads_present(
    tmp_path: Path,
    store: SqliteArtifactStore,
    audit: SqliteAuditLog,
    router: ModelRouter,
) -> None:
    """After F19.7 runs CONTRACT/ORACLE/ASSERTIONS heads, the TB picked
    by ``ctx.tb_for_module(state, module_id)`` is the F19.8 differential
    harness — its provenance.inputs list contains the oracle ref.
    """
    spec = _make_spec(store, design_id="d-diff")
    plan = _make_plan(store, design_id="d-diff")
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id="d-diff", top_module=TOP, constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        graph.invoke(_initial(spec, plan), _thread("d-diff"))
        raw_final = graph.invoke(None, _thread("d-diff"))
    final = (
        raw_final
        if isinstance(raw_final, DesignState)
        else DesignState.model_validate(raw_final)
    )

    # The persisted TB has the oracle ref in its provenance lineage.
    tb_id = f"d-diff.{TOP}.tb"
    tb = store.get_by_id(tb_id)
    assert tb is not None
    input_ids = {r.artifact_id for r in tb.provenance.inputs}
    assert f"d-diff.{TOP}.oracle" in input_ids
    assert f"d-diff.{TOP}.assertions" in input_ids
    # And the design reached COMPLETED — the differential TB doesn't
    # block the spine when M19 artifacts are wired.
    assert final.status is DesignStatus.COMPLETED


# --------------------------------------------------------------------------- #
# 9) F19.9 dispatch — reflection routing picks recovery route on RTL failure
# --------------------------------------------------------------------------- #
def test_rtl_failure_goes_to_human_when_no_reflection_router(
    tmp_path: Path,
    store: SqliteArtifactStore,
    audit: SqliteAuditLog,
    router: ModelRouter,
) -> None:
    """Pre-F19.9 behaviour: ``reflection_router=None`` => human gate.

    Builds the demo context, drops the reflection router, and forces
    an RTL failure via the same monkeypatch pattern
    :mod:`tests.test_state_graph_drivers` uses for the HUMAN-gate
    test.
    """
    from dataclasses import replace

    from chip_agent.agents.rtl_stage import RTLStageOutcome
    from chip_agent.design_state import EscalationLevel, LintResult, Provenance, RTLArtifact

    spec = _make_spec(store, design_id="d-no-reflect")
    plan = _make_plan(store, design_id="d-no-reflect")
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id="d-no-reflect", top_module=TOP,
        constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )
    # Drop the F19.9 router so the failure path falls through to the
    # human gate (the pre-F19.9 behaviour).
    ctx = replace(ctx, reflection_router=None)

    def _human_escalation(plan, module_id, tb, **_kw):  # type: ignore[no-untyped-def]
        blob = store.put_blob(b"// stub\n", media_type="text/x-verilog")
        rtl = RTLArtifact(
            artifact_id=f"d-no-reflect.{module_id}.rtl",
            design_id="d-no-reflect", module_id=module_id,
            top_module=module_id, source=blob,
            provenance=Provenance(produced_by=Stage.RTL),
        )
        store.put(rtl)
        rtl_loaded = store.get_by_id(rtl.artifact_id)
        assert isinstance(rtl_loaded, RTLArtifact)
        lint = LintResult(
            artifact_id=f"d-no-reflect.{module_id}.lint",
            design_id="d-no-reflect", module_id=module_id,
            passed=False, violations=[],
            provenance=Provenance(produced_by=Stage.RTL),
        )
        store.put(lint)
        lint_loaded = store.get_by_id(lint.artifact_id)
        assert isinstance(lint_loaded, LintResult)
        return RTLStageOutcome(
            passed=False, escalate_to=EscalationLevel.HUMAN,
            rtl=rtl_loaded, rtl_ref=rtl_loaded.ref(),
            lint=lint_loaded, elaborate=None, sim=None, diagnosis=None,
            inner_attempts=1, outer_attempts=0,
            versions=[1], last_failure=lint_loaded.ref(),
        )
    assert ctx.rtl_driver is not None
    ctx.rtl_driver.drive_module = _human_escalation  # type: ignore[method-assign]

    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        paused = graph.invoke(_initial(spec, plan), _thread("no-reflect"))
    state = (
        paused
        if isinstance(paused, DesignState)
        else DesignState.model_validate(paused)
    )
    assert state.status is DesignStatus.AWAITING_HUMAN
    # The budget is untouched because the reflection dispatcher never
    # ran.
    assert state.global_feedback_budget == 2


def test_rtl_failure_escalates_human_when_budget_exhausted(
    tmp_path: Path,
    store: SqliteArtifactStore,
    audit: SqliteAuditLog,
    router: ModelRouter,
) -> None:
    """``global_feedback_budget=0`` => skip reflection, go straight to HUMAN.

    Defends against reflection routing looping forever: when the
    budget is already spent, the dispatcher must route to the human
    gate without consulting the router.
    """
    from chip_agent.agents.rtl_stage import RTLStageOutcome
    from chip_agent.design_state import (
        EscalationLevel,
        LintResult,
        Provenance,
        RTLArtifact,
    )

    spec = _make_spec(store, design_id="d-budget")
    plan = _make_plan(store, design_id="d-budget")
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id="d-budget", top_module=TOP,
        constraints=spec.constraints,
        router=router, spec=spec, plan=plan,
    )

    def _human_escalation(plan, module_id, tb, **_kw):  # type: ignore[no-untyped-def]
        blob = store.put_blob(b"// stub\n", media_type="text/x-verilog")
        rtl = RTLArtifact(
            artifact_id=f"d-budget.{module_id}.rtl",
            design_id="d-budget", module_id=module_id,
            top_module=module_id, source=blob,
            provenance=Provenance(produced_by=Stage.RTL),
        )
        store.put(rtl)
        rtl_loaded = store.get_by_id(rtl.artifact_id)
        assert isinstance(rtl_loaded, RTLArtifact)
        lint = LintResult(
            artifact_id=f"d-budget.{module_id}.lint",
            design_id="d-budget", module_id=module_id,
            passed=False, violations=[],
            provenance=Provenance(produced_by=Stage.RTL),
        )
        store.put(lint)
        lint_loaded = store.get_by_id(lint.artifact_id)
        assert isinstance(lint_loaded, LintResult)
        return RTLStageOutcome(
            passed=False, escalate_to=EscalationLevel.HUMAN,
            rtl=rtl_loaded, rtl_ref=rtl_loaded.ref(),
            lint=lint_loaded, elaborate=None, sim=None, diagnosis=None,
            inner_attempts=1, outer_attempts=0,
            versions=[1], last_failure=lint_loaded.ref(),
        )
    assert ctx.rtl_driver is not None
    ctx.rtl_driver.drive_module = _human_escalation  # type: ignore[method-assign]

    initial = _initial(spec, plan)
    initial.global_feedback_budget = 0   # exhausted

    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        paused = graph.invoke(initial, _thread("budget-spent"))
    state = (
        paused
        if isinstance(paused, DesignState)
        else DesignState.model_validate(paused)
    )
    assert state.status is DesignStatus.AWAITING_HUMAN
    assert state.global_feedback_budget == 0  # untouched: no decrement


def test_dispatch_rtl_failure_routes_by_reflection_kind(
    store: SqliteArtifactStore,
) -> None:
    """Unit test the F19.9 dispatcher directly with a stub
    ``ReflectionRoutingAgent``.

    Bypasses the graph so we can exercise every route enum cleanly
    without staging a full sim failure. Mirrors the contract /
    oracle / assertion / RTL head setup F19.7 + F19.8 produce in a
    real run, then verifies the four ``ReflectionRoute`` kinds map
    to the expected ``Command(goto=...)`` targets and the right
    heads get cleared.
    """
    from dataclasses import dataclass

    from langgraph.types import Command

    from chip_agent.agents.rtl_stage import RTLStageOutcome
    from chip_agent.design_state import (
        AssertionSpec,
        BehaviorInvariant,
        ContractArtifact,
        EscalationLevel,
        FailureDiagnosis,
        ModuleState,
        OracleArtifact,
        Provenance,
        ReflectionRoute,
        ReflectionRouteKind,
        RTLArtifact,
        Stage,
        StructuredInvariant,
    )
    from chip_agent.graph.heads import set_module_stage_head
    from chip_agent.graph.stage_context import StageContext
    from chip_agent.graph.state_graph import (
        _NODE_NAMES,
        HUMAN_REVIEW_NODE,
        _dispatch_rtl_failure,
    )

    @dataclass
    class _StubReflectionRouter:
        route: ReflectionRoute
        calls: int = 0

        def classify(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return self.route

    def _make_state(design_id: str) -> tuple[DesignState, object]:
        # Seed every M19 + RTL head so the dispatcher can pull them.
        contract = ContractArtifact(
            artifact_id=f"{design_id}.counter.contract",
            design_id=design_id,
            module_id="counter",
            behavior_invariants=[
                BehaviorInvariant(
                    name="inc", description="d", condition="c",
                ),
            ],
            provenance=Provenance(produced_by=Stage.PLAN),
        )
        store.put(contract)
        oracle_blob = store.put_blob(
            b"def reference(stim): return []\n", media_type="text/x-python",
        )
        oracle = OracleArtifact(
            artifact_id=f"{design_id}.counter.oracle",
            design_id=design_id,
            module_id="counter",
            source=oracle_blob,
            reference_fn_name="reference",
            provenance=Provenance(produced_by=Stage.PLAN),
        )
        store.put(oracle)
        spec_blob = store.put_blob(
            b"def assert_x(args): return (True, '')\n",
            media_type="text/x-python",
        )
        assertion_spec = AssertionSpec(
            artifact_id=f"{design_id}.counter.assertions",
            design_id=design_id,
            module_id="counter",
            source=spec_blob,
            assertions=[
                StructuredInvariant(name="x", callsite="assert_x"),
            ],
            provenance=Provenance(produced_by=Stage.PLAN),
        )
        store.put(assertion_spec)
        rtl_blob = store.put_blob(
            b"module counter(); endmodule\n",
            media_type="text/x-verilog",
        )
        rtl = RTLArtifact(
            artifact_id=f"{design_id}.counter.rtl",
            design_id=design_id,
            module_id="counter",
            top_module="counter",
            source=rtl_blob,
            provenance=Provenance(produced_by=Stage.RTL),
        )
        store.put(rtl)

        state = DesignState(
            design_id=design_id, name=TOP,
            top_module_id=TOP,
            modules={TOP: ModuleState(module_id=TOP, name=TOP)},
        )
        set_module_stage_head(state, TOP, Stage.CONTRACT, contract.ref())
        set_module_stage_head(state, TOP, Stage.ORACLE, oracle.ref())
        set_module_stage_head(state, TOP, Stage.ASSERTIONS, assertion_spec.ref())
        set_module_stage_head(state, TOP, Stage.RTL, rtl.ref())
        # Plan: one module so there are no siblings.
        plan = _make_plan(store, design_id=design_id)
        return state, plan

    diagnosis = FailureDiagnosis(
        artifact_id="d.diag", design_id="d", module_id="counter",
        cycle=4, failing_signal="q", expected="5", actual="10",
        provenance=Provenance(produced_by=Stage.RTL),
    )
    fake_outcome = RTLStageOutcome(
        passed=False, escalate_to=EscalationLevel.HUMAN,
        rtl=None, rtl_ref=None,
        lint=None, elaborate=None, sim=None, diagnosis=diagnosis,
        inner_attempts=1, outer_attempts=3,
        versions=[1], last_failure=None,
    )

    # --- 1) REGEN_CURRENT_RTL: dispatcher returns goto=rtl, budget decrements.
    state, plan = _make_state("d-regen")
    initial_budget = state.global_feedback_budget
    stub = _StubReflectionRouter(
        route=ReflectionRoute(kind=ReflectionRouteKind.REGEN_CURRENT_RTL),
    )
    ctx = StageContext(store=store, reflection_router=stub)  # type: ignore[arg-type]
    cmd = _dispatch_rtl_failure(ctx, state, plan, TOP, fake_outcome)
    assert isinstance(cmd, Command)
    assert cmd.goto == _NODE_NAMES[Stage.RTL]
    assert state.global_feedback_budget == initial_budget - 1
    # The RTL head was cleared.
    assert state.modules[TOP].stages[Stage.RTL].head is None
    # CONTRACT head still intact.
    assert state.modules[TOP].stages[Stage.CONTRACT].head is not None

    # --- 2) RE_EXTRACT_CONTRACT: dispatcher returns goto=contract,
    #        all M19 + RTL heads for this module cleared.
    state, plan = _make_state("d-recontract")
    stub = _StubReflectionRouter(
        route=ReflectionRoute(kind=ReflectionRouteKind.RE_EXTRACT_CONTRACT),
    )
    ctx = StageContext(store=store, reflection_router=stub)  # type: ignore[arg-type]
    cmd = _dispatch_rtl_failure(ctx, state, plan, TOP, fake_outcome)
    assert isinstance(cmd, Command)
    assert cmd.goto == _NODE_NAMES[Stage.CONTRACT]
    for stage in (
        Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS, Stage.RTL,
    ):
        assert state.modules[TOP].stages[stage].head is None

    # --- 3) ESCALATE_HUMAN: dispatcher returns goto=human_review.
    state, plan = _make_state("d-human")
    stub = _StubReflectionRouter(
        route=ReflectionRoute(kind=ReflectionRouteKind.ESCALATE_HUMAN),
    )
    ctx = StageContext(store=store, reflection_router=stub)  # type: ignore[arg-type]
    cmd = _dispatch_rtl_failure(ctx, state, plan, TOP, fake_outcome)
    assert isinstance(cmd, Command)
    assert cmd.goto == HUMAN_REVIEW_NODE
    # Heads stay intact on ESCALATE_HUMAN — the operator inspects them.
    assert state.modules[TOP].stages[Stage.RTL].head is not None

    # --- 4) REVISIT_SIBLING_RTL with a hallucinated sibling: ESCALATE_HUMAN
    #        fallback (target_module not in siblings).
    state, plan = _make_state("d-sib")
    stub = _StubReflectionRouter(
        route=ReflectionRoute(
            kind=ReflectionRouteKind.REVISIT_SIBLING_RTL,
            target_module="not_a_real_sibling",
        ),
    )
    ctx = StageContext(store=store, reflection_router=stub)  # type: ignore[arg-type]
    cmd = _dispatch_rtl_failure(ctx, state, plan, TOP, fake_outcome)
    assert isinstance(cmd, Command)
    assert cmd.goto == HUMAN_REVIEW_NODE
