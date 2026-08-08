"""F6.1 acceptance: :class:`SynthStageDriver` runs Yosys once on a module's
RTL head and returns a typed :class:`SynthStageOutcome` the gate handler
can apply later."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.agents.synth_stage import (
    SynthStageDriver,
    SynthStageError,
    SynthStageOutcome,
)
from chip_agent.design_state import (
    EscalationLevel,
    Provenance,
    RTLArtifact,
    Stage,
    ToolRun,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.yosys import YosysSynthService


# --------------------------------------------------------------------------- #
# Stubs / fixtures
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


@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(db_path=tmp_path / "store.sqlite",
                            content_dir=tmp_path / "runs")
    yield s
    s.close()


def _rtl(store: SqliteArtifactStore, *, top: str = "counter",
         design_id: str = "d0") -> RTLArtifact:
    blob = store.put_blob(b"module counter; endmodule\n",
                          media_type="text/x-verilog")
    art = RTLArtifact(
        artifact_id=f"{design_id}.{top}.rtl",
        design_id=design_id, module_id=top, top_module=top,
        language="verilog",
        source=blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(art)
    return store.get_by_id(art.artifact_id)  # type: ignore[return-value]


def _clean_run() -> ToolRun:
    return ToolRun(
        returncode=0,
        stdout=(
            "Number of wires:                  12\n"
            "Number of cells:                  17\n"
        ),
        stderr="", artifacts_dir="/tmp", duration_s=0.1,
    )


def _write_netlist(text: str) -> Callable[[Path], None]:
    def _impl(mount: Path) -> None:
        (mount / "netlist.v").write_text(text)
    return _impl


# --------------------------------------------------------------------------- #
# Happy path: clean synth -> passed outcome, no escalation
# --------------------------------------------------------------------------- #
def test_drive_emits_typed_outcome_on_pass(store: SqliteArtifactStore) -> None:
    rtl = _rtl(store)
    sandbox = StubSandbox(
        tool_run=_clean_run(),
        side_effect=_write_netlist("// gate-level\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    driver = SynthStageDriver(service=svc, store=store, design_id="d0")

    outcome = driver.drive(rtl)
    assert isinstance(outcome, SynthStageOutcome)
    assert outcome.passed
    assert outcome.escalate_to is None
    assert outcome.netlist.cell_count == 17
    # Both artifacts persisted to the store.
    assert outcome.netlist_ref.artifact_id == "d0.counter.netlist"
    assert outcome.report_ref.artifact_id == "d0.counter.synth_report"
    assert store.get(outcome.netlist_ref).artifact_id == outcome.netlist_ref.artifact_id


def test_drive_passes_std_cell_lib_through(store: SqliteArtifactStore) -> None:
    rtl = _rtl(store)
    sandbox = StubSandbox(
        tool_run=_clean_run(),
        side_effect=_write_netlist("// gate-level\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    driver = SynthStageDriver(service=svc, store=store, design_id="d0")
    outcome = driver.drive(rtl, std_cell_lib="sky130_fd_sc_ms")
    assert outcome.netlist.std_cell_lib == "sky130_fd_sc_ms"


# --------------------------------------------------------------------------- #
# Failure path: error-severity violation -> HUMAN escalation hint
# --------------------------------------------------------------------------- #
def test_drive_flags_human_when_gate_fails(store: SqliteArtifactStore) -> None:
    rtl = _rtl(store)
    sandbox = StubSandbox(
        tool_run=ToolRun(
            returncode=0,
            stdout=(
                "Warning: Latch inferred for signal `\\q' from process `p'.\n"
                "Number of cells:                  17\n"
            ),
            stderr="", artifacts_dir="/tmp", duration_s=0.1,
        ),
        side_effect=_write_netlist("// gate-level\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    driver = SynthStageDriver(service=svc, store=store, design_id="d0")

    outcome = driver.drive(rtl)
    assert not outcome.passed
    assert outcome.escalate_to is EscalationLevel.HUMAN
    assert any(v.code == "LATCH_INFERRED" for v in outcome.report.violations)


# --------------------------------------------------------------------------- #
# Design-id contract
# --------------------------------------------------------------------------- #
def test_driver_rejects_mismatched_design_id(store: SqliteArtifactStore) -> None:
    rtl = _rtl(store, design_id="other")
    sandbox = StubSandbox(
        tool_run=_clean_run(), side_effect=_write_netlist("// nl\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    driver = SynthStageDriver(service=svc, store=store, design_id="d0")
    with pytest.raises(SynthStageError):
        driver.drive(rtl)


def test_driver_rejects_empty_design_id(store: SqliteArtifactStore) -> None:
    sandbox = StubSandbox(
        tool_run=_clean_run(), side_effect=_write_netlist("// nl\n"),
    )
    svc = YosysSynthService(sandbox=sandbox, store=store)
    with pytest.raises(SynthStageError):
        SynthStageDriver(service=svc, store=store, design_id="")
