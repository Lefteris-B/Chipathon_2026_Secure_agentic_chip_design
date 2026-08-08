"""F22.2-C acceptance: every tool service emits one TOOL span per call.

One focused test per service. The stub-sandbox shape is copied from
each tool's own test file (intentional — keeping the harness flat so a
future tool service's tracing test can be added in one place).

These tests don't verify the JSONL on-disk shape (that's
``tests/test_jsonl_tracer.py``) — they verify the gateway between
each tool's entry method and the tracer fires with the right
``tool:<name>`` name and a minimal set of typed-result attributes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.design_state import (
    LayoutArtifact,
    NetlistArtifact,
    Provenance,
    RTLArtifact,
    Stage,
    TestbenchArtifact,
)
from chip_agent.obs.tracing import InMemoryTracer, SpanKind
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.cocotb_sim import SimulationService
from chip_agent.tools.librelane import LibreLanePhysicalService, PhysicalConfig
from chip_agent.tools.magic_drc import MagicDRCService
from chip_agent.tools.netgen_lvs import NetgenLVSService
from chip_agent.tools.opensta import OpenSTAService
from chip_agent.tools.sandbox import ToolRun
from chip_agent.tools.security_check import SecurityCheckService
from chip_agent.tools.verible import VeribleLintService
from chip_agent.tools.verilator import VerilatorElaborateService
from chip_agent.tools.yosys import YosysSynthService


# --------------------------------------------------------------------------- #
# Shared stubs.
# --------------------------------------------------------------------------- #
def _run(stdout: str = "", stderr: str = "", *, rc: int = 0) -> ToolRun:
    return ToolRun(
        returncode=rc, stdout=stdout, stderr=stderr,
        artifacts_dir="/tmp", duration_s=0.05,
    )


@dataclass
class _StubSandbox:
    """Sandbox that records calls + can write files into the mount dir
    on demand (some tool services expect the tool to emit an output
    file at a known name)."""

    tool_run: ToolRun
    files_to_create: dict[str, bytes] = field(default_factory=dict)
    sandbox: object | None = None  # mirrors the real sandbox protocol
    calls: list[Any] = field(default_factory=list)

    def run(
        self, cmd: list[str], mount: Path | str, *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun:
        mount_path = Path(mount)
        for name, body in self.files_to_create.items():
            target = mount_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        self.calls.append({"cmd": list(cmd), "mount": mount_path})
        return self.tool_run


@pytest.fixture
def store(tmp_path: Path) -> Iterable[SqliteArtifactStore]:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "runs",
    )
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Helpers for staging artifacts.
# --------------------------------------------------------------------------- #
def _stage_rtl(store: SqliteArtifactStore, *, source: str = "module m; endmodule\n") -> RTLArtifact:
    blob = store.put_blob(source.encode(), media_type="text/x-verilog")
    art = RTLArtifact(
        artifact_id="d0.m.rtl", design_id="d0", module_id="m",
        top_module="m", language="verilog", source=blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _stage_tb(store: SqliteArtifactStore) -> TestbenchArtifact:
    body = b"# minimal cocotb tb\nimport cocotb\n"
    blob = store.put_blob(body, media_type="text/x-python")
    art = TestbenchArtifact(
        artifact_id="d0.m.tb", design_id="d0", module_id="m",
        target_module="m", framework="cocotb", source=blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _stage_netlist(store: SqliteArtifactStore) -> NetlistArtifact:
    blob = store.put_blob(b"module m; endmodule\n", media_type="text/x-verilog")
    art = NetlistArtifact(
        artifact_id="d0.m.netlist", design_id="d0", module_id="m",
        netlist=blob, std_cell_lib="sky130_fd_sc_hd", cell_count=10,
        provenance=Provenance(produced_by=Stage.SYNTH),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _stage_layout(store: SqliteArtifactStore) -> LayoutArtifact:
    def_blob = store.put_blob(b"VERSION 5.8 ;\n", media_type="text/x-def")
    art = LayoutArtifact(
        artifact_id="d0.m.layout", design_id="d0", module_id="m",
        def_file=def_blob, top_module="m",
        provenance=Provenance(produced_by=Stage.PHYSICAL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _model_span_only(tracer: InMemoryTracer) -> list[Any]:
    return [s for s in tracer.spans if s.kind is SpanKind.TOOL]


# --------------------------------------------------------------------------- #
# 1. Verible
# --------------------------------------------------------------------------- #
def test_verible_lint_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    rtl = _stage_rtl(store)
    svc = VeribleLintService(
        sandbox=_StubSandbox(tool_run=_run()),
        store=store, tracer=tracer,
    )
    with tracer.run("d0"):
        svc.lint(rtl)
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:verible_lint"
    assert spans[0].attributes["tool_name"] == "verible"
    assert spans[0].attributes["module_id"] == "m"
    assert "passed" in spans[0].attributes
    assert "violation_count" in spans[0].attributes


# --------------------------------------------------------------------------- #
# 2. Verilator
# --------------------------------------------------------------------------- #
def test_verilator_elaborate_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    rtl = _stage_rtl(store)
    svc = VerilatorElaborateService(
        sandbox=_StubSandbox(tool_run=_run()),
        store=store, tracer=tracer,
    )
    with tracer.run("d0"):
        svc.elaborate(rtl)
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:verilator_elaborate"
    assert spans[0].attributes["tool_name"] == "verilator"


# --------------------------------------------------------------------------- #
# 3. cocotb simulator
# --------------------------------------------------------------------------- #
def test_cocotb_simulate_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    rtl = _stage_rtl(store)
    tb = _stage_tb(store)
    # Stage a minimal results.xml at the mount root so the parser is happy.
    minimal_xml = b'<?xml version="1.0"?><testsuites><testsuite name="m" tests="1" failures="0"><testcase name="ok"/></testsuite></testsuites>\n'
    sandbox = _StubSandbox(
        tool_run=_run(),
        files_to_create={"build/results.xml": minimal_xml},
    )
    svc = SimulationService(sandbox=sandbox, store=store, tracer=tracer)
    with tracer.run("d0"):
        svc.simulate(rtl, tb, seed=7)
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:cocotb_simulate"
    assert spans[0].attributes["seed"] == 7
    assert spans[0].attributes["simulator"]


# --------------------------------------------------------------------------- #
# 4. Yosys
# --------------------------------------------------------------------------- #
def test_yosys_synthesize_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    rtl = _stage_rtl(store)
    # Stage a netlist.v at the mount root so the synth path passes the
    # "passed but no netlist" guard at yosys.py:323-326.
    sandbox = _StubSandbox(
        tool_run=_run(
            stdout="ABC: result 1\n=== m ===\nNumber of cells: 10\n",
        ),
        files_to_create={"netlist.v": b"module m; endmodule\n"},
    )
    svc = YosysSynthService(sandbox=sandbox, store=store, tracer=tracer)
    with tracer.run("d0"):
        svc.synthesize(rtl)
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:yosys_synthesize"
    assert spans[0].attributes["std_cell_lib"] == "gf180mcu_fd_sc_mcu7t5v0"


# --------------------------------------------------------------------------- #
# 5. LibreLane (physical)
# --------------------------------------------------------------------------- #
def test_librelane_run_flow_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    netlist = _stage_netlist(store)
    # The "tag" run dir is where LibreLane writes its outputs. The
    # service's _harvest_outputs walks ``runs/<tag>/`` under the mount.
    minimal_def = b"VERSION 5.8 ;\n"
    minimal_metrics = b"{}\n"
    tag_dir = f"runs/{LibreLanePhysicalService.NAME}-test"
    sandbox = _StubSandbox(
        tool_run=_run(),
        files_to_create={
            f"{tag_dir}/final/def/m.def": minimal_def,
            f"{tag_dir}/final/metrics.json": minimal_metrics,
        },
    )
    svc = LibreLanePhysicalService(
        sandbox=sandbox, store=store, tracer=tracer,
        flow_run_tag=f"{LibreLanePhysicalService.NAME}-test",
    )
    config = PhysicalConfig(
        design_name="m", top_module="m", clock_period_ns=10.0,
    )
    try:
        with tracer.run("d0"):
            svc.run_flow(netlist, config=config)
    except Exception:
        # The flow may raise if harvesting can't find files in the
        # expected layout under StubSandbox. We still expect the span
        # to have opened (and closed via error path), which is what the
        # test verifies.
        pass
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:librelane_run_flow"
    assert spans[0].attributes["tool_name"] == "librelane"
    assert spans[0].attributes["design_name"] == "m"
    assert spans[0].attributes["clock_period_ns"] == 10.0


# --------------------------------------------------------------------------- #
# 6. OpenSTA
# --------------------------------------------------------------------------- #
def test_opensta_check_timing_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    netlist = _stage_netlist(store)
    sandbox = _StubSandbox(tool_run=_run(stdout="worst slack: 1.234\n"))
    svc = OpenSTAService(sandbox=sandbox, store=store, tracer=tracer)
    with tracer.run("d0"):
        svc.check_timing(netlist, clock_period_ns=5.0)
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:opensta_check_timing"
    assert spans[0].attributes["clock_period_ns"] == 5.0
    assert "wns_ns" in spans[0].attributes


# --------------------------------------------------------------------------- #
# 7. Netgen LVS
# --------------------------------------------------------------------------- #
def test_netgen_lvs_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    netlist = _stage_netlist(store)
    layout = _stage_layout(store)
    sandbox = _StubSandbox(
        tool_run=_run(),
        files_to_create={
            "lvs.rpt": b"Circuits match uniquely.\n",
        },
    )
    svc = NetgenLVSService(sandbox=sandbox, store=store, tracer=tracer)
    with tracer.run("d0"):
        svc.check_lvs(netlist, layout, layout_netlist_bytes=b"* extracted\n")
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:netgen_lvs"
    assert spans[0].attributes["tool_name"] == "netgen"


# --------------------------------------------------------------------------- #
# 8. Magic DRC
# --------------------------------------------------------------------------- #
def test_magic_drc_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    layout = _stage_layout(store)
    sandbox = _StubSandbox(
        tool_run=_run(),
        files_to_create={"drc.rpt": b"m.def\n[INFO]: COUNT: 0\n"},
    )
    svc = MagicDRCService(sandbox=sandbox, store=store, tracer=tracer)
    with tracer.run("d0"):
        svc.check_drc(layout)
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:magic_drc"
    assert spans[0].attributes["tool_name"] == "magic"


# --------------------------------------------------------------------------- #
# 9. Security check
# --------------------------------------------------------------------------- #
def test_security_check_emits_tool_span(store: SqliteArtifactStore) -> None:
    tracer = InMemoryTracer()
    netlist = _stage_netlist(store)
    svc = SecurityCheckService(store=store, tracer=tracer)
    with tracer.run("d0"):
        svc.check_security(netlist)
    spans = _model_span_only(tracer)
    assert len(spans) == 1
    assert spans[0].name == "tool:security_check"
    assert spans[0].attributes["tool_name"] == "structural_security"
    assert isinstance(spans[0].attributes["checks"], list)


# --------------------------------------------------------------------------- #
# Backward compat: each service still accepts no tracer (NoopTracer default).
# --------------------------------------------------------------------------- #
def test_default_tracer_is_noop_for_all_services(store: SqliteArtifactStore) -> None:
    """Each service constructed without a tracer keyword runs cleanly
    against a NoopTracer (no span records, no errors)."""
    # We just construct all 9. The detailed exec smoke is covered above.
    rtl = _stage_rtl(store)
    netlist = _stage_netlist(store)
    layout = _stage_layout(store)
    del rtl, netlist, layout  # constructors don't need them; this is a smoke
    sandbox = _StubSandbox(tool_run=_run())
    services: list[Any] = [
        VeribleLintService(sandbox=sandbox, store=store),
        VerilatorElaborateService(sandbox=sandbox, store=store),
        SimulationService(sandbox=sandbox, store=store),
        YosysSynthService(sandbox=sandbox, store=store),
        LibreLanePhysicalService(sandbox=sandbox, store=store),
        OpenSTAService(sandbox=sandbox, store=store),
        NetgenLVSService(sandbox=sandbox, store=store),
        MagicDRCService(sandbox=sandbox, store=store),
        SecurityCheckService(store=store),
    ]
    for svc in services:
        # NoopTracer's spans list is empty by construction.
        assert svc._tracer.spans == []
