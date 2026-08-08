"""F19.6 acceptance: OracleVerificationGate runs the F19.4 oracle's
reference function against the F19.5 assertion callables in a
sandboxed subprocess and emits OracleVerificationArtifact recording
agreement / disagreement.

Tests use a mix of:
* real SubprocessProcessRunner (happy path + AC) — exercises the
  static runner script end-to-end against actual Python source.
* StubProcessRunner with canned ProcessResult — exercises the
  timeout / runner-error / malformed-result paths without flakiness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.agents.oracle_verification import (
    OracleVerificationError,
    OracleVerificationGate,
)
from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    AssertionSpec,
    ModuleDecl,
    OracleArtifact,
    OracleVerificationArtifact,
    Port,
    Provenance,
    Stage,
    StructuredInvariant,
)
from chip_agent.store import SqliteArtifactStore
from chip_agent.tools.sandbox import ProcessResult


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
        content_dir=tmp_path / "runs",
    )
    yield s
    s.close()


def _module_counter() -> ModuleDecl:
    return ModuleDecl(
        module_id="counter",
        name="counter",
        description="8-bit synchronous up-counter, async active-low reset",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="en", direction="in", width=1),
            Port(name="q", direction="out", width=8),
        ],
    )


# The canonical 5-cycle counter stim exercises reset + en-low hold +
# three increment cycles, enough to trip increment_by_one when q
# advances by 2 instead of 1.
_COUNTER_STIM = [
    {"rst_n": 0, "en": 0},  # reset asserted -> q=0
    {"rst_n": 1, "en": 0},  # released, en low -> q hold
    {"rst_n": 1, "en": 1},  # en on -> q increments
    {"rst_n": 1, "en": 1},
    {"rst_n": 1, "en": 1},
]


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


# F19.6 AC fixture: a counter that increments by 2. The increment
# assertion must reject; the reset assertion must still pass.
_BROKEN_COUNTER_ORACLE_PY = """\
def reference(stim):
    q = 0
    out = []
    for cyc in stim:
        if cyc.get("rst_n", 1) == 0:
            q = 0
        elif cyc.get("en", 0) == 1:
            q = (q + 2) % 256   # BUG: should be + 1
        out.append({"q": q})
    return out
"""


_COUNTER_ASSERTIONS_PY = """\
def assert_reset_clears_count(args):
    \"\"\"When rst_n low, q must be 0.\"\"\"
    stim, observed = args
    for s, o in zip(stim, observed):
        if s.get("rst_n", 1) == 0 and o.get("q", 0) != 0:
            return (False, f"rst_n low but q={o['q']}")
    return (True, "reset always clears q")


def assert_increment_by_one(args):
    \"\"\"With rst_n high and en high, q advances by 1.\"\"\"
    stim, observed = args
    prev_q = None
    for s, o in zip(stim, observed):
        if (s.get("rst_n", 1) == 1 and s.get("en", 0) == 1
                and prev_q is not None):
            expected = (prev_q + 1) % 256
            if o.get("q", 0) != expected:
                return (False, f"expected q={expected}, got {o['q']}")
        prev_q = o.get("q", 0)
    return (True, "increment behaviour holds")


def assert_count_wraps_at_2_to_n(args):
    \"\"\"q must wrap from 255 to 0 on the next enabled cycle.\"\"\"
    stim, observed = args
    prev_q = None
    for s, o in zip(stim, observed):
        if (prev_q == 255 and s.get("en", 0) == 1
                and s.get("rst_n", 1) == 1
                and o.get("q", 0) != 0):
            return (False, f"q was 255 but next q was {o['q']}")
        prev_q = o.get("q", 0)
    return (True, "wrap behaviour holds")
"""


_RAISING_ASSERTIONS_PY = """\
def assert_reset_clears_count(args):
    stim, observed = args
    raise KeyError("synthetic error from assertion")
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
    return store.get_by_id(oracle.artifact_id)  # type: ignore[return-value]


