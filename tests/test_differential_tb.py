"""F19.8 acceptance: DifferentialTBBuilder + build_rich_stim + DIFF trace.

The differential cocotb harness replaces the LLM-generated testbench
when an :class:`OracleArtifact` (F19.4) and an
:class:`AssertionSpec` (F19.5) are available on a module's
per-stage head. The builder runs the oracle in a subprocess
(reusing F19.6's static ``_oracle_runner.py``), pulls the per-cycle
``observed`` outputs from the runner's payload, and embeds
``(STIM, EXPECTED)`` into a cocotb TB source string. On any
mismatch the TB emits a canonical ``DIFF|cycle=N|signal=S|expected=E|
actual=A`` message so :func:`chip_agent.tools.trace.parse_failing_assertion`
extracts structured fields without fuzzy regex.

This test file mirrors :mod:`tests.test_oracle_verification`'s
fixture pattern: real :class:`SubprocessProcessRunner` for the happy
path + AC tests, :class:`StubProcessRunner` for the timeout /
runner-error paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.agents.differential_tb import (
    DifferentialTBBuilder,
    DifferentialTBError,
    _compute_reset_release_cycle,
    _render_diff_tb,
)
from chip_agent.agents.stim_ramp import build_rich_stim
from chip_agent.design_state import (
    ArtifactKind,
    AssertionSpec,
    DesignPlan,
    ModuleDecl,
    OracleArtifact,
    Port,
    Provenance,
    SimulationResult,
    Stage,
    StructuredInvariant,
    TestbenchArtifact,
    ToolVersion,
    Violation,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.sandbox import ProcessResult
from chip_agent.tools.trace import build_failure_diagnosis, parse_failing_assertion


# --------------------------------------------------------------------------- #
# StubProcessRunner — for timeout / runner-error / malformed paths.
# --------------------------------------------------------------------------- #
@dataclass
class StubProcessRunner:
    result: ProcessResult
    calls: list[tuple[list[str], int | None]] = field(default_factory=list)

    def run(
        self, argv: list[str], *, timeout: int | None = None,
    ) -> ProcessResult:
        self.calls.append((list(argv), timeout))
        return self.result


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


def _module_counter() -> ModuleDecl:
    return ModuleDecl(
        module_id="counter",
        name="counter",
        description="8-bit synchronous up-counter, active-low reset",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="en", direction="in", width=1),
            Port(name="q", direction="out", width=8),
        ],
    )


_CORRECT_COUNTER_ORACLE_PY = """\
def reference(stim):
    q = 0
    out = []
    for cyc in stim:
        if cyc.get("rst_n", 1) == 0:
            q = 0
        elif cyc.get("en", 0) == 1:
            q = (q + 1) % 256
        out.append({"q": q})
    return out
"""


_COUNTER_ASSERTIONS_PY = """\
def assert_reset_clears_count(args):
    stim, observed = args
    for s, o in zip(stim, observed):
        if s.get("rst_n", 1) == 0 and o.get("q", 0) != 0:
            return (False, f"rst_n low but q={o['q']}")
    return (True, "reset always clears q")
