"""DifferentialTBBuilder (F19.8).

Deterministic, no-router builder that replaces the LLM-generated
testbench when an :class:`OracleArtifact` (F19.4) and an
:class:`AssertionSpec` (F19.5) are available on a module's per-stage
head. The builder runs the oracle once in a subprocess (same seam the
F19.6 :class:`OracleVerificationGate` uses), captures the per-cycle
``observed`` outputs, and embeds ``(STIM, EXPECTED)`` into a cocotb
testbench source string. On every cycle past the reset-release window
the rendered TB asserts ``int(getattr(dut, port).value) ==
EXPECTED[i][port]`` for every output port; on mismatch the assertion
message is the canonical token shape
``DIFF|cycle=N|signal=S|expected=E|actual=A`` so
:func:`chip_agent.tools.trace.parse_failing_assertion` extracts
structured fields without fuzzy regex.

The builder is content-addressed via the persisted
:class:`TestbenchArtifact`: two runs of the same
``(oracle, assertion_spec, stim)`` triple produce the same TB body
and thus the same content hash. The provenance.inputs list pins
``[oracle.ref(), assertion_spec.ref(), plan.ref()]`` so the artifact
lineage walks backward through the M19 Phase 1 stages.
"""

from __future__ import annotations

import json
import pprint
import shutil
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