def _make_assertions(
    store: SqliteArtifactStore,
    *,
    source: str = _COUNTER_ASSERTIONS_PY,
    invariants: list[StructuredInvariant] | None = None,
    design_id: str = "d0",
    module_id: str = "counter",
) -> AssertionSpec:
    if invariants is None:
        invariants = [
            StructuredInvariant(
                name="reset_clears_count",
                callsite="assert_reset_clears_count",
                description="When rst_n low, q is 0.",
            ),
            StructuredInvariant(
                name="increment_by_one",
                callsite="assert_increment_by_one",
                description="q advances by 1 per enabled cycle.",
            ),
            StructuredInvariant(
                name="count_wraps_at_2_to_n",
                callsite="assert_count_wraps_at_2_to_n",
                description="q wraps at 255.",
            ),
        ]
    blob = store.put_blob(source.encode("utf-8"), media_type="text/x-python")
    spec = AssertionSpec(
        artifact_id=f"{design_id}.{module_id}.assertions",
        design_id=design_id,
        module_id=module_id,
        source=blob,
        assertions=invariants,
        provenance=Provenance(produced_by=Stage.PLAN, agent="assertion_gen"),
    )
    store.put(spec)
    return store.get_by_id(spec.artifact_id)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Tests — happy path / AC (real subprocess)
# --------------------------------------------------------------------------- #
def test_verify_returns_oracle_verification_artifact(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    gate = OracleVerificationGate(store=store, design_id="d0")
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )

    assert isinstance(result, OracleVerificationArtifact)
    assert result.kind is ArtifactKind.ORACLE_VERIFICATION
    assert result.module_id == "counter"
    assert result.design_id == "d0"
    assert result.artifact_id == "d0.counter.oracle_verification"
    assert result.oracle_ref == oracle.ref()
    assert result.assertion_spec_ref == assertions.ref()


def test_verify_correct_oracle_passes_with_no_violations(
    store: SqliteArtifactStore,
) -> None:
    """The happy path: a correct counter oracle + correct assertions
    on a stim that exercises reset + hold + increment → all assertions
    pass, gate_ok=True."""
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    gate = OracleVerificationGate(store=store, design_id="d0")
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )

    assert result.passed is True
    assert result.gate_ok is True
    assert result.violations == []
    assert result.assertions_checked == 3
    assert result.assertions_passed == 3


def test_verify_broken_oracle_fails_with_increment_disagreement(
    store: SqliteArtifactStore,
) -> None:
    """F19.6 AC: a manually-broken oracle (counter that increments
    by 2 instead of 1) fails the assertion-gate with a violation
    naming the increment_by_one assertion; the reset assertion
    still passes (gate_ok=False but the agreement is partial)."""
    oracle = _make_oracle(store, source=_BROKEN_COUNTER_ORACLE_PY)
    assertions = _make_assertions(store)
    gate = OracleVerificationGate(store=store, design_id="d0")
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )

    assert result.passed is False
    assert result.gate_ok is False
    disagreements = [
        v for v in result.violations
        if v.code == "ORACLE.ASSERTION_DISAGREEMENT"
    ]
    assert len(disagreements) >= 1
    # The violation must NAME the failing assertion in `location`.
    names_that_failed = {v.detail.get("name") for v in disagreements}
    assert "increment_by_one" in names_that_failed
    callsites_that_failed = {v.location for v in disagreements}
    assert "assert_increment_by_one" in callsites_that_failed
    # Reset assertion still holds against the broken oracle (q stays
    # at 0 when rst_n is low).
    assert result.assertions_passed >= 1
    assert result.assertions_passed < result.assertions_checked


def test_verify_records_stim_cycle_count(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    gate = OracleVerificationGate(store=store, design_id="d0")
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )
    assert result.stim_cycles == len(_COUNTER_STIM)


def test_verify_records_oracle_and_assertion_refs_in_provenance(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    gate = OracleVerificationGate(store=store, design_id="d0")

    # Without spec_ref: only oracle + assertions.
    result_no_spec = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )
    assert result_no_spec.provenance.inputs == [oracle.ref(), assertions.ref()]

    # With spec_ref: appended in that order.
    spec_ref = ArtifactRef(
        artifact_id="d0.spec", version=1, kind=ArtifactKind.SPEC,
        content_hash="sha256:" + "c" * 64,
    )
    result_with_spec = gate.verify(
        oracle, assertions, _module_counter(),
        stim=_COUNTER_STIM, spec_ref=spec_ref,
    )
    assert result_with_spec.provenance.inputs == [
        oracle.ref(), assertions.ref(), spec_ref,
    ]


def test_verify_persists_artifact_to_store(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    gate = OracleVerificationGate(store=store, design_id="d0")
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )

    refetched = store.get_by_id(result.artifact_id)
    assert isinstance(refetched, OracleVerificationArtifact)
    assert refetched.content_hash == result.content_hash
    assert refetched.oracle_ref == oracle.ref()
    assert refetched.assertions_passed == 3


