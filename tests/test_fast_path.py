"""F19.13 acceptance: trivial-module fast path.

Pins the heuristic, the settings field + StageContext threading, and
the graph-level dispatch: trivial modules skip CONTRACT / ORACLE /
ASSERTIONS / ORACLE_VERIFICATION; non-trivial modules still run the
full M19 pipeline; each per-module routing call is logged as an
``EventType.M19_FAST_PATH_DECISION`` audit event with the literal
``m19_fast_path_used: true|false`` key the AC names.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chip_agent.design_state import (
    DesignConstraints,
    DesignPlan,
    DesignState,
    ModuleDecl,
    ModuleState,
    Port,
    Provenance,
    Spec,
    Stage,
)
from chip_agent.graph.state_graph import (
    _is_trivial_module,
    build_design_graph,
    open_sqlite_checkpointer,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.obs.tracing import NoopTracer
from chip_agent.settings import Settings
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import make_routing_config, make_test_router

HMAC_KEY = b"f19-13-fast-path-hmac"


# --------------------------------------------------------------------------- #
# Heuristic — pure function
# --------------------------------------------------------------------------- #
def _module(name: str, ports: list[Port]) -> ModuleDecl:
    return ModuleDecl(
        module_id=name, name=name, description=f"{name} module",
        ports=ports,
    )


def test_is_trivial_module_2_ports_no_clock_returns_true() -> None:
    """Buffer-like 2-port module (in, out) → True at the default threshold."""
    buf = _module("buf", [
        Port(name="d_in", direction="in", width=1),
        Port(name="d_out", direction="out", width=1),
    ])
    assert _is_trivial_module(buf, max_ports=2) is True


def test_is_trivial_module_with_clock_returns_false() -> None:
    """A clocked module is never trivial regardless of port count."""
    clocked = _module("clocked", [
        Port(name="clk", direction="in", width=1),
        Port(name="q", direction="out", width=1),
    ])
    assert _is_trivial_module(clocked, max_ports=2) is False
    assert _is_trivial_module(clocked, max_ports=8) is False


def test_is_trivial_module_3_ports_no_clock_obeys_threshold() -> None:
    """3-port combinational module passes only when ``max_ports>=3``."""
    and_gate = _module("and_gate", [
        Port(name="a", direction="in", width=1),
        Port(name="b", direction="in", width=1),
        Port(name="y", direction="out", width=1),
    ])
    assert _is_trivial_module(and_gate, max_ports=2) is False
    assert _is_trivial_module(and_gate, max_ports=3) is True


def test_is_trivial_module_active_low_reset_returns_false() -> None:
    """A module with ``rst_n`` is not trivial (reset name triggers state)."""
    m = _module("reg_d", [
        Port(name="rst_n", direction="in", width=1),
        Port(name="q", direction="out", width=1),
    ])
    assert _is_trivial_module(m, max_ports=2) is False
    assert _is_trivial_module(m, max_ports=8) is False


def test_is_trivial_module_empty_port_list_passes() -> None:
    """Degenerate 0-port module → True (≤ any max, no clock)."""
    void = _module("void", [])
    assert _is_trivial_module(void, max_ports=2) is True


# --------------------------------------------------------------------------- #
# Settings + StageContext plumbing
# --------------------------------------------------------------------------- #
def test_routing_settings_m19_trivial_max_ports_default_is_2(
    tmp_path: Path,
) -> None:
    """A YAML that omits the knob defaults it to ``2``."""
    cfg = tmp_path / "minimal.yaml"
    cfg.write_text(
        "constraints:\n  pdk: sky130A\n"
        "routing:\n"
        "  registry:\n    m: {provider: stub, model: m}\n"
        "  loops:\n    inner: {model: m, temperature: 0.0, n: 1}\n"
        "    outer: {model: m, temperature: 0.0, n: 1}\n"
        "  tasks: {}\n",
    )
    s = Settings.from_yaml(cfg)
    assert s.routing.m19_trivial_max_ports == 2


def test_routing_settings_m19_trivial_max_ports_range_validation(
    tmp_path: Path,
) -> None:
    """Field rejects values outside [1, 8]."""
    base = (
        "constraints:\n  pdk: sky130A\n"
        "routing:\n"
        "  m19_trivial_max_ports: {value}\n"
        "  registry:\n    m: {{provider: stub, model: m}}\n"
        "  loops:\n    inner: {{model: m, temperature: 0.0, n: 1}}\n"
        "    outer: {{model: m, temperature: 0.0, n: 1}}\n"
        "  tasks: {{}}\n"
    )
    # In-range values round-trip.
    for val in (1, 2, 4, 8):
        cfg = tmp_path / f"ok_{val}.yaml"
        cfg.write_text(base.format(value=val))
        s = Settings.from_yaml(cfg)
        assert s.routing.m19_trivial_max_ports == val
    # Out-of-range raises.
    for bad in (0, 9, 100):
        cfg = tmp_path / f"bad_{bad}.yaml"
        cfg.write_text(base.format(value=bad))
        with pytest.raises(ValidationError):
            Settings.from_yaml(cfg)


# --------------------------------------------------------------------------- #
# Graph-level fast path
# --------------------------------------------------------------------------- #
TOP_TRIVIAL = "buf"
TOP_NONTRIVIAL = "counter"


def _make_two_module_spec(
    store: SqliteArtifactStore, *, design_id: str,
) -> tuple[Spec, DesignPlan]:
    """Trivial 2-port buffer + non-trivial 4-port counter, in one plan."""
    spec = Spec(
        artifact_id=f"{design_id}.spec",
        design_id=design_id,
        raw_text="trivial buffer and a counter",
        normalized="2-port buffer alongside an 8-bit counter",
        requirements=[],
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC, agent="test"),
    )
    store.put(spec)
    spec_loaded = store.get_by_id(spec.artifact_id)
    assert isinstance(spec_loaded, Spec)

    buf = ModuleDecl(
        module_id=TOP_TRIVIAL, name=TOP_TRIVIAL,
        description="combinational buffer",
        ports=[
            Port(name="d_in", direction="in", width=1),
            Port(name="d_out", direction="out", width=1),
        ],
    )
    counter = ModuleDecl(
        module_id=TOP_NONTRIVIAL, name=TOP_NONTRIVIAL,
        description="8-bit counter",
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
        top_module_id=TOP_NONTRIVIAL,
        modules=[buf, counter],
        provenance=Provenance(produced_by=Stage.PLAN, agent="test"),
    )
    store.put(plan)
    plan_loaded = store.get_by_id(plan.artifact_id)
    assert isinstance(plan_loaded, DesignPlan)
    return spec_loaded, plan_loaded


def _initial_state(spec: Spec, plan: DesignPlan) -> DesignState:
    return DesignState(
        design_id=spec.design_id,
        name=TOP_NONTRIVIAL,
        constraints=spec.constraints,
        spec=spec.ref(),
        plan=plan.ref(),
        top_module_id=TOP_NONTRIVIAL,
        modules={
            TOP_TRIVIAL: ModuleState(module_id=TOP_TRIVIAL, name=TOP_TRIVIAL),
            TOP_NONTRIVIAL: ModuleState(
                module_id=TOP_NONTRIVIAL, name=TOP_NONTRIVIAL,
            ),
        },
        use_test_first_workflow=True,
    )


def _thread(name: str) -> dict[str, object]:
    return {"configurable": {"thread_id": name}}


def _fast_path_events(audit: SqliteAuditLog, *, design_id: str) -> list[dict]:
    return [
        dict(e.payload)
        for e in audit.events(design_id=design_id)
        if e.event_type is EventType.M19_FAST_PATH_DECISION
    ]


def test_m19_nodes_skip_trivial_modules_and_emit_audit_event(
    tmp_path: Path,
) -> None:
    """Run the M19 stages over a plan with one trivial + one non-trivial
    module. Assert: trivial module has NO M19 heads; non-trivial has all
    four; exactly two ``M19_FAST_PATH_DECISION`` events fire with the
    expected verdict.
    """
    from chip_agent.cli_stubs import build_demo_stage_context
    from chip_agent.design_state import ModelRouter

    cfg = make_routing_config(tmp_path)
    router, _ = make_test_router(config_path=cfg)
    assert isinstance(router, ModelRouter)

    store = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    audit = SqliteAuditLog(
        db_path=tmp_path / "audit.sqlite", hmac_key=HMAC_KEY,
    )
    design_id = "fp-multi"
    spec, plan = _make_two_module_spec(store, design_id=design_id)
    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id=design_id, top_module=TOP_NONTRIVIAL,
        constraints=spec.constraints, router=router,
        spec=spec, plan=plan,
        use_test_first_workflow=True,
        m19_trivial_max_ports=2,
    )
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        graph.invoke(_initial_state(spec, plan), _thread(design_id))
        raw_final = graph.invoke(None, _thread(design_id))
    final = (
        raw_final
        if isinstance(raw_final, DesignState)
        else DesignState.model_validate(raw_final)
    )

    # The trivial buffer has NO CONTRACT / ORACLE / ASSERTIONS /
    # ORACLE_VERIFICATION heads (fast path skipped them).
    buf_stages = final.modules[TOP_TRIVIAL].stages
    for stage in (
        Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS,
        Stage.ORACLE_VERIFICATION,
    ):
        assert (
            stage not in buf_stages or buf_stages[stage].head is None
        ), f"trivial module unexpectedly promoted {stage.value} head"

    # The counter has all four M19 heads.
    counter_stages = final.modules[TOP_NONTRIVIAL].stages
    for stage in (
        Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS,
        Stage.ORACLE_VERIFICATION,
    ):
        assert (
            stage in counter_stages and counter_stages[stage].head is not None
        ), f"non-trivial module missing {stage.value} head"

    # Audit: exactly two M19_FAST_PATH_DECISION events, one per module.
    events = _fast_path_events(audit, design_id=design_id)
    assert len(events) == 2
    by_module = {e["module_id"]: e for e in events}
    assert by_module[TOP_TRIVIAL]["m19_fast_path_used"] is True
    assert by_module[TOP_TRIVIAL]["port_count"] == 2
    assert by_module[TOP_TRIVIAL]["has_clock_or_reset"] is False
    assert by_module[TOP_NONTRIVIAL]["m19_fast_path_used"] is False
    assert by_module[TOP_NONTRIVIAL]["port_count"] == 4
    assert by_module[TOP_NONTRIVIAL]["has_clock_or_reset"] is True

    audit.close()
    store.close()


def test_m19_fast_path_no_events_when_workflow_flag_false(
    tmp_path: Path,
) -> None:
    """``use_test_first_workflow=False`` short-circuits the CONTRACT node
    before the per-module loop runs, so no M19_FAST_PATH_DECISION events
    fire. The fast path is a refinement of the M19 path, not an
    alternative to it.
    """
    from chip_agent.cli_stubs import build_demo_stage_context
    from chip_agent.design_state import ModelRouter

    cfg = make_routing_config(tmp_path)
    router, _ = make_test_router(config_path=cfg)
    assert isinstance(router, ModelRouter)

    store = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    audit = SqliteAuditLog(
        db_path=tmp_path / "audit.sqlite", hmac_key=HMAC_KEY,
    )
    design_id = "fp-off"
    spec, plan = _make_two_module_spec(store, design_id=design_id)

    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id=design_id, top_module=TOP_NONTRIVIAL,
        constraints=spec.constraints, router=router,
        spec=spec, plan=plan,
        use_test_first_workflow=False,
        m19_trivial_max_ports=2,
    )
    initial = _initial_state(spec, plan)
    # Reflect the workflow-flag-off path on the seeded state too.
    initial.use_test_first_workflow = False
    with open_sqlite_checkpointer(tmp_path / "ckpt.sqlite") as saver:
        graph = build_design_graph(checkpointer=saver, stage_context=ctx)
        graph.invoke(initial, _thread(design_id))
        graph.invoke(None, _thread(design_id))

    events = _fast_path_events(audit, design_id=design_id)
    assert events == []

    audit.close()
    store.close()
