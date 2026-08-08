"""Stub services + ``build_demo_stage_context`` factory.

The CLI runs the demo through the real stage drivers, but the underlying
tool services (Yosys, LibreLane, Magic, OpenSTA, Netgen, cocotb-sim,
Verible, Verilator) need real binaries that only land with the
``DockerSandbox`` path. Until then this module exposes the same stub
shapes the per-driver unit tests already use — :class:`_StubSandbox` that
records calls + optionally drops files into the mount, plus stub signoff
runners + stub RTL inner-loop seams. The drivers themselves are real;
only the seam below them is faked.

Everything here is deliberately concentrated so a single later feature
can delete the StubSandbox plumbing in one diff and swap each
``*StageDriver``'s service over to a real ``DockerSandbox``-backed one.

F10.1 removed the ``_StubRouter`` baked into this module. Callers must
now pass a real :class:`ModelRouter` (the CLI builds one from
``Settings.routing``; tests build one over a stub :class:`CompletionBackend`
via :mod:`tests._routing_stub`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from chip_agent.agents.assertion_gen import AssertionGenAgent
from chip_agent.agents.contract_extraction import ContractExtractionAgent
from chip_agent.agents.differential_tb import DifferentialTBBuilder
from chip_agent.agents.gdsii_stage import GDSIIStageDriver
from chip_agent.agents.human_hint_distill import HumanHintDistillAgent
from chip_agent.agents.oracle_gen import OracleGenAgent
from chip_agent.agents.oracle_verification import OracleVerificationGate
from chip_agent.agents.physical_repair_routing import PhysicalRepairRoutingAgent
from chip_agent.agents.physical_stage import PhysicalStageDriver
from chip_agent.agents.reflection_routing import ReflectionRoutingAgent
from chip_agent.agents.rtl_gen import Elaborator, Linter, RTLGenerationAgent
from chip_agent.agents.rtl_stage import RTLStageDriver, Simulator
from chip_agent.agents.signoff_stage import (
    DRCRunner,
    LVSRunner,
    SignoffStageDriver,
    STARunner,
)
from chip_agent.agents.stim_ramp import build_rich_stim
from chip_agent.agents.synth_stage import SynthStageDriver
from chip_agent.agents.testbench_gen import TestbenchGenAgent
from chip_agent.agents.vector_tb import VectorTBBuilder
from chip_agent.design_state import (
    DesignConstraints,
    DesignPlan,
    DesignState,
    DRCReport,
    FailureDiagnosis,
    LayoutArtifact,
    LintResult,
    LVSReport,
    ModelRouter,
    ModuleDecl,
    NetlistArtifact,
    Provenance,
    RTLArtifact,
    SecurityReport,
    SimulationResult,
    Spec,
    Stage,
    TestbenchArtifact,
    TimingReport,
    ToolRun,
    ToolVersion,
)
from chip_agent.graph.heads import (
    MissingHeadError,
    load_assertion_spec_head,
    load_oracle_head,
)
from chip_agent.graph.stage_context import StageContext
from chip_agent.obs.audit_log import SqliteAuditLog
from chip_agent.obs.tracing import Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike
from chip_agent.tools.cocotb_sim import SimulationService
from chip_agent.tools.gdsii_emit import GDSIIEmitService
from chip_agent.tools.librelane import (
    DEFAULT_FLOW_RUN_TAG,
    LibreLanePhysicalService,
    PhysicalConfig,
)
from chip_agent.tools.magic_drc import MagicDRCService
from chip_agent.tools.netgen_lvs import NetgenLVSService
from chip_agent.tools.opensta import OpenSTAService
from chip_agent.tools.pdk_paths import liberty_path as liberty_path_for
from chip_agent.tools.pdk_paths import magic_tech, netgen_setup
from chip_agent.tools.verible import VeribleLintService
from chip_agent.tools.verilator import VerilatorElaborateService
from chip_agent.tools.yosys import YosysSynthService

__all__ = ["build_demo_stage_context"]


# --------------------------------------------------------------------------- #
# Sandbox stub — same shape the per-driver unit tests already use.
# --------------------------------------------------------------------------- #
@dataclass
class _StubSandbox:
    """Records the call and, optionally, drops files into the mount.

    Each tool service has its own expectations about what files land in
    the mount dir after a successful run; the ``side_effect`` hook lets
    one stub class serve every service.
    """

    tool_run: ToolRun
    side_effect: Callable[[Path], None] | None = None
    calls: list[tuple[list[str], Path]] = field(default_factory=list)

    def run(
        self,
        cmd: list[str],
        mount: Path | str,
        *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun:
        mount_path = Path(mount)
        self.calls.append((list(cmd), mount_path))
        if self.side_effect is not None:
            self.side_effect(mount_path)
        return self.tool_run


def _ok_run(stdout: str = "", stderr: str = "") -> ToolRun:
    return ToolRun(
        returncode=0, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.0,
    )


# --------------------------------------------------------------------------- #
# Per-tool side effects (file drops). Each mirrors the smallest payload
# the real tool would write that lets the parser declare "passed".
# --------------------------------------------------------------------------- #
_YOSYS_CLEAN_STDOUT = (
    "2.21. Printing statistics.\n"
    "\n"
    "=== counter ===\n"
    "\n"
    "Number of wires:                  4\n"
    "Number of cells:                  1\n"
    "  sky130_fd_sc_hd__dfrtp_1          1\n"
)


def _drop_netlist(*, body: bytes, filename: str = "netlist.v") -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        (mount / filename).write_bytes(body)
    return _impl


_STUB_LAYOUT_SPICE = (
    "* stub extracted netlist — content shape only, no real devices\n"
    ".SUBCKT {top} clk rst_n\n"
    "* placeholder body so Netgen LVS has structurally-matching input\n"
    ".ENDS {top}\n"
)


def _drop_librelane_artifacts(
    *,
    design_name: str,
    def_text: str,
    metrics: dict[str, float],
    run_tag: str = DEFAULT_FLOW_RUN_TAG,
) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        final = mount / "runs" / run_tag / "final"
        (final / "def").mkdir(parents=True, exist_ok=True)
        (final / "def" / f"{design_name}.def").write_text(def_text)
        (final / "metrics.json").write_text(json.dumps(metrics))
        # F12.3: drop a canned SPICE so the harvest pass populates
        # ``LayoutArtifact.librelane_layout_spice``. The stub LVS runner
        # doesn't inspect the bytes; it just needs to exist so the
        # signoff driver doesn't raise SignoffStageError on missing data.
        spice_dir = mount / "runs" / run_tag / "final" / "spice"
        spice_dir.mkdir(parents=True, exist_ok=True)
        (spice_dir / f"{design_name}.spice").write_text(
            _STUB_LAYOUT_SPICE.format(top=design_name),
        )
    return _impl


def _drop_gds(*, top: str, content: bytes) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        (mount / f"{top}.gds").write_bytes(content)
    return _impl


def _clean_librelane_metrics() -> dict[str, float]:
    """A passing :class:`PhysicalRun` shape — no congestion, no DRC viol.

    F12.4 + F13.2: emits the IIC chipathon26 metric-key shape (double
    underscore between domain + field) so stub `LayoutArtifact`s carry
    non-zero cell_count + die_area_um2 + utilization_pct just like the
    live flow does.
    """
    return {
        "route__congestion__overflow": 0.0,
        "design__instance__count": 1.0,
        "design__core__area": 100.0,
        "design__die__area": 100.0,  # F13.2: chipathon26 canonical key
        "design__core__util": 0.3,   # F13.2: chipathon26 canonical key
    }


# --------------------------------------------------------------------------- #
# RTL inner-loop stubs (Linter / Elaborator / Simulator).
# --------------------------------------------------------------------------- #
@dataclass
class _StubChecker:
    """Pass-through linter + elaborator: every artifact passes."""

    seen: list[RTLArtifact] = field(default_factory=list)

    def _result(self, rtl: RTLArtifact) -> LintResult:
        self.seen.append(rtl)
        return LintResult(
            artifact_id=f"{rtl.design_id}.{rtl.module_id}.lint",
            design_id=rtl.design_id, module_id=rtl.module_id,
            passed=True,
            violations=[],
            provenance=Provenance(
                produced_by=Stage.RTL, inputs=[rtl.ref()],
            ),
        )

    def lint(self, rtl: RTLArtifact) -> LintResult:
        return self._result(rtl)

    def elaborate(self, rtl: RTLArtifact) -> LintResult:
        return self._result(rtl)


_DETERMINISTIC_SIM_VERSION = ToolVersion(
    name="deterministic-sim",
    version="stub",
)


@dataclass
class _DeterministicSimulator:
    """Pass-through cocotb-class simulator: every run passes.

    Used on the ``--sandbox stub`` path where no real container is
    available. The persisted ``SimulationResult.checker`` carries a
    stable marker (``deterministic-sim``) so tests can distinguish this
    path from the F10.2 docker path that runs real cocotb.
    """

    seen: list[tuple[RTLArtifact, TestbenchArtifact, int]] = field(default_factory=list)

    def simulate(
        self, rtl: RTLArtifact, tb: TestbenchArtifact, *, seed: int = 0,
    ) -> SimulationResult:
        self.seen.append((rtl, tb, seed))
        return SimulationResult(
            artifact_id=f"{rtl.design_id}.{rtl.module_id}.sim",
            design_id=rtl.design_id, module_id=rtl.module_id,
            passed=True,
            tests_total=1, tests_passed=1,
            failing_assertions=[],
            violations=[],
            checker=_DETERMINISTIC_SIM_VERSION,
            provenance=Provenance(
                produced_by=Stage.RTL, inputs=[rtl.ref(), tb.ref()],
            ),
        )


# --------------------------------------------------------------------------- #
# Signoff runner stubs — match the SIGNOFF driver's four Protocols.
# --------------------------------------------------------------------------- #
def _ok_timing(design_id: str, top_module: str) -> TimingReport:
    return TimingReport(
        artifact_id=f"{design_id}.{top_module}.timing",
        design_id=design_id, module_id=top_module,
        passed=True, wns_ns=0.5, tns_ns=0.0,
        setup_violations=0, hold_violations=0,
        metrics={"errors": 0.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _ok_drc(design_id: str, top_module: str) -> DRCReport:
    return DRCReport(
        artifact_id=f"{design_id}.{top_module}.drc",
        design_id=design_id, module_id=top_module,
        passed=True, violations=[],
        metrics={"errors": 0.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _ok_lvs(design_id: str, top_module: str) -> LVSReport:
    return LVSReport(
        artifact_id=f"{design_id}.{top_module}.lvs",
        design_id=design_id, module_id=top_module,
        passed=True, violations=[],
        metrics={"errors": 0.0, "mismatches": 0.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _ok_security(design_id: str, top_module: str) -> SecurityReport:
    return SecurityReport(
        artifact_id=f"{design_id}.{top_module}.security",
        design_id=design_id, module_id=top_module,
        passed=True, violations=[],
        metrics={"errors": 0.0},
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


@dataclass
class _StubSTA:
    report: TimingReport

    def check_timing(
        self,
        netlist: NetlistArtifact,
        *,
        clock_period_ns: float,
        sdc_text: str | None = None,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        sdf_bytes: bytes | None = None,
        netlist_bytes_override: bytes | None = None,
    ) -> TimingReport:
        return self.report


@dataclass
class _StubDRC:
    report: DRCReport

    def check_drc(
        self,
        layout: LayoutArtifact,
        *,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        librelane_report_bytes: bytes | None = None,
    ) -> DRCReport:
        return self.report


@dataclass
class _StubLVS:
    report: LVSReport

    def check_lvs(
        self,
        netlist: NetlistArtifact,
        layout: LayoutArtifact,
        *,
        layout_netlist_bytes: bytes,
        top_module: str | None = None,
        time_limit_s: int | None = None,
        netlist_bytes_override: bytes | None = None,
    ) -> LVSReport:
        return self.report


@dataclass
class _StubSecurity:
    report: SecurityReport

    def check_security(
        self,
        netlist: NetlistArtifact,
        *,
        layout: LayoutArtifact | None = None,
        top_module: str | None = None,
    ) -> SecurityReport:
        return self.report


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
# F19.8 — combinational modules (no clk port) bypass the differential
# TB builder and fall back to the LLM TB. Mirrors the heuristic in
# ``chip_agent.agents.stim_ramp`` and ``differential_tb._find_clock_port``.
_F19_8_CLOCK_NAMES = frozenset({"clk", "clock"})


def _has_clock_port(module: ModuleDecl) -> bool:
    return any(
        p.direction == "in" and p.name.lower() in _F19_8_CLOCK_NAMES
        for p in module.ports
    )


def build_demo_stage_context(
    *,
    store: SqliteArtifactStore,
    audit_log: SqliteAuditLog | None,
    tracer: Tracer,
    design_id: str,
    top_module: str,
    constraints: DesignConstraints,
    router: ModelRouter,
    spec: Spec,
    plan: DesignPlan,
    sandbox: SandboxLike | None = None,
    use_test_first_workflow: bool = True,
    m19_trivial_max_ports: int = 2,
    sta_corners: tuple[str, ...] | None = None,
    sta_report_power: bool = False,
    human_transcript_for: Callable[
        [DesignState, str, FailureDiagnosis], str | None
    ] | None = None,
) -> StageContext:
    """Wire a :class:`StageContext` for the demo.

    Each ``*StageDriver`` is real. With ``sandbox=None`` the underlying
    tool services run against in-memory ``_StubSandbox`` instances; when
    a ``SandboxLike`` is passed, Yosys / LibreLane / Verible / Verilator /
    Magic-GDS each run against it — the path that lights the real
    ``DockerSandbox``. The four signoff runners + the RTL inner-loop
    simulator stay stubbed; a later feature wires a real cocotb runner.
    """
    # ----- RTL stage --------------------------------------------------- #
    # The RTL inner loop's lint + elaborate gates run against real
    # Verible / Verilator when a sandbox is supplied; without a sandbox
    # we keep the in-memory _StubChecker. F10.2 also swaps the cocotb
    # simulator: under ``--sandbox docker`` the real
    # :class:`SimulationService` runs cocotb inside the container so
    # the outer loop has a behavioural gate; on stub, the
    # ``_DeterministicSimulator`` keeps the demo offline-runnable.
    linter: Linter
    elaborator: Elaborator
    simulator: Simulator
    if sandbox is None:
        checker = _StubChecker()
        linter = checker
        elaborator = checker
        simulator = _DeterministicSimulator()
    else:
        linter = VeribleLintService(
            sandbox=sandbox, store=store, tracer=tracer,
        )
        elaborator = VerilatorElaborateService(
            sandbox=sandbox, store=store, tracer=tracer,
        )
        simulator = SimulationService(
            sandbox=sandbox, store=store, tracer=tracer,
        )
    gen_agent = RTLGenerationAgent(
        router=router, store=store,
        linter=linter, elaborator=elaborator,
        design_id=design_id,
    )
    rtl_driver = RTLStageDriver(
        gen_agent=gen_agent,
        simulator=simulator,
        store=store,
        router=router,
        linter=linter,
        elaborator=elaborator,
        design_id=design_id,
        audit_log=audit_log,  # F12.5: record RTL_FRONTIER_FALLBACK events
    )
    tb_gen = TestbenchGenAgent(
        router=router, store=store, design_id=design_id,
    )
    # F19.8: differential cocotb harness builder. Takes precedence over
    # ``tb_gen`` when the M19 oracle + assertions heads exist on the
    # per-module state; falls back to ``tb_gen`` otherwise so back-
    # compat tests (which don't run M19) still work.
    diff_tb_builder = DifferentialTBBuilder(
        store=store, design_id=design_id,
    )
    # Ground-truth vector TB for serial-load / completion-gated designs whose
    # spec ships published test vectors (e.g. a bit-serial block cipher). Takes
    # precedence over the differential TB when applicable because it checks the
    # DUT against the spec's known outputs instead of a model-generated oracle
    # (which the present80 run showed can be wrong) — and it drives a full
    # transaction so a high-latency design's completion is actually observed,
    # avoiding the ``proj_diff_tb_window_too_short`` vacuous pass.
    vector_tb_builder = VectorTBBuilder(
        store=store, design_id=design_id,
    )
    tb_cache: dict[str, TestbenchArtifact] = {}

    # ----- F19.7 + F19.9 M19 agents (between PLAN and RTL) ----------- #
    # The graph nodes call each agent once per module per run; results
    # land on the per-module ``ModuleState.stages[Stage.X].head``.
    # F19.11: the whole block is gated behind
    # ``use_test_first_workflow``. When ``False``, every M19 agent is
    # left ``None`` and the existing fallbacks fire:
    #   - The four M19 nodes (``chip_agent/graph/state_graph.py``
    #     :func:`_make_contract_node` and siblings) short-circuit to
    #     placeholder ``_advance(stage)``.
    #   - ``tb_for_module`` raises ``MissingHeadError`` looking for the
    #     oracle / assertion head and falls back to the LLM TB
    #     (``tb_gen.generate_module``).
    #   - ``_dispatch_rtl_failure`` sees ``reflection_router is None``
    #     and routes RTL failures straight to the human gate.
    contract_extractor: ContractExtractionAgent | None = None
    oracle_gen: OracleGenAgent | None = None
    assertion_gen: AssertionGenAgent | None = None
    oracle_verifier: OracleVerificationGate | None = None
    reflection_router: ReflectionRoutingAgent | None = None
    if use_test_first_workflow:
        contract_extractor = ContractExtractionAgent(
            router=router, design_id=design_id,
        )
        oracle_gen = OracleGenAgent(
            router=router, store=store, design_id=design_id,
        )
        assertion_gen = AssertionGenAgent(
            router=router, store=store, design_id=design_id,
        )
        oracle_verifier = OracleVerificationGate(
            store=store, design_id=design_id,
        )
        reflection_router = ReflectionRoutingAgent(
            router=router, design_id=design_id,
        )

    # F23.2: interactive human-hint distiller. Wired whenever a router is
    # available so the graph CAN open an interactive HUMAN turn — but it
    # only fires when a ``human_transcript_for`` provider is ALSO wired
    # (an interactive surface like chat/TUI supplies one; non-interactive
    # ``run`` leaves it None, so the escalation path is unchanged).
    human_hint_distiller = HumanHintDistillAgent(
        router=router, design_id=design_id,
    )

    # F21.3: physical-repair routing is INDEPENDENT of the test-first
    # workflow flag — SIGNOFF timing-failure recovery works on any
    # spec. Wired whenever a router is supplied so the dispatcher can
    # call ``classify(...)`` on a SIGNOFF gate close; when no router
    # is available the dispatcher's defensive fallback keeps the
    # spine on today's HUMAN-escalation path.
    physical_repair_router = PhysicalRepairRoutingAgent(
        router=router, design_id=design_id,
    )

    def tb_for_module(
        state: DesignState, module_id: str,
    ) -> TestbenchArtifact:
        if module_id in tb_cache:
            return tb_cache[module_id]
        module = next(
            (m for m in plan.modules if m.module_id == module_id), None,
        )
        if module is None:
            raise KeyError(
                f"module_id {module_id!r} not in plan "
                f"{plan.artifact_id!r}",
            )
        # F19.8 — prefer the differential builder when both M19 heads
        # are promoted AND the module is clocked. F19.7 wires CONTRACT/
        # ORACLE/ASSERTIONS/ORACLE_VERIFICATION before RTL, so in any
        # normal run the M19 branch fires. Combinational modules (no
        # clk-like input — e.g. the ALU + decoder demos) fall back to
        # the LLM TB because the differential harness needs cocotb's
        # Clock primitive to advance the DUT.
        if not _has_clock_port(module):
            tb_cache[module_id] = tb_gen.generate_module(
                module, plan, spec=spec,
            )
            return tb_cache[module_id]
        # Ground-truth vector TB first: for a serial-load / completion-gated
        # design whose spec ships published vectors, this is trustworthy where
        # the differential oracle is not. Returns None (fall through) otherwise.
        vector_tb = vector_tb_builder.build(module, spec, plan)
        if vector_tb is not None:
            tb_cache[module_id] = vector_tb
            return tb_cache[module_id]
        try:
            oracle = load_oracle_head(state, module_id, store)
            assertion_spec = load_assertion_spec_head(
                state, module_id, store,
            )
        except MissingHeadError:
            tb_cache[module_id] = tb_gen.generate_module(
                module, plan, spec=spec,
            )
            return tb_cache[module_id]

        stim = build_rich_stim(module, seed=0)
        tb_cache[module_id] = diff_tb_builder.build(
            module, oracle, assertion_spec,
            stim=stim, plan=plan,
            spec_ref=spec.ref(),
        )
        return tb_cache[module_id]

    # ----- SYNTH stage ------------------------------------------------- #
    synth_sandbox: SandboxLike = sandbox or _StubSandbox(
        tool_run=_ok_run(stdout=_YOSYS_CLEAN_STDOUT),
        side_effect=_drop_netlist(
            body=f"// stub netlist for {top_module}\n".encode(),
        ),
    )
    synth_driver = SynthStageDriver(
        service=YosysSynthService(
            sandbox=synth_sandbox, store=store, tracer=tracer,
        ),
        store=store,
        design_id=design_id,
    )

    # ----- PHYSICAL stage --------------------------------------------- #
    physical_sandbox: SandboxLike = sandbox or _StubSandbox(
        tool_run=_ok_run(),
        side_effect=_drop_librelane_artifacts(
            design_name=top_module,
            def_text="# stub DEF\nEND DESIGN\n",
            metrics=_clean_librelane_metrics(),
        ),
    )
    physical_driver = PhysicalStageDriver(
        service=LibreLanePhysicalService(
            sandbox=physical_sandbox, store=store, tracer=tracer,
        ),
        store=store,
        design_id=design_id,
    )
    physical_config = PhysicalConfig(
        design_name=top_module,
        top_module=top_module,
        clock_period_ns=constraints.target_clock_ns or 10.0,
        target_utilization=constraints.target_utilization or 0.5,
        std_cell_lib=constraints.std_cell_lib,
        pdk=constraints.pdk,
        sta_corners=sta_corners,
        sta_report_power=sta_report_power,
    )

    # ----- SIGNOFF stage ---------------------------------------------- #
    # F12.1: real OpenSTAService when a sandbox is supplied — the timing
    # leg now reads_sdf the LibreLane CTS SDF (when harvested). DRC / LVS /
    # security remain stubbed until F12.2/F12.3/F12.4 wire their real
    # runners.
    sta: STARunner
    drc: DRCRunner
    lvs: LVSRunner
    if sandbox is None:
        sta = _StubSTA(report=_ok_timing(design_id, top_module))
        drc = _StubDRC(report=_ok_drc(design_id, top_module))
        lvs = _StubLVS(report=_ok_lvs(design_id, top_module))
    else:
        sta = OpenSTAService(
            sandbox=sandbox, store=store, tracer=tracer,
            liberty_path=liberty_path_for(constraints.pdk, constraints.std_cell_lib),
        )
        drc = MagicDRCService(
            sandbox=sandbox, store=store, tracer=tracer,
            tech=magic_tech(constraints.pdk),
        )
        lvs = NetgenLVSService(
            sandbox=sandbox, store=store, tracer=tracer,
            setup_file=netgen_setup(constraints.pdk),
        )
    signoff_driver = SignoffStageDriver(
        sta=sta,
        drc=drc,
        lvs=lvs,
        security=_StubSecurity(report=_ok_security(design_id, top_module)),
        store=store,
        design_id=design_id,
    )

    # ----- GDSII stage ------------------------------------------------ #
    gdsii_sandbox: SandboxLike = sandbox or _StubSandbox(
        tool_run=_ok_run(
            stdout=f"Wrote 1 cells\nGDS written to {top_module}.gds\n",
        ),
        side_effect=_drop_gds(
            top=top_module,
            content=b"\x00CHIP_AGENT_DEMO_STUB_GDS\x00",
        ),
    )
    gdsii_driver = GDSIIStageDriver(
        service=GDSIIEmitService(
            sandbox=gdsii_sandbox, store=store,
            tech=magic_tech(constraints.pdk),
        ),
        store=store,
        design_id=design_id,
    )

    return StageContext(
        store=store,
        tracer=tracer,
        audit_log=audit_log,
        rtl_driver=rtl_driver,
        synth_driver=synth_driver,
        physical_driver=physical_driver,
        signoff_driver=signoff_driver,
        gdsii_driver=gdsii_driver,
        contract_extractor=contract_extractor,
        oracle_gen=oracle_gen,
        assertion_gen=assertion_gen,
        oracle_verifier=oracle_verifier,
        reflection_router=reflection_router,
        physical_repair_router=physical_repair_router,
        human_hint_distiller=human_hint_distiller,
        human_transcript_for=human_transcript_for,
        m19_trivial_max_ports=m19_trivial_max_ports,
        tb_for_module=tb_for_module,
        physical_config=physical_config,
        clock_period_ns=physical_config.clock_period_ns,
        # ``top_module`` here is already the resolved Verilog ``module
        # <name>`` token — both ``cli.cmd_run`` and ``cli._resume_stage_context``
        # call ``_resolve_verilog_top_name(plan)`` before passing it in.
        # Propagating it into the context plumbs the same name into the
        # SIGNOFF + GDSII nodes that previously fell back to the planner's
        # ``mod_*`` handle and produced empty / broken signoff artifacts.
        top_module_verilog_name=top_module,
    )