def test_verify_handles_assertion_raising_exception(
    store: SqliteArtifactStore,
) -> None:
    """An assertion function that raises an exception must not crash
    the subprocess. The runner catches it and reports passed=False
    with a traceback in detail."""
    oracle = _make_oracle(store)
    raising_invariants = [
        StructuredInvariant(
            name="reset_clears_count",
            callsite="assert_reset_clears_count",
            description="",
        ),
    ]
    assertions = _make_assertions(
        store, source=_RAISING_ASSERTIONS_PY, invariants=raising_invariants,
    )
    gate = OracleVerificationGate(store=store, design_id="d0")
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )

    assert result.passed is False
    disagreements = [
        v for v in result.violations
        if v.code == "ORACLE.ASSERTION_DISAGREEMENT"
    ]
    assert len(disagreements) == 1
    v = disagreements[0]
    assert "KeyError" in v.message
    assert "traceback" in v.detail
    assert "KeyError" in v.detail["traceback"]


# --------------------------------------------------------------------------- #
# Tests — runner-level failure modes (stub subprocess)
# --------------------------------------------------------------------------- #
def test_verify_subprocess_timeout_emits_timeout_violation(
    store: SqliteArtifactStore,
) -> None:
    """A runaway oracle that hits the timeout produces a single
    ORACLE.TIMEOUT violation. The artifact is still persisted so the
    failure is auditable."""
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    stub = StubProcessRunner(result=ProcessResult(
        returncode=124, stdout="", stderr="[sandbox] timed out after 1s",
        timed_out=True,
    ))
    gate = OracleVerificationGate(
        store=store, design_id="d0", runner=stub, timeout_s=1,
    )
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )

    assert result.passed is False
    assert result.gate_ok is False
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.code == "ORACLE.TIMEOUT"
    assert v.severity == "error"
    # No assertion-level violations on timeout — subprocess never ran them.
    assert all(
        v.code == "ORACLE.TIMEOUT" for v in result.violations
    )
    # Stats stay at 0 — the runner never produced result.json.
    assert result.assertions_checked == 0


def test_verify_subprocess_nonzero_exit_emits_runner_error(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    stub = StubProcessRunner(result=ProcessResult(
        returncode=1, stdout="", stderr="SyntaxError: bad oracle",
        timed_out=False,
    ))
    gate = OracleVerificationGate(store=store, design_id="d0", runner=stub)
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )

    assert result.passed is False
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.code == "ORACLE.RUNNER_ERROR"
    assert "SyntaxError" in v.detail.get("stderr", "")


def test_verify_malformed_runner_output_emits_violation(
    store: SqliteArtifactStore,
) -> None:
    """Subprocess exited cleanly but produced no result.json — likely
    a runner-script regression. Surface as a distinct violation
    code so the failure mode is diagnosable."""
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    # Stub: rc=0, but the stub never writes result.json into the work
    # dir (no real subprocess fired), so the gate's parser sees no file.
    stub = StubProcessRunner(result=ProcessResult(
        returncode=0, stdout="", stderr="", timed_out=False,
    ))
    gate = OracleVerificationGate(store=store, design_id="d0", runner=stub)
    result = gate.verify(
        oracle, assertions, _module_counter(), stim=_COUNTER_STIM,
    )

    assert result.passed is False
    assert len(result.violations) == 1
    assert result.violations[0].code == "ORACLE.MALFORMED_RESULT"


# --------------------------------------------------------------------------- #
# Tests — configuration / error paths
# --------------------------------------------------------------------------- #
def test_verify_raises_on_design_id_mismatch(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    gate = OracleVerificationGate(store=store, design_id="other_design")
    with pytest.raises(OracleVerificationError, match="design_id"):
        gate.verify(oracle, assertions, _module_counter(), stim=_COUNTER_STIM)


def test_verify_raises_on_oracle_module_mismatch(
    store: SqliteArtifactStore,
) -> None:
    oracle = _make_oracle(store)
    assertions = _make_assertions(store)
    other_module = ModuleDecl(
        module_id="not_counter", name="not_counter",
        description="something else",
        ports=[Port(name="clk", direction="in")],
    )
    gate = OracleVerificationGate(store=store, design_id="d0")
    with pytest.raises(OracleVerificationError, match="module_id"):
        gate.verify(oracle, assertions, other_module, stim=_COUNTER_STIM)