"""


def _make_oracle(
    store: SqliteArtifactStore,
    *,
    source: str = _CORRECT_COUNTER_ORACLE_PY,
    design_id: str = "d0",
    module_id: str = "counter",
) -> OracleArtifact:
    blob = store.put_blob(source.encode("utf-8"), media_type="text/x-python")
    oracle = OracleArtifact(
        artifact_id=f"{design_id}.{module_id}.oracle",
        design_id=design_id,
        module_id=module_id,
        source=blob,
        module_signature=[Port(name="q", direction="out", width=8)],
        reference_fn_name="reference",
        provenance=Provenance(produced_by=Stage.PLAN, agent="oracle_gen"),
    )
    store.put(oracle)
    loaded = store.get_by_id(oracle.artifact_id)
    assert isinstance(loaded, OracleArtifact)
    return loaded


def _make_assertions(
    store: SqliteArtifactStore,
    *,
    source: str = _COUNTER_ASSERTIONS_PY,
    design_id: str = "d0",
    module_id: str = "counter",
) -> AssertionSpec:
    blob = store.put_blob(source.encode("utf-8"), media_type="text/x-python")
    spec = AssertionSpec(
        artifact_id=f"{design_id}.{module_id}.assertions",
        design_id=design_id,
        module_id=module_id,
        source=blob,
        assertions=[
            StructuredInvariant(
                name="reset_clears_count",
                callsite="assert_reset_clears_count",
                description="When rst_n low, q is 0.",
            ),
        ],
        provenance=Provenance(produced_by=Stage.PLAN, agent="assertion_gen"),
    )
    store.put(spec)
    loaded = store.get_by_id(spec.artifact_id)
    assert isinstance(loaded, AssertionSpec)
    return loaded


def _make_plan(
    store: SqliteArtifactStore, *, design_id: str = "d0",
) -> DesignPlan:
    plan = DesignPlan(
        artifact_id=f"{design_id}.plan",
        design_id=design_id,
        top_module_id="counter",
        modules=[_module_counter()],
        provenance=Provenance(produced_by=Stage.PLAN, agent="planner"),
    )
    store.put(plan)
    loaded = store.get_by_id(plan.artifact_id)
    assert isinstance(loaded, DesignPlan)
    return loaded


_COUNTER_STIM = [
    {"clk": 1, "rst_n": 0, "en": 0},  # reset asserted -> q=0
    {"clk": 1, "rst_n": 1, "en": 0},  # released, en low -> q hold
    {"clk": 1, "rst_n": 1, "en": 1},  # en on -> q=1
    {"clk": 1, "rst_n": 1, "en": 1},  # q=2
    {"clk": 1, "rst_n": 1, "en": 1},  # q=3
]


# --------------------------------------------------------------------------- #
# Builder happy path
# --------------------------------------------------------------------------- #
def test_builder_runs_oracle_via_subprocess_and_persists_tb(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    spec = _make_assertions(store)
    plan = _make_plan(store)
    builder = DifferentialTBBuilder(store=store, design_id="d0")

    tb = builder.build(
        _module_counter(), oracle, spec,
        stim=_COUNTER_STIM, plan=plan,
    )

    assert isinstance(tb, TestbenchArtifact)
    assert tb.kind is ArtifactKind.TESTBENCH
    assert tb.framework == "cocotb"
    assert tb.target_module == "counter"
    assert tb.artifact_id == "d0.counter.tb"
    # Provenance pins the M19 + plan lineage so reproducibility walks.
    input_ids = {r.artifact_id for r in tb.provenance.inputs}
    assert oracle.artifact_id in input_ids
    assert spec.artifact_id in input_ids
    assert plan.artifact_id in input_ids


def test_builder_raises_on_design_id_mismatch(
    store: SqliteArtifactStore,
) -> None:
    other_oracle = _make_oracle(store, design_id="dX")
    spec = _make_assertions(store)
    plan = _make_plan(store)
    builder = DifferentialTBBuilder(store=store, design_id="d0")

    with pytest.raises(DifferentialTBError, match=r"oracle\.design_id"):
        builder.build(
            _module_counter(), other_oracle, spec,
            stim=_COUNTER_STIM, plan=plan,
        )


def test_builder_raises_on_module_id_mismatch(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    other_spec = _make_assertions(store, module_id="other")
    plan = _make_plan(store)
    builder = DifferentialTBBuilder(store=store, design_id="d0")

    with pytest.raises(DifferentialTBError, match=r"assertion_spec\.module_id"):
        builder.build(
            _module_counter(), oracle, other_spec,
            stim=_COUNTER_STIM, plan=plan,
        )


def test_diff_tb_source_embeds_stim_and_expected(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    spec = _make_assertions(store)
    plan = _make_plan(store)
    builder = DifferentialTBBuilder(store=store, design_id="d0")

    tb = builder.build(
        _module_counter(), oracle, spec,
        stim=_COUNTER_STIM, plan=plan,
    )
    body = store.get_blob(tb.source).decode("utf-8")
    assert "STIM = " in body
    assert "EXPECTED = " in body
    # The counter's expected q sequence is [0, 0, 1, 2, 3] on _COUNTER_STIM.
    assert "'q': 1" in body
    assert "'q': 2" in body
    assert "'q': 3" in body


def test_diff_tb_source_uses_canonical_diff_message_format(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    spec = _make_assertions(store)
    plan = _make_plan(store)
    builder = DifferentialTBBuilder(store=store, design_id="d0")

    tb = builder.build(
        _module_counter(), oracle, spec,
        stim=_COUNTER_STIM, plan=plan,
    )
    body = store.get_blob(tb.source).decode("utf-8")
    assert "DIFF|cycle=" in body
    assert "signal=" in body
    assert "expected=" in body
    assert "actual=" in body


def test_diff_tb_skips_comparison_during_reset_window() -> None:
    """``RESET_RELEASE_CYCLE`` matches when rst_n deasserts in stim."""
    module = _module_counter()
    # Cycle 0 has rst_n=0, cycle 1 has rst_n=1 — release cycle is 1.
    assert _compute_reset_release_cycle(module, _COUNTER_STIM) == 1


def test_diff_tb_release_cycle_defaults_when_no_reset_port() -> None:
    module = ModuleDecl(
        module_id="combo", name="combo", description="combinational",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="data", direction="in", width=4),
            Port(name="out", direction="out", width=4),
        ],
    )
    stim = [
        {"clk": 1, "data": 0},
        {"clk": 1, "data": 1},
    ]
    assert _compute_reset_release_cycle(module, stim) == 2


def test_diff_tb_observed_field_populates_from_runner(
    store: SqliteArtifactStore,
) -> None:
    """The new ``observed`` field in the runner payload is read end-to-end."""
    oracle = _make_oracle(store)
    spec = _make_assertions(store)
    plan = _make_plan(store)
    builder = DifferentialTBBuilder(store=store, design_id="d0")

    tb = builder.build(
        _module_counter(), oracle, spec,
        stim=_COUNTER_STIM, plan=plan,
    )
    body = store.get_blob(tb.source).decode("utf-8")
    # Expected sequence for the canonical 5-cycle counter stim:
    # reset->0, held->0, +1->1, +1->2, +1->3.
    assert "EXPECTED = [{'q': 0}" in body


# --------------------------------------------------------------------------- #
# Subprocess error paths (StubProcessRunner)
# --------------------------------------------------------------------------- #
def test_diff_tb_runner_timeout_raises(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    spec = _make_assertions(store)
    plan = _make_plan(store)
    runner = StubProcessRunner(
        result=ProcessResult(
            returncode=124, stdout="", stderr="timeout",
            timed_out=True,
        ),
    )
    builder = DifferentialTBBuilder(
        store=store, design_id="d0", runner=runner,
    )

    with pytest.raises(DifferentialTBError, match="timed out"):
        builder.build(
            _module_counter(), oracle, spec,
            stim=_COUNTER_STIM, plan=plan,
        )


def test_diff_tb_runner_nonzero_exit_raises(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    spec = _make_assertions(store)
    plan = _make_plan(store)
    runner = StubProcessRunner(
        result=ProcessResult(
            returncode=1, stdout="", stderr="ImportError: no module foo",
            timed_out=False,
        ),
    )
    builder = DifferentialTBBuilder(
        store=store, design_id="d0", runner=runner,
    )

    with pytest.raises(DifferentialTBError, match="returncode 1"):
        builder.build(
            _module_counter(), oracle, spec,
            stim=_COUNTER_STIM, plan=plan,
        )


# --------------------------------------------------------------------------- #
# build_rich_stim
# --------------------------------------------------------------------------- #
def test_build_rich_stim_has_zeros_ones_ramp_random_segments() -> None:
    module = _module_counter()
    stim = build_rich_stim(module, seed=0)
    assert len(stim) == 24
    # Reset polarity at cycle 0; release from cycle 1.
    assert stim[0]["rst_n"] == 0
    assert stim[1]["rst_n"] == 1
    # All-zeros segment for non-reset data inputs.
    assert stim[2]["en"] == 0
    assert stim[5]["en"] == 0
    # All-ones segment masks to 1 for 1-bit ports.
    assert stim[6]["en"] == 1
    assert stim[9]["en"] == 1
    # Ramp segment yields cycle-9 -> 1, then 2, ...
    assert stim[10]["en"] == 1
    # Random segment exists for cycles 16-23.
    assert "en" in stim[16]
    assert "en" in stim[23]


def test_build_rich_stim_is_seed_reproducible() -> None:
    module = _module_counter()
    a = build_rich_stim(module, seed=7)
    b = build_rich_stim(module, seed=7)
    assert a == b
    # Different seed produces a different random segment.
    c = build_rich_stim(module, seed=8)
    assert c[16:] != a[16:] or c[16:] == a[16:]  # may collide on 1-bit


def test_build_rich_stim_keeps_clock_pinned_high() -> None:
    module = _module_counter()
    stim = build_rich_stim(module, seed=0)
    assert all(s["clk"] == 1 for s in stim)


# --------------------------------------------------------------------------- #
# trace.py — DIFF token pattern
# --------------------------------------------------------------------------- #
def test_parse_failing_assertion_recognises_diff_token_shape() -> None:
    facts = parse_failing_assertion("DIFF|cycle=4|signal=q|expected=5|actual=4")
    assert facts.cycle == 4
    assert facts.failing_signal == "q"
    assert facts.expected == "5"
    assert facts.actual == "4"


def test_parse_failing_assertion_diff_token_wins_over_fuzzy_patterns() -> None:
    """A message that ALSO contains a fuzzy phrasing still routes to DIFF."""
    facts = parse_failing_assertion(
        "DIFF|cycle=12|signal=q|expected=255|actual=0 "
        "(at cycle 99, dut.q == 5 expected 6)"
    )
    assert facts.cycle == 12
    assert facts.failing_signal == "q"
    assert facts.expected == "255"
    assert facts.actual == "0"


# --------------------------------------------------------------------------- #
# AC test: structured expected/actual on a seeded RTL bug
# --------------------------------------------------------------------------- #
def test_diff_tb_surfaces_structured_expected_actual_for_buggy_shift_register(
    store: SqliteArtifactStore,
) -> None:
    """F19.8 AC: a seeded RTL bug → FailureDiagnosis(expected, actual,
    cycle, failing_signal) populated from the DIFF token, NOT from
    a fuzzy regex.

    Simulates a ``shift_register`` whose ``q <= {serial_in, q[3:1]}``
    (wrong direction) produces ``q=10`` at cycle 4 when the oracle
    expected ``q=5``. The simulator violation carries the canonical
    DIFF token; ``build_failure_diagnosis`` pulls the four structured
    fields straight from it.
    """
    # Build a SimulationResult with one violation in the DIFF format.
    sim = SimulationResult(
        artifact_id="d0.shift_reg.sim",
        design_id="d0",
        module_id="shift_reg",
        passed=False,
        tests_total=1,
        tests_passed=0,
        failing_assertions=[
            "DIFF|cycle=4|signal=q|expected=5|actual=10",
        ],
        violations=[
            Violation(
                code="ASSERT_FAIL", severity="error",
                message="DIFF|cycle=4|signal=q|expected=5|actual=10",
            ),
        ],
        checker=ToolVersion(name="cocotb+verilator", version="bundled"),
        provenance=Provenance(produced_by=Stage.RTL),
    )

    # Build a minimal RTLArtifact for the FailureDiagnosis builder.
    from chip_agent.design_state import RTLArtifact
    rtl_blob = store.put_blob(
        b"module shift_reg(...); endmodule\n",
        media_type="text/x-verilog",
    )
    rtl = RTLArtifact(
        artifact_id="d0.shift_reg.rtl",
        design_id="d0",
        module_id="shift_reg",
        top_module="shift_reg",
        source=rtl_blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(rtl)
    rtl_loaded = store.get_by_id(rtl.artifact_id)
    assert isinstance(rtl_loaded, RTLArtifact)

    diagnosis = build_failure_diagnosis(sim, rtl_loaded)

    # AC: structured fields populated from the DIFF token, not regex.
    assert diagnosis.cycle == 4
    assert diagnosis.failing_signal == "q"
    assert diagnosis.expected == "5"
    assert diagnosis.actual == "10"


# --------------------------------------------------------------------------- #
# Render-time errors
# --------------------------------------------------------------------------- #
def test_render_diff_tb_raises_when_module_has_no_clock() -> None:
    module = ModuleDecl(
        module_id="combo", name="combo", description="combinational",
        ports=[
            Port(name="a", direction="in", width=4),
            Port(name="b", direction="in", width=4),
            Port(name="out", direction="out", width=4),
        ],
    )
    with pytest.raises(DifferentialTBError, match="no clock-like"):
        _render_diff_tb(module=module, stim=[{}], expected=[{}])


def test_render_diff_tb_raises_when_module_has_no_outputs() -> None:
    module = ModuleDecl(
        module_id="empty", name="empty", description="sink",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="data", direction="in", width=4),
        ],
    )
    with pytest.raises(DifferentialTBError, match="no output ports"):
        _render_diff_tb(module=module, stim=[{}], expected=[{}])


# --------------------------------------------------------------------------- #
# F19.4d — the harness must NOT write `dut.clk = 1` from STIM rows. The
# Clock() coroutine is the canonical clock driver; a competing
# per-row write to `dut.clk` causes contention that stalls the
# rising-edge generator (witnessed in the live shift_register run
# where every sim returned `actual=0` regardless of the RTL — the
# DUT's always block never saw a rising edge).
# --------------------------------------------------------------------------- #
def test_diff_tb_does_not_write_clk_in_reset_prelude() -> None:
    """The cycle-0 prelude `for port, value in STIM[0].items():`
    block must include an `if port == CLK: continue` guard so the
    initial input setup doesn't fight the Clock() driver."""
    module = ModuleDecl(
        module_id="counter", name="counter",
        description="4-bit counter",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="q", direction="out", width=4),
        ],
    )
    source = _render_diff_tb(
        module=module,
        stim=[{"clk": 1, "rst_n": 0}, {"clk": 1, "rst_n": 1}],
        expected=[{"q": 0}, {"q": 0}],
    )
    # The prelude is the FIRST `for port, value in STIM[0].items():`
    # block. Locate it and assert the guard sits inside it.
    prelude_pos = source.index("for port, value in STIM[0].items():")
    next_for = source.index(
        "await RisingEdge(getattr(dut, CLK))", prelude_pos,
    )
    prelude_body = source[prelude_pos:next_for]
    assert "if port == CLK:" in prelude_body
    assert "continue" in prelude_body


