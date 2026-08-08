"""OracleVerificationGate (F19.6).

Sandboxed verifier that runs an F19.4 :class:`OracleArtifact`'s
reference function against an F19.5 :class:`AssertionSpec`'s assertion
callables on a caller-supplied stimulus, and emits an
:class:`OracleVerificationArtifact` recording whether the two
independently-derived interpretations of the same contract agreed.

The gate is purely mechanical — no model, no prompt, no escalation.
It is content-addressed: two runs of the same
``(oracle, assertion_spec, stim)`` triple produce the same artifact
body. In Phase 1 of M19 the gate's verdict (``gate_ok``) is
informational; F19.10 promotes it to a hard gate.

The subprocess is plain Python in the host venv — NOT Docker. Per the
F19.6 description, oracle / assertion code is trusted project-internal
content; production sandboxing for adversarial inputs is out of scope.
The runner has a configurable timeout (default 60s) so a runaway
oracle can be killed cleanly.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from chip_agent.design_state import (
    ArtifactRef,
    AssertionSpec,
    ModuleDecl,
    OracleArtifact,
    OracleVerificationArtifact,
    Provenance,
    Stage,
    ToolVersion,
    Violation,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.sandbox import ProcessRunner, SubprocessProcessRunner

__all__ = ["OracleVerificationError", "OracleVerificationGate"]


# Path to the static runner module we copy into the work dir before
# invoking the subprocess. Lives next to this module so import-time
# discovery is path-based, no resource lookup needed.
_RUNNER_SOURCE_PATH = Path(__file__).parent / "_oracle_runner.py"


class OracleVerificationError(ValueError):
    """The gate was misconfigured (design/module mismatch)."""


class OracleVerificationGate:
    """ContractArtifact-derived oracle + assertions -> OracleVerificationArtifact.

    Construction injects a :class:`ProcessRunner` so tests can swap a
    :class:`StubProcessRunner` for timeout/runner-error/malformed paths.
    Production uses the default :class:`SubprocessProcessRunner`.
    """

    TOOL_VERSION: ClassVar[str] = "f19.6"

    def __init__(
        self,
        *,
        store: SqliteArtifactStore,
        design_id: str,
        runner: ProcessRunner | None = None,
        timeout_s: int = 60,
        agent_name: str = "oracle_verifier",
    ) -> None:
        if not design_id:
            raise OracleVerificationError("design_id must be non-empty")
        self.store = store
        self.design_id = design_id
        self.runner: ProcessRunner = runner if runner is not None else SubprocessProcessRunner()
        self.timeout_s = timeout_s
        self.agent_name = agent_name

    def verify(
        self,
        oracle: OracleArtifact,
        assertion_spec: AssertionSpec,
        module: ModuleDecl,
        *,
        stim: list[dict[str, int]],
        spec_ref: ArtifactRef | None = None,
    ) -> OracleVerificationArtifact:
        """Run the oracle + assertions in a subprocess; persist the verdict.

        Never raises on assertion failure — the returned artifact's
        ``gate_ok`` is the verdict. Raises :class:`OracleVerificationError`
        only on configuration mismatches (design_id / module_id).
        """
        self._validate_inputs(oracle, assertion_spec, module)

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

        with tempfile.TemporaryDirectory(prefix="chip-agent-oracle-") as tmp:
            work_dir = Path(tmp)
            (work_dir / "oracle.py").write_bytes(oracle_src)
            (work_dir / "assertions.py").write_bytes(assertion_src)
            (work_dir / "input.json").write_text(json.dumps(input_payload))
            shutil.copy(_RUNNER_SOURCE_PATH, work_dir / "runner.py")

            argv = [
                sys.executable,
                str(work_dir / "runner.py"),
                str(work_dir),
            ]
            proc = self.runner.run(argv, timeout=self.timeout_s)

            runner_output = _read_runner_output(work_dir, proc)

        violations, passed, runner_stats = _interpret_runner_output(
            proc=proc, runner_output=runner_output, callsites=callsites,
        )

        inputs: list[ArtifactRef] = [oracle.ref(), assertion_spec.ref()]
        if spec_ref is not None:
            inputs.append(spec_ref)

        artifact = OracleVerificationArtifact(
            artifact_id=f"{self.design_id}.{module.module_id}.oracle_verification",
            design_id=self.design_id,
            module_id=module.module_id,
            passed=passed,
            checker=ToolVersion(name="oracle_verifier", version=self.TOOL_VERSION),
            metrics={
                "stim_cycles": float(runner_stats["stim_cycles"]),
                "assertions_checked": float(runner_stats["assertions_checked"]),
                "assertions_passed": float(runner_stats["assertions_passed"]),
                "duration_s": runner_stats["duration_s"],
            },
            violations=violations,
            oracle_ref=oracle.ref(),
            assertion_spec_ref=assertion_spec.ref(),
            stim_cycles=runner_stats["stim_cycles"],
            assertions_checked=runner_stats["assertions_checked"],
            assertions_passed=runner_stats["assertions_passed"],
            provenance=Provenance(
                produced_by=Stage.PLAN,
                agent=self.agent_name,
                tool=ToolVersion(name="oracle_verifier", version=self.TOOL_VERSION),
                inputs=inputs,
            ),
        )
        self.store.put(artifact)
        loaded = self.store.get_by_id(artifact.artifact_id)
        assert isinstance(loaded, OracleVerificationArtifact)
        return loaded

    # ------------------------------------------------------------- helpers
    def _validate_inputs(
        self,
        oracle: OracleArtifact,
        assertion_spec: AssertionSpec,
        module: ModuleDecl,
    ) -> None:
        if oracle.design_id != self.design_id:
            raise OracleVerificationError(
                f"oracle.design_id {oracle.design_id!r} does not match "
                f"gate.design_id {self.design_id!r}"
            )
        if assertion_spec.design_id != self.design_id:
            raise OracleVerificationError(
                f"assertion_spec.design_id {assertion_spec.design_id!r} does not "
                f"match gate.design_id {self.design_id!r}"
            )
        if oracle.module_id != module.module_id:
            raise OracleVerificationError(
                f"oracle.module_id {oracle.module_id!r} does not match "
                f"module.module_id {module.module_id!r}"
            )
        if assertion_spec.module_id != module.module_id:
            raise OracleVerificationError(
                f"assertion_spec.module_id {assertion_spec.module_id!r} does not "
                f"match module.module_id {module.module_id!r}"
            )


# --------------------------------------------------------------------------- #
# Runner-output interpretation
# --------------------------------------------------------------------------- #
def _read_runner_output(
    work_dir: Path,
    proc: Any,
) -> dict[str, Any] | None:
    """Read ``result.json`` from the work dir; return None if missing
    or unparseable (the caller emits MALFORMED_RESULT)."""
    if proc.timed_out or proc.returncode != 0:
        # No point trying to parse result.json on a runner-level failure.
        return None
    result_path = work_dir / "result.json"
    if not result_path.exists():
        return None
    try:
        parsed = json.loads(result_path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _interpret_runner_output(
    *,
    proc: Any,
    runner_output: dict[str, Any] | None,
    callsites: list[dict[str, str]],
) -> tuple[list[Violation], bool, dict[str, Any]]:
    """Convert subprocess outcome + runner JSON into Violations + a
    pass/fail verdict + summary stats. Never raises."""
    violations: list[Violation] = []
    stats: dict[str, Any] = {
        "stim_cycles": 0,
        "assertions_checked": 0,
        "assertions_passed": 0,
        "duration_s": 0.0,
    }

    if proc.timed_out:
        violations.append(Violation(
            code="ORACLE.TIMEOUT",
            severity="error",
            message=f"subprocess timed out after {proc.returncode}s (rc={proc.returncode})",
            detail={"stderr": proc.stderr},
        ))
        return violations, False, stats

    if proc.returncode != 0:
        violations.append(Violation(
            code="ORACLE.RUNNER_ERROR",
            severity="error",
            message=f"runner exited with returncode {proc.returncode}",
            detail={"stdout": proc.stdout, "stderr": proc.stderr},
        ))
        return violations, False, stats

    if runner_output is None:
        violations.append(Violation(
            code="ORACLE.MALFORMED_RESULT",
            severity="error",
            message="runner produced no parseable result.json",
            detail={"stdout": proc.stdout, "stderr": proc.stderr},
        ))
        return violations, False, stats

    # Happy path: subprocess produced a parseable result.
    stats["stim_cycles"] = int(runner_output.get("stim_cycles", 0))
    stats["assertions_checked"] = int(runner_output.get("assertions_checked", 0))
    stats["assertions_passed"] = int(runner_output.get("assertions_passed", 0))
    stats["duration_s"] = float(runner_output.get("duration_s", 0.0))

    for result in runner_output.get("results", []):
        if result.get("passed"):
            continue
        detail: dict[str, Any] = {
            "name": result.get("name"),
            "callsite": result.get("callsite"),
            "assertion_message": result.get("message", ""),
        }
        if "traceback" in result:
            detail["traceback"] = result["traceback"]
        violations.append(Violation(
            code="ORACLE.ASSERTION_DISAGREEMENT",
            severity="error",
            message=f"{result.get('name', '<unknown>')}: {result.get('message', '')}",
            location=str(result.get("callsite", "")),
            detail=detail,
        ))

    passed = (
        not violations
        and stats["assertions_checked"] == len(callsites)
        and stats["assertions_passed"] == stats["assertions_checked"]
    )
    return violations, passed, stats
