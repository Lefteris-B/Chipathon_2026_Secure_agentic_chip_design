"""Stage-driver injection context for the LangGraph spine (F9.1).

The control graph's macro nodes (RTL → SYNTH → PHYSICAL → SIGNOFF →
GDSII) need access to typed stage drivers + the artifact store + tracer
+ audit log, but those instances are not JSON-serialisable — they can't
ride on LangGraph's ``RunnableConfig.configurable`` (which the
checkpointer round-trips through msgpack). Instead the spine reads them
from a :class:`StageContext` closure-captured by
:func:`~chip_agent.graph.state_graph.build_design_graph`.

The dataclass is intentionally a bag of *optional* fields: it lets the
CLI hand the graph a fully-wired context for real runs, the per-driver
test fixtures hand in only the driver they exercise, and the F5.1
back-compat tests skip the context entirely (the placeholder
``_advance(stage)`` path is preserved when ``stage_context is None``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from chip_agent.agents.assertion_gen import AssertionGenAgent
from chip_agent.agents.contract_extraction import ContractExtractionAgent
from chip_agent.agents.gdsii_stage import GDSIIStageDriver
from chip_agent.agents.human_hint_distill import HumanHintDistillAgent
from chip_agent.agents.oracle_gen import OracleGenAgent
from chip_agent.agents.oracle_verification import OracleVerificationGate
from chip_agent.agents.physical_repair_routing import PhysicalRepairRoutingAgent
from chip_agent.agents.physical_stage import PhysicalStageDriver
from chip_agent.agents.reflection_routing import ReflectionRoutingAgent
from chip_agent.agents.rtl_stage import RTLStageDriver
from chip_agent.agents.signoff_stage import SignoffStageDriver
from chip_agent.agents.synth_stage import SynthStageDriver
from chip_agent.design_state import (
    DesignState,
    FailureDiagnosis,
    TestbenchArtifact,
)
from chip_agent.obs.audit_log import SqliteAuditLog
from chip_agent.obs.tracing import NoopTracer, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.librelane import PhysicalConfig

__all__ = ["StageContext"]


@dataclass(frozen=True)
class StageContext:
    """Closure-captured dependencies the graph's stage nodes need.

    Every driver field is optional so the same context type powers
    test fixtures (one stage at a time) and real CLI runs (every
    stage). A node falls back to placeholder behaviour when the
    matching driver is ``None`` — the F5.1 graph tests rely on this
    when they pass ``stage_context=None`` outright.
    """

    store: SqliteArtifactStore
    tracer: Tracer = field(default_factory=NoopTracer)
    audit_log: SqliteAuditLog | None = None

    rtl_driver: RTLStageDriver | None = None
    synth_driver: SynthStageDriver | None = None
    physical_driver: PhysicalStageDriver | None = None
    signoff_driver: SignoffStageDriver | None = None
    gdsii_driver: GDSIIStageDriver | None = None

    # F19.7: M19 Phase 1 agents wired between PLAN and RTL. Each is
    # optional; the matching node falls back to the placeholder
    # ``_advance(stage)`` behaviour when ``None`` (preserves the F5.1
    # back-compat path for tests that drive a subset of the spine).
    contract_extractor: ContractExtractionAgent | None = None
    oracle_gen: OracleGenAgent | None = None
    assertion_gen: AssertionGenAgent | None = None
    oracle_verifier: OracleVerificationGate | None = None

    # F19.9: adaptive reflection routing — RTL outer-loop recovery
    # dispatcher. When the RTL outer budget is spent the graph calls
    # ``classify(...)`` to pick among RE_EXTRACT_CONTRACT,
    # REVISIT_SIBLING_RTL, REGEN_CURRENT_RTL, ESCALATE_HUMAN. When
    # ``None`` the RTL node falls back to the pre-F19.9 behaviour
    # (escalate straight to the human gate).
    reflection_router: ReflectionRoutingAgent | None = None

    # F21.3: SIGNOFF timing-failure recovery dispatcher. When SIGNOFF's
    # timing leg closes with negative slack at one or more corners, the
    # graph calls ``classify(...)`` to pick among LOWER_DENSITY,
    # INCREASE_DELAY_OPTIMIZATION, RELAX_CLOCK_PERIOD, ESCALATE_HUMAN
    # and applies the corresponding LibreLane knob delta on the next
    # PHYSICAL re-entry. When ``None`` SIGNOFF failures escalate
    # straight to the human gate (today's pre-F21.3 path).
    physical_repair_router: PhysicalRepairRoutingAgent | None = None

    # F23.2/F23.3: interactive HUMAN-escalation repair. When the RTL
    # outer loop exhausts every automated route (F19.9 returns
    # ESCALATE_HUMAN), the graph — if BOTH of these are wired and the
    # stage has human turns left — opens an interactive turn instead of
    # dead-ending: ``human_transcript_for`` supplies the operator's chat
    # for the failing module (return ``None`` to decline / when no
    # interactive channel exists), ``human_hint_distiller`` distils it
    # into a typed HumanHint, and the graph re-enters a bounded retry.
    # Both ``None`` (the default, e.g. non-interactive ``run``) preserves
    # today's straight-to-human-gate behaviour byte-for-byte.
    human_hint_distiller: HumanHintDistillAgent | None = None
    human_transcript_for: (
        Callable[[DesignState, str, FailureDiagnosis], str | None] | None
    ) = None

    # F19.13: per-module fast-path threshold. The four M19 nodes call
    # ``_is_trivial_module(module, max_ports=ctx.m19_trivial_max_ports)``
    # and skip the per-module body when it returns True. Default ``2``
    # matches the literal AC; ``cli_stubs.build_demo_stage_context``
    # reads ``settings.routing.m19_trivial_max_ports`` and passes the
    # operator's choice through.
    m19_trivial_max_ports: int = 2

    # RTL stage extras — not on DesignState, supplied per-run by the CLI.
    # F19.8: callable now takes ``state`` so the closure can read M19
    # heads (oracle / assertion_spec) and dispatch to the differential
    # TB builder; legacy LLM-TB closures ignore ``state``.
    tb_for_module: Callable[[DesignState, str], TestbenchArtifact] | None = None

    # Physical / signoff stage extras.
    physical_config: PhysicalConfig | None = None
    clock_period_ns: float | None = None
    # LVS spice extracted from the physical run dir; F9.1 leaves it
    # empty (stub LVS runners ignore it). F9.2 wires the real bytes.
    layout_netlist_bytes: bytes = b""

    # The Verilog ``module <name>`` token for the design's top module.
    # SIGNOFF (OpenSTA ``link_design``, Netgen ``lvs``) and GDSII (Magic
    # ``load <top>``) MUST see this — NOT the planner's ``mod_*`` handle
    # that lives on ``netlist.module_id`` / ``layout.module_id``. When
    # unset the legacy fallback to ``module_id`` applies, which matches
    # the F9.1 stub-driver tests but breaks live signoff (STA:
    # ``link_design`` rejects the unknown module; LVS: Netgen produces
    # ``LVS.UNKNOWN``; GDS: Magic ``load`` of the wrong cell name writes
    # a 130-byte zero-cell skeleton).
    top_module_verilog_name: str | None = None