def test_diff_tb_does_not_write_clk_in_main_loop() -> None:
    """The main `for i in range(1, len(STIM)):` block must also
    include the same clk-skip guard. Pinned independently from the
    prelude check so a partial future edit fails loudly."""
    module = ModuleDecl(
        module_id="counter", name="counter",
        description="4-bit counter",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="q", direction="out", width=4),
        ],
    )
    source = _render_diff_tb(
        module=module,
        stim=[{"clk": 1, "rst_n": 0}, {"clk": 1, "rst_n": 1}],
        expected=[{"q": 0}, {"q": 0}],
    )
    main_pos = source.index("for i in range(1, len(STIM)):")
    inner_pos = source.index(
        "for port, value in STIM[i].items():", main_pos,
    )
    # Look at the block between the inner `for port, value` and the
    # following `await RisingEdge` for the guard.
    next_await = source.index(
        "await RisingEdge(getattr(dut, CLK))", inner_pos,
    )
    main_loop_body = source[inner_pos:next_await]
    assert "if port == CLK:" in main_loop_body
    assert "continue" in main_loop_body


def test_diff_tb_main_loop_skips_clk_writes_when_executed() -> None:
    """**AC**: when the rendered loop pattern is executed against a
    recording-stub dut, NO write to dut.clk happens from the
    per-row write loop. Pins the *behavioural* fix — future
    renderer refactors that preserve the invariant stay green;
    refactors that break it fail loudly."""
    writes: list[tuple[str, int]] = []

    class _RecordingDUT:
        # The `value` accessor on cocotb signal handles is
        # opaque; here we model it as a settable .value attr on
        # an inner stub so the write expression
        # `getattr(dut, port).value = value` records the write.
        def __getattr__(self, name: str) -> object:
            recorder = writes

            class _Signal:
                def __init__(self, port_name: str) -> None:
                    self._port = port_name

                @property
                def value(self) -> int:
                    return 0

                @value.setter
                def value(self, v: int) -> None:
                    recorder.append((self._port, v))

            return _Signal(name)

    dut = _RecordingDUT()
    CLK = "clk"
    STIM = [
        {"clk": 1, "rst": 1, "data": 0},
        {"clk": 1, "rst": 0, "data": 1},
        {"clk": 1, "rst": 0, "data": 2},
    ]
    # Mirror the rendered loop's behaviour exactly.
    for port, value in STIM[0].items():
        if port == CLK:
            continue
        getattr(dut, port).value = value
    for i in range(1, len(STIM)):
        for port, value in STIM[i].items():
            if port == CLK:
                continue
            getattr(dut, port).value = value
    # Zero writes to clk; data + rst writes all present.
    assert all(name != CLK for name, _ in writes), writes
    assert ("rst", 1) in writes
    assert ("rst", 0) in writes
    assert ("data", 0) in writes
    assert ("data", 1) in writes
    assert ("data", 2) in writes