from chip_agent.design_state import (
    ArtifactRef,
    AssertionSpec,
    DesignPlan,
    ModuleDecl,
    OracleArtifact,
    Port,
    Provenance,
    Stage,
    TestbenchArtifact,
    ToolVersion,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.sandbox import ProcessRunner, SubprocessProcessRunner

__all__ = [
    "DifferentialTBBuilder",
    "DifferentialTBError",
    "completion_port_never_asserted",
]


# Path to the static runner module we copy into the work dir before
# invoking the subprocess. Lives next to this module; the F19.6 gate
# uses the same file (see oracle_verification.py).
_RUNNER_SOURCE_PATH = Path(__file__).parent / "_oracle_runner.py"

# Lowercase port-name sets used to detect clk + reset polarity. Match
# stim_ramp.py so heuristics stay aligned across the two helpers.
_CLOCK_NAMES = frozenset({"clk", "clock"})
_ACTIVE_LOW_RESET_NAMES = frozenset({"rst_n", "rstn", "reset_n", "resetn"})
_ACTIVE_HIGH_RESET_NAMES = frozenset({"rst", "reset"})

# F23.6: 1-bit output ports whose name marks "the operation finished /
# result is valid". If the oracle never drives one high across the whole
# EXPECTED trace, the stimulus window never let the design finish, so the
# differential pass is vacuous.
_COMPLETION_PORT_NAMES = frozenset(
    {"done", "valid", "complete", "finish", "finished", "eot", "ack", "ready"},
)
# A window shorter than this can be legitimately pre-completion; only flag
# vacuity once the window is long enough that "never finished" is suspicious.
_MIN_WINDOW_FOR_VACUITY = 4


def completion_port_never_asserted(
    module: ModuleDecl, observed: list[dict[str, int]],
) -> str | None:
    """Name of a completion output port the oracle never asserts, else None.

    Deterministic, pure. Targets the ``proj_diff_tb_window_too_short`` class:
    a design that declares a ``done``/``valid``/… output which the oracle's
    EXPECTED trace holds at 0 for the entire (non-trivial) window — the
    tell-tale of a stimulus too short to observe completion.
    """
    if len(observed) < _MIN_WINDOW_FOR_VACUITY:
        return None
    for port in module.ports:
        if (
            port.direction == "out"
            and port.width == 1
            and port.name.lower() in _COMPLETION_PORT_NAMES
            and all((cyc.get(port.name, 0) or 0) == 0 for cyc in observed)
        ):
            return port.name
    return None


class DifferentialTBError(ValueError):
    """The builder was misconfigured or the oracle subprocess failed."""


class DifferentialTBBuilder:
    """OracleArtifact + AssertionSpec -> differential cocotb TestbenchArtifact.

    Construction injects a :class:`ProcessRunner` so tests can swap a
    :class:`StubProcessRunner` for the subprocess that runs the
    oracle — mirrors the F19.6 :class:`OracleVerificationGate.__init__`
    pattern.
    """

    TOOL_VERSION: ClassVar[str] = "f19.8"

    def __init__(
        self,
        *,
        store: SqliteArtifactStore,
        design_id: str,
        runner: ProcessRunner | None = None,
        timeout_s: int = 60,
        agent_name: str = "differential_tb_builder",
    ) -> None:
        if not design_id:
            raise DifferentialTBError("design_id must be non-empty")
        self.store = store
        self.design_id = design_id
        self.runner: ProcessRunner = (
            runner if runner is not None else SubprocessProcessRunner()
        )
        self.timeout_s = timeout_s
        self.agent_name = agent_name

    def build(
        self,
        module: ModuleDecl,
        oracle: OracleArtifact,
        assertion_spec: AssertionSpec,
        *,
        stim: list[dict[str, int]],
        plan: DesignPlan,
        spec_ref: ArtifactRef | None = None,
    ) -> TestbenchArtifact:
        """Run the oracle, render the differential TB, persist it."""
        self._validate_inputs(oracle, assertion_spec, module, plan)

        observed = self._run_oracle(oracle, assertion_spec, stim)

        if len(observed) != len(stim):
            raise DifferentialTBError(
                f"oracle returned {len(observed)} observed cycles but stim "
                f"has {len(stim)}; refusing to render a misaligned TB",
            )

        source = _render_diff_tb(
            module=module, stim=stim, expected=observed,
        )

        # F23.6 gate-honesty: flag a vacuous window — a completion output
        # port the oracle never asserts across the whole EXPECTED trace means
        # the stimulus is too short to observe the design finishing, so a
        # cycle-by-cycle "pass" proves nothing (the present80 landmine, see
        # docs/KNOWN_ISSUES.md 2026-08-08).
        vacuous_port = completion_port_never_asserted(module, observed)

        return self._persist(
            module=module, source=source, oracle=oracle,
            assertion_spec=assertion_spec, plan=plan, spec_ref=spec_ref,
            vacuous_completion_port=vacuous_port,
        )

    # ------------------------------------------------------------- helpers
    def _validate_inputs(
        self,
        oracle: OracleArtifact,
        assertion_spec: AssertionSpec,
        module: ModuleDecl,
        plan: DesignPlan,
    ) -> None:
        if oracle.design_id != self.design_id:
            raise DifferentialTBError(
                f"oracle.design_id {oracle.design_id!r} does not match "
                f"builder.design_id {self.design_id!r}",
            )
        if assertion_spec.design_id != self.design_id:
            raise DifferentialTBError(
                f"assertion_spec.design_id {assertion_spec.design_id!r} does "
                f"not match builder.design_id {self.design_id!r}",
            )
        if plan.design_id != self.design_id:
            raise DifferentialTBError(
                f"plan.design_id {plan.design_id!r} does not match "
                f"builder.design_id {self.design_id!r}",
            )
        if oracle.module_id != module.module_id:
            raise DifferentialTBError(
                f"oracle.module_id {oracle.module_id!r} does not match "
                f"module.module_id {module.module_id!r}",
            )
        if assertion_spec.module_id != module.module_id:
            raise DifferentialTBError(
                f"assertion_spec.module_id {assertion_spec.module_id!r} "
                f"does not match module.module_id {module.module_id!r}",
            )

    def _run_oracle(
        self,
        oracle: OracleArtifact,
        assertion_spec: AssertionSpec,
        stim: list[dict[str, int]],
    ) -> list[dict[str, int]]:
        """Drive ``_oracle_runner.py`` and return the ``observed`` list.

        Raises :class:`DifferentialTBError` on timeout / nonzero exit
        / malformed result.json so the RTL outer loop sees a single,
        unambiguous failure mode.
        """
        oracle_src = self.store.get_blob(oracle.source)
        assertion_src = self.store.get_blob(assertion_spec.source)
        callsites = [
            {"name": inv.name, "callsite": inv.callsite}
            for inv in assertion_spec.assertions
        ]
        input_payload = {
            "reference_fn_name": oracle.reference_fn_name,
            "stim": stim,
            "callsites": callsites,
        }
        with tempfile.TemporaryDirectory(prefix="chip-agent-diff-") as tmp:
            work_dir = Path(tmp)
            (work_dir / "oracle.py").write_bytes(oracle_src)
            (work_dir / "assertions.py").write_bytes(assertion_src)
            (work_dir / "input.json").write_text(json.dumps(input_payload))
            shutil.copy(_RUNNER_SOURCE_PATH, work_dir / "runner.py")

            argv = [
                sys.executable, str(work_dir / "runner.py"), str(work_dir),
            ]
            proc = self.runner.run(argv, timeout=self.timeout_s)

            if proc.timed_out:
                raise DifferentialTBError(
                    f"oracle subprocess timed out after {self.timeout_s}s; "
                    f"stderr={proc.stderr!r}",
                )
            if proc.returncode != 0:
                raise DifferentialTBError(
                    f"oracle subprocess exited with returncode "
                    f"{proc.returncode}; stderr={proc.stderr!r}",
                )

            result_path = work_dir / "result.json"
            if not result_path.exists():
                raise DifferentialTBError(
                    "oracle subprocess produced no result.json; "
                    f"stderr={proc.stderr!r}",
                )
            try:
                payload = json.loads(result_path.read_text())
            except json.JSONDecodeError as e:
                raise DifferentialTBError(
                    f"oracle result.json is not valid JSON: {e}",
                ) from e

        observed = payload.get("observed")
        if not isinstance(observed, list):
            raise DifferentialTBError(
                "oracle result.json is missing the 'observed' key (F19.8 "
                "requires the runner's F19.8-extended payload); rerun "
                "after updating _oracle_runner.py",
            )
        for entry in observed:
            if not isinstance(entry, dict):
                raise DifferentialTBError(
                    f"oracle 'observed' entry must be a dict, got "
                    f"{type(entry).__name__}",
                )
        return observed

    def _persist(
        self,
        *,
        module: ModuleDecl,
        source: str,
        oracle: OracleArtifact,
        assertion_spec: AssertionSpec,
        plan: DesignPlan,
        spec_ref: ArtifactRef | None,
        vacuous_completion_port: str | None = None,
    ) -> TestbenchArtifact:
        normalised = source.rstrip("\n") + "\n"
        blob = self.store.put_blob(
            normalised.encode("utf-8"), media_type="text/x-python",
        )
        inputs: list[ArtifactRef] = [
            oracle.ref(), assertion_spec.ref(), plan.ref(),
        ]
        if spec_ref is not None:
            inputs.append(spec_ref)
        # F23.6: the vacuous-window flag rides on metadata (non-content, so
        # it never perturbs the TB's identity hash); the RTL node reads it to
        # decide whether a green sim is trustworthy.
        metadata = (
            {"vacuous_completion_port": vacuous_completion_port}
            if vacuous_completion_port is not None
            else {}
        )
        tb = TestbenchArtifact(
            artifact_id=f"{self.design_id}.{module.module_id}.tb",
            design_id=self.design_id,
            module_id=module.module_id,
            framework="cocotb",
            target_module=module.name,
            source=blob,
            metadata=metadata,
            provenance=Provenance(
                produced_by=Stage.RTL,
                agent=self.agent_name,
                tool=ToolVersion(
                    name="differential_tb", version=self.TOOL_VERSION,
                ),
                inputs=inputs,
            ),
        )
        self.store.put(tb)
        loaded = self.store.get_by_id(tb.artifact_id)
        assert isinstance(loaded, TestbenchArtifact)
        return loaded


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #
_DIFF_TB_TEMPLATE = '''\
"""F19.8 differential cocotb testbench for ``{top}``.

Stim + expected outputs are embedded from the OracleArtifact at build
time; the DUT is compared cycle-by-cycle against the oracle's reference
outputs. On any mismatch the test fails with the canonical token
``DIFF|cycle=N|signal=S|expected=E|actual=A`` so
:func:`chip_agent.tools.trace.parse_failing_assertion` extracts
structured fields without fuzzy regex.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

STIM = {stim_literal}
EXPECTED = {expected_literal}
RESET_RELEASE_CYCLE = {reset_release_cycle}
CLK = {clk!r}
OUTPUT_PORTS = {output_ports_literal!r}


@cocotb.test()
async def test_{test_id}_diff(dut):
    """Drive STIM through the DUT; assert outputs match EXPECTED.

    F19.4d: ``clk`` is driven solely by the Clock() coroutine. The
    per-row write loops skip the CLK port to avoid contention with
    the clock driver (writing dut.clk=1 every row stalls the
    rising-edge generator, so @(posedge clk) never fires).

    F19.4e: a 1ns Timer delay between the rising edge and the
    output read lets NBAs from @(posedge clk) settle, so reads
    observe the post-edge value rather than the pre-edge one.
    (RisingEdge fires in the active region BEFORE the non-blocking
    assignment region settles.)
    """
    cocotb.start_soon(Clock(getattr(dut, CLK), 10, units="ns").start())

    for port, value in STIM[0].items():
        if port == CLK:
            continue
        getattr(dut, port).value = value
    await RisingEdge(getattr(dut, CLK))

    for i in range(1, len(STIM)):
        for port, value in STIM[i].items():
            if port == CLK:
                continue
            getattr(dut, port).value = value
        await RisingEdge(getattr(dut, CLK))

        if i < RESET_RELEASE_CYCLE:
            continue
        await Timer(1, units="ns")
        for port in OUTPUT_PORTS:
            expected = EXPECTED[i].get(port)
            if expected is None:
                # Oracle did not pin this output at this cycle; skip
                # so we don't surface a spurious DIFF on an unmodeled
                # signal.
                continue
            actual = int(getattr(dut, port).value)
            assert actual == expected, (
                f"DIFF|cycle={{i}}|signal={{port}}|"
                f"expected={{expected}}|actual={{actual}}"
            )
'''


def _render_diff_tb(
    *,
    module: ModuleDecl,
    stim: list[dict[str, int]],
    expected: list[dict[str, int]],
) -> str:
    """Render the cocotb TB source for ``module`` from ``(stim, expected)``."""
    clk = _find_clock_port(module.ports)
    if clk is None:
        raise DifferentialTBError(
            f"module {module.module_id!r} has no clock-like input port; "
            f"differential TB needs one to drive cocotb.Clock",
        )
    output_ports = [p.name for p in module.ports if p.direction == "out"]
    if not output_ports:
        raise DifferentialTBError(
            f"module {module.module_id!r} has no output ports; nothing "
            f"for the differential TB to compare",
        )
    reset_release_cycle = _compute_reset_release_cycle(module, stim)
    test_id = _safe_identifier(module.name)
    return _DIFF_TB_TEMPLATE.format(
        top=module.name,
        clk=clk,
        test_id=test_id,
        output_ports_literal=output_ports,
        reset_release_cycle=reset_release_cycle,
        stim_literal=pprint.pformat(stim, width=88, sort_dicts=False),
        expected_literal=pprint.pformat(expected, width=88, sort_dicts=False),
    )


def _find_clock_port(ports: list[Port]) -> str | None:
    for p in ports:
        if p.direction == "in" and p.name.lower() in _CLOCK_NAMES:
            return p.name
    return None


def _compute_reset_release_cycle(
    module: ModuleDecl, stim: list[dict[str, int]],
) -> int:
    """First cycle index where the reset port is deasserted.

    Defaults to ``2`` when no reset port matches the heuristic name
    sets — same convention as ``build_ramp_stim``'s "first two cycles
    are setup". The comparison loop starts at this index.
    """
    rst_name: str | None = None
    rst_active: int | None = None
    for p in module.ports:
        if p.direction != "in":
            continue
        name_lower = p.name.lower()
        if name_lower in _ACTIVE_LOW_RESET_NAMES:
            rst_name, rst_active = p.name, 0
            break
        if name_lower in _ACTIVE_HIGH_RESET_NAMES:
            rst_name, rst_active = p.name, 1
            break
    if rst_name is None or rst_active is None:
        return 2
    for i, snapshot in enumerate(stim):
        if snapshot.get(rst_name) != rst_active:
            return i
    return len(stim)  # reset never deasserts — compare nothing


def _safe_identifier(name: str) -> str:
    """Convert ``name`` to a Python identifier for the test function.

    cocotb's test function name is mostly cosmetic — only the
    ``@cocotb.test()`` decorator matters — but we keep it readable.
    """
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)