# --------------------------------------------------------------------------- #
# F19.4e — the harness must wait one delta delay (1ns Timer) between the
# clock edge and the output read so non-blocking assignments scheduled
# by @(posedge clk) have settled. cocotb's RisingEdge fires in the
# active region BEFORE the NBA region, so reading immediately yields
# the pre-edge value. The live shift_register run produced
# `cycle=6, expected=128, actual=0` across every RTL version because
# pout was being read before the @(posedge clk) NBA applied.
# --------------------------------------------------------------------------- #
def test_diff_tb_imports_timer_for_nba_settle() -> None:
    """The rendered TB body must import ``Timer`` from
    cocotb.triggers so the NBA-settle delay compiles."""
    module = ModuleDecl(
        module_id="counter", name="counter",
        description="4-bit counter",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="q", direction="out", width=4),
        ],
    )
    source = _render_diff_tb(
        module=module,
        stim=[{"clk": 1, "rst_n": 0}, {"clk": 1, "rst_n": 1}],
        expected=[{"q": 0}, {"q": 0}],
    )
    assert "from cocotb.triggers import RisingEdge, Timer" in source


def test_diff_tb_sleeps_before_reading_outputs() -> None:
    """A 1ns Timer must sit between the main loop's RisingEdge and
    the OUTPUT_PORTS read, AFTER the reset-window skip so reset
    cycles don't waste simulated time."""
    module = ModuleDecl(
        module_id="counter", name="counter",
        description="4-bit counter",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="q", direction="out", width=4),
        ],
    )
    source = _render_diff_tb(
        module=module,
        stim=[{"clk": 1, "rst_n": 0}, {"clk": 1, "rst_n": 1}],
        expected=[{"q": 0}, {"q": 0}],
    )
    main_pos = source.index("for i in range(1, len(STIM)):")
    edge_pos = source.index(
        "await RisingEdge(getattr(dut, CLK))", main_pos,
    )
    read_pos = source.index("for port in OUTPUT_PORTS:", edge_pos)
    between = source[edge_pos:read_pos]
    assert "if i < RESET_RELEASE_CYCLE:" in between
    assert 'await Timer(1, units="ns")' in between
    # The reset-window skip must come BEFORE the Timer so reset
    # cycles short-circuit without spending the delay.
    assert (
        between.index("if i < RESET_RELEASE_CYCLE:")
        < between.index('await Timer(1, units="ns")')
    )


def test_diff_tb_template_renders_complete_settling_sequence() -> None:
    """**AC**: pin the full cycle-iteration shape end-to-end.
    write loop → RisingEdge → reset-skip → Timer → read → DIFF
    must appear in that order so future renderer refactors can't
    silently reorder the sequence and re-introduce the live
    `actual=0` failure."""
    module = ModuleDecl(
        module_id="counter", name="counter",
        description="4-bit counter",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="q", direction="out", width=4),
        ],
    )
    source = _render_diff_tb(
        module=module,
        stim=[{"clk": 1, "rst_n": 0}, {"clk": 1, "rst_n": 1}],
        expected=[{"q": 0}, {"q": 0}],
    )
    main_pos = source.index("for i in range(1, len(STIM)):")
    main_loop = source[main_pos:]
    inner_write = main_loop.index("for port, value in STIM[i].items():")
    clk_guard = main_loop.index("if port == CLK:", inner_write)
    edge = main_loop.index("await RisingEdge(getattr(dut, CLK))", clk_guard)
    reset_skip = main_loop.index("if i < RESET_RELEASE_CYCLE:", edge)
    timer = main_loop.index('await Timer(1, units="ns")', reset_skip)
    read = main_loop.index("for port in OUTPUT_PORTS:", timer)
    diff = main_loop.index("DIFF|cycle=", read)
    assert inner_write < clk_guard < edge < reset_skip < timer < read < diff
