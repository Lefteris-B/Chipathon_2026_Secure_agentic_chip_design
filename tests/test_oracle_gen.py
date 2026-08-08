"""F19.4 acceptance: OracleGenAgent produces a Python reference model
from a ContractArtifact.

The router is stubbed with canned Python source. The AC test fetches
the persisted blob, exec's it in-process, and verifies the resulting
``reference(stim)`` function steps the counter through a hand-rolled
5-cycle stimulus correctly. Production sandbox-based execution lives in
F19.6's triangulation gate; this test only proves the agent persists a
working reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.agents.oracle_gen import OracleGenAgent, OracleGenError
from chip_agent.design_state import (
    ArtifactKind,
    BehaviorInvariant,
    ClockDomain,
    ContractArtifact,
    DesignConstraints,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    ModuleDecl,
    OracleArtifact,
    Port,
    PortAssumption,
    Provenance,
    ResetSpec,
    Spec,
    Stage,
    TaskType,
)
from chip_agent.store import SqliteArtifactStore


# --------------------------------------------------------------------------- #
# StubRouter — satisfies the ModelRouter Protocol with canned Python source.
# --------------------------------------------------------------------------- #
@dataclass
class StubRouter:
    chosen: str
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic",
            model="claude-opus-4-7",
            temperature=0.2,
            seed=None,
            prompt_tokens=380,
            completion_tokens=220,
            cost_usd=0.0055,
        )
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        self.calls.append({
            "task": task,
            "context": dict(context),
            "escalation": escalation,
            "n": n,
        })
        return GenerationResult(
            candidates=[self.chosen],
            chosen=self.chosen,
            invocation=self.invocation,
        )


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
            Port(name="clk", direction="in", width=1, description="primary clock"),
            Port(name="rst_n", direction="in", width=1, description="async active-low reset"),
            Port(name="en", direction="in", width=1, description="sync enable"),
            Port(name="q", direction="out", width=8, description="current count value"),
        ],
    )


def _contract_counter() -> ContractArtifact:
    return ContractArtifact(
        artifact_id="d0.counter.contract",
        design_id="d0",
        module_id="counter",
        behavior_invariants=[
            BehaviorInvariant(
                name="increment_by_one",
                description="On rising clk while rst_n high and en high, q advances by 1.",
                condition="(en && rst_n) -> next(q) == (q + 1) mod 256",
            ),
            BehaviorInvariant(
                name="reset_clears_count",
                description="When rst_n is low, q is forced to 0 asynchronously.",
                condition="!rst_n -> q == 0",
            ),
            BehaviorInvariant(
                name="count_wraps_at_2_to_n",
                description="An 8-bit counter wraps from 255 back to 0.",
                condition="q == 255 && en -> next(q) == 0",
            ),
        ],
        port_assumptions=[
            PortAssumption(port_name="q", expected_range=(0, 255), encoding="binary"),
        ],
        clock_domains=[
            ClockDomain(name="clk", frequency_mhz=100.0, source="external"),
        ],
        reset=ResetSpec(
            name="rst_n",
            polarity="active_low",
            synchronicity="async",
            affects=["q"],
        ),
        encoding={"is_pipelined": "false"},
        ambiguity_notes=["spec did not pin overflow behaviour; assumed wrap modulo 256"],
        provenance=Provenance(produced_by=Stage.PLAN, agent="contract_extractor"),
    )


def _spec_counter() -> Spec:
    return Spec(
        artifact_id="d0.spec",
        design_id="d0",
        raw_text="8-bit counter, async active-low reset",
        normalized="* 8-bit counter\n* async active-low reset (rst_n)\n* en gates increment\n",
        requirements=["Counter increments while en is high"],
        constraints=DesignConstraints(pdk="sky130A"),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


# --------------------------------------------------------------------------- #
# Canned model outputs.
# --------------------------------------------------------------------------- #
_COUNTER_REFERENCE_PY = '''\
# rationale: counter wraps modulo 256 per contract ambiguity_notes
# rationale: rst_n is active-low and async — q snaps to 0 the instant it deasserts

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
'''

_TRIVIAL_REFERENCE_PY = """\
def reference(stim):
    return [{"q": 0} for _ in stim]
"""


# --------------------------------------------------------------------------- #
# Tests — plumbing
# --------------------------------------------------------------------------- #
def test_generate_returns_oracle_artifact(store: SqliteArtifactStore) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())

    assert isinstance(oracle, OracleArtifact)
    assert oracle.kind is ArtifactKind.ORACLE
    assert oracle.module_id == "counter"
    assert oracle.design_id == "d0"
    assert oracle.artifact_id == "d0.counter.oracle"


def test_generate_persists_source_as_python_blob(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())

    assert oracle.source.media_type == "text/x-python"
    blob = store.get_blob(oracle.source)
    # The agent normalises to exactly one trailing newline.
    assert blob.endswith(b"\n")
    assert b"def reference(stim):" in blob


def _code_calls(router: StubRouter) -> list:
    """F19.4b: filter router calls to the ORACLE_GEN (code) task.

    The plan stage's ORACLE_PLAN call is the first call per generate()
    invocation when the stub returns a non-JSON response (the
    StubRouter's canned text is the counter Python source, so the
    plan parse fails and the agent falls back to direct code-gen).
    Existing assertions that pre-date F19.4b key on the code call's
    shape; this helper keeps them stable.
    """
    return [c for c in router.calls if c["task"] == TaskType.ORACLE_GEN]


def test_generate_dispatches_to_oracle_gen_task(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    # F19.4b: 2 router calls now — plan (unparseable JSON → fallback)
    # + code. The code call is the canonical ORACLE_GEN dispatch.
    code_calls = _code_calls(router)
    assert len(code_calls) == 1
    assert code_calls[0]["task"] == TaskType.ORACLE_GEN


def test_generate_passes_system_prompt_to_router(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    code_calls = _code_calls(router)
    assert code_calls[0]["context"]["system"] == OracleGenAgent.SYSTEM_PROMPT


def test_generate_inlines_contract_into_user_prompt(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    prompt = _code_calls(router)[0]["context"]["prompt"]
    # Contract sub-models must be visible in the prompt the model receives.
    assert "increment_by_one" in prompt          # behaviour invariant
    assert "active_low" in prompt                # reset polarity
    assert "is_pipelined" in prompt              # encoding key
    assert "ambiguity_notes" in prompt
    assert "wrap modulo 256" in prompt           # the actual note text


def test_generate_inlines_module_ports_into_user_prompt(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    prompt = _code_calls(router)[0]["context"]["prompt"]
    for port in ("clk", "rst_n", "en", "q"):
        assert port in prompt, f"port {port!r} missing from user prompt"


def test_generate_inlines_spec_normalized_when_provided(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")

    # Without spec: prompt does not carry the normalised spec text.
    agent.generate(_contract_counter(), _module_counter())
    code_calls = _code_calls(router)
    prompt_no_spec = code_calls[0]["context"]["prompt"]
    assert "Raw spec" not in prompt_no_spec
    assert "en gates increment" not in prompt_no_spec

    # With spec: the marker AND the normalised body land in the prompt.
    agent.generate(_contract_counter(), _module_counter(), spec=_spec_counter())
    code_calls = _code_calls(router)
    prompt_with_spec = code_calls[1]["context"]["prompt"]
    assert "Raw spec" in prompt_with_spec
    assert "en gates increment" in prompt_with_spec


# --------------------------------------------------------------------------- #
# Tests — parsing pipeline
# --------------------------------------------------------------------------- #
def test_generate_strips_python_fence_markers(
    store: SqliteArtifactStore,
) -> None:
    fenced = "```python\n" + _TRIVIAL_REFERENCE_PY + "\n```"
    router = StubRouter(chosen=fenced)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())

    blob = store.get_blob(oracle.source)
    assert b"```" not in blob


def test_generate_normalises_trailing_newline(
    store: SqliteArtifactStore,
) -> None:
    # No trailing newline at all.
    no_newline = _TRIVIAL_REFERENCE_PY.rstrip("\n")
    router = StubRouter(chosen=no_newline)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())
    blob = store.get_blob(oracle.source)
    assert blob.endswith(b"\n")
    assert not blob.endswith(b"\n\n")

    # Many trailing newlines collapse to one.
    many_newlines = _TRIVIAL_REFERENCE_PY + "\n\n\n\n"
    router2 = StubRouter(chosen=many_newlines)
    agent2 = OracleGenAgent(router=router2, store=store, design_id="d0")
    oracle2 = agent2.generate(_contract_counter(), _module_counter())
    blob2 = store.get_blob(oracle2.source)
    assert blob2.endswith(b"\n")
    assert not blob2.endswith(b"\n\n")


def test_generate_raises_on_syntactically_invalid_python(
    store: SqliteArtifactStore,
) -> None:
    """A SyntaxError MUST surface as OracleGenError before anything is
    persisted — no orphan blob, no orphan artifact row."""
    bogus = "def reference("
    router = StubRouter(chosen=bogus)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    with pytest.raises(OracleGenError, match="compile"):
        agent.generate(_contract_counter(), _module_counter())

    # No oracle artifact row should exist for this design.
    refs = store.list(design_id="d0", kind=ArtifactKind.ORACLE)
    assert refs == []


def test_generate_raises_when_model_output_is_empty_after_fence_strip(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen="```python\n\n```")
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    with pytest.raises(OracleGenError, match="no usable Python"):
        agent.generate(_contract_counter(), _module_counter())


def test_generate_populates_module_signature_from_ports(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    module = _module_counter()
    oracle = agent.generate(_contract_counter(), module)

    assert oracle.module_signature == module.ports
    assert oracle.reference_fn_name == "reference"


def test_generate_extracts_rationale_notes_from_leading_comments(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_COUNTER_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())

    assert oracle.rationale_notes == [
        "counter wraps modulo 256 per contract ambiguity_notes",
        "rst_n is active-low and async — q snaps to 0 the instant it deasserts",
    ]


def test_generate_rationale_notes_empty_when_no_leading_comments(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())
    assert oracle.rationale_notes == []


# --------------------------------------------------------------------------- #
# Tests — provenance
# --------------------------------------------------------------------------- #
def test_generate_records_provenance_with_contract_only_by_default(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    contract = _contract_counter()
    oracle = agent.generate(contract, _module_counter())

    assert oracle.provenance.agent == "oracle_gen"
    assert oracle.provenance.produced_by is Stage.PLAN
    assert oracle.provenance.model is not None
    assert oracle.provenance.model.model == "claude-opus-4-7"
    assert len(oracle.provenance.inputs) == 1
    assert oracle.provenance.inputs[0].artifact_id == "d0.counter.contract"


def test_generate_records_provenance_with_spec_when_provided(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    contract = _contract_counter()
    spec = _spec_counter()
    oracle = agent.generate(contract, _module_counter(), spec=spec)

    ids = [r.artifact_id for r in oracle.provenance.inputs]
    assert ids == ["d0.counter.contract", "d0.spec"]


# --------------------------------------------------------------------------- #
# Tests — error paths
# --------------------------------------------------------------------------- #
def test_generate_raises_on_design_id_mismatch(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    contract = _contract_counter()
    contract = contract.model_copy(update={"design_id": "d1"})
    with pytest.raises(OracleGenError, match="design_id"):
        agent.generate(contract, _module_counter())


def test_generate_raises_on_contract_module_mismatch(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    other_module = ModuleDecl(
        module_id="not_counter", name="not_counter",
        description="something else",
        ports=[Port(name="clk", direction="in")],
    )
    with pytest.raises(OracleGenError, match="module_id"):
        agent.generate(_contract_counter(), other_module)


# --------------------------------------------------------------------------- #
# AC: persisted source simulates a counter against a hand-rolled stimulus.
# --------------------------------------------------------------------------- #
def test_counter_oracle_source_correctly_simulates_a_5_cycle_stim(
    store: SqliteArtifactStore,
) -> None:
    """F19.4 AC: 'a counter contract produces a Python function
    `def reference(stim) -> list[OutputCycle]:` that produces correct
    outputs for a hand-rolled stimulus sequence; persisted as a BlobRef on
    OracleArtifact.source.'

    The stub returns a counter reference; the test fetches the blob,
    compile+exec's it in-process, and verifies the function simulates a
    5-cycle stimulus correctly. Production sandbox-based execution
    lives in F19.6.
    """
    router = StubRouter(chosen=_COUNTER_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())

    source = store.get_blob(oracle.source)
    namespace: dict[str, object] = {}
    code = compile(source, "<oracle>", "exec")
    exec(code, namespace)

    reference = namespace[oracle.reference_fn_name]
    assert callable(reference)

    stim = [
        {"clk": 1, "rst_n": 0, "en": 0},  # reset asserted  -> q = 0
        {"clk": 1, "rst_n": 1, "en": 0},  # released, en low -> q hold
        {"clk": 1, "rst_n": 1, "en": 1},  # en on -> q increments
        {"clk": 1, "rst_n": 1, "en": 1},
        {"clk": 1, "rst_n": 1, "en": 1},
    ]
    out = reference(stim)
    assert [c["q"] for c in out] == [0, 0, 1, 2, 3]


# --------------------------------------------------------------------------- #
# F19.4b — plan stage + worked-example self-check
# --------------------------------------------------------------------------- #
import json as _json  # noqa: E402

from chip_agent.tools.sandbox import ProcessResult  # noqa: E402

_COUNTER_PLAN_JSON = _json.dumps({
    "is_clocked": True,
    "step_summary": (
        "On rising clk: if rst_n low, q held at 0; "
        "else if en high, q increments by 1 mod 256; "
        "else q holds previous value."
    ),
    "reset_summary": "Active-low async reset forces q to 0 immediately.",
    "key_state": ["q"],
    "worked_example": [
        {"cycle_index": 0, "stim": {"rst_n": 0, "en": 0}, "expected_output": {"q": 0}},
        {"cycle_index": 1, "stim": {"rst_n": 1, "en": 0}, "expected_output": {"q": 0}},
        {"cycle_index": 2, "stim": {"rst_n": 1, "en": 1}, "expected_output": {"q": 1}},
        {"cycle_index": 3, "stim": {"rst_n": 1, "en": 1}, "expected_output": {"q": 2}},
        {"cycle_index": 4, "stim": {"rst_n": 1, "en": 1}, "expected_output": {"q": 3}},
    ],
})

# The matching counter Python — produces q=[0,0,1,2,3] for the
# worked_example stim above, so the self-check passes on the first
# code attempt.
_MATCHING_COUNTER_PY = """\
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

# A semantically-wrong counter that increments by 2 — produces
# q=[0,0,2,4,6] for the worked_example stim, diverging from the
# plan's [0,0,1,2,3]. Self-check rejects it.
_OFF_BY_TWO_COUNTER_PY = """\
def reference(stim):
    q = 0
    out = []
    for cyc in stim:
        if cyc.get("rst_n", 1) == 0:
            q = 0
        elif cyc.get("en", 0) == 1:
            q = (q + 2) % 256
        out.append({"q": q})
    return out
"""


@dataclass
class _SequencedRouter:
    """F19.4b test seam: returns a sequence of canned responses keyed by
    task. The plan call gets ``plan_responses.pop(0)`` (or the
    constructor default), each code call gets ``code_responses.pop(0)``,
    falling back to the last entry. Records every call for assertions.
    """
    plan_responses: list[str] = field(default_factory=list)
    code_responses: list[str] = field(default_factory=list)
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic", model="claude-opus-4-7",
            temperature=0.0, seed=None,
            prompt_tokens=400, completion_tokens=200, cost_usd=0.005,
        ),
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        self.calls.append({
            "task": task,
            "context": dict(context),
            "escalation": escalation, "n": n,
        })
        if task is TaskType.ORACLE_PLAN:
            text = (
                self.plan_responses.pop(0)
                if self.plan_responses
                else "not valid json"
            )
        else:
            text = (
                self.code_responses.pop(0)
                if self.code_responses
                else _TRIVIAL_REFERENCE_PY
            )
        return GenerationResult(
            candidates=[text], chosen=text, invocation=self.invocation,
        )


def test_plan_json_round_trips_via_parse_helper() -> None:
    """The plan helper's JSON-parse + shape validation accept the
    canonical counter plan shape."""
    from chip_agent.agents.oracle_gen import (
        _parse_plan_json,
        _validate_plan_shape,
    )
    payload = _parse_plan_json(_COUNTER_PLAN_JSON)
    assert payload is not None
    assert _validate_plan_shape(payload) is True
    assert len(payload["worked_example"]) == 5


def test_plan_validation_rejects_missing_worked_example() -> None:
    """Empty / missing worked_example fails the shape check."""
    from chip_agent.agents.oracle_gen import _validate_plan_shape
    assert _validate_plan_shape({"worked_example": []}) is False
    assert _validate_plan_shape({}) is False


def test_plan_validation_rejects_non_int_stim_value() -> None:
    """Stim / expected_output values must be ints (not strings, not bools)."""
    from chip_agent.agents.oracle_gen import _validate_plan_shape
    bad = {
        "worked_example": [
            {"cycle_index": 0, "stim": {"x": "high"}, "expected_output": {"y": 1}},
        ],
    }
    assert _validate_plan_shape(bad) is False
    bad_bool = {
        "worked_example": [
            {"cycle_index": 0, "stim": {"x": True}, "expected_output": {"y": 1}},
        ],
    }
    assert _validate_plan_shape(bad_bool) is False


def test_generate_runs_plan_stage_before_code_stage(
    store: SqliteArtifactStore,
) -> None:
    """When the plan stage emits valid JSON, the agent calls plan
    first then code-with-plan; the code prompt includes the inlined
    plan."""
    router = _SequencedRouter(
        plan_responses=[_COUNTER_PLAN_JSON],
        code_responses=[_MATCHING_COUNTER_PY],
    )
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    plan_calls = [c for c in router.calls if c["task"] == TaskType.ORACLE_PLAN]
    code_calls = [c for c in router.calls if c["task"] == TaskType.ORACLE_GEN]
    assert len(plan_calls) == 1
    assert len(code_calls) == 1
    # Code prompt is plan-aware: includes the plan JSON literal.
    code_prompt = code_calls[0]["context"]["prompt"]
    assert "Your previously-emitted PLAN" in code_prompt
    assert "increment_by_one" in code_prompt  # contract still in prompt


def test_generate_persists_plan_into_rationale_notes(
    store: SqliteArtifactStore,
) -> None:
    """The validated plan JSON lands in OracleArtifact.rationale_notes
    (which is in _NON_CONTENT_FIELDS, so the content hash is unchanged
    from the pre-F19.4b path).
    """
    router = _SequencedRouter(
        plan_responses=[_COUNTER_PLAN_JSON],
        code_responses=[_MATCHING_COUNTER_PY],
    )
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())

    assert len(oracle.rationale_notes) >= 1
    plan_note = oracle.rationale_notes[0]
    assert plan_note.startswith("plan: ")
    parsed = _json.loads(plan_note.removeprefix("plan: "))
    assert parsed["is_clocked"] is True
    assert len(parsed["worked_example"]) == 5


def test_generate_self_check_retries_on_worked_example_mismatch(
    store: SqliteArtifactStore,
) -> None:
    """When the first code attempt diverges from the plan's
    worked_example, the agent retries with a delta prompt. Second
    attempt converges → persisted."""
    router = _SequencedRouter(
        plan_responses=[_COUNTER_PLAN_JSON],
        code_responses=[_OFF_BY_TWO_COUNTER_PY, _MATCHING_COUNTER_PY],
    )
    agent = OracleGenAgent(
        router=router, store=store, design_id="d0",
        max_self_check_attempts=2,
    )
    oracle = agent.generate(_contract_counter(), _module_counter())

    code_calls = [c for c in router.calls if c["task"] == TaskType.ORACLE_GEN]
    # First attempt + one retry = 2 code calls.
    assert len(code_calls) == 2
    # Retry prompt carries the mismatch delta.
    retry_prompt = code_calls[1]["context"]["prompt"]
    assert "diverged from the plan's worked_example" in retry_prompt
    # The persisted source is the second (correct) attempt.
    source = store.get_blob(oracle.source).decode()
    assert "(q + 1) % 256" in source  # the matching counter
    assert "(q + 2) % 256" not in source  # not the off-by-two one


def test_generate_self_check_exhaustion_falls_back_to_direct(
    store: SqliteArtifactStore,
) -> None:
    """After ``max_self_check_attempts`` failed retries, the agent
    falls back to the direct (pre-F19.4b) code-gen path so the spine
    doesn't crash on a model that can't satisfy its own plan."""
    router = _SequencedRouter(
        plan_responses=[_COUNTER_PLAN_JSON],
        # Three off-by-two responses + a final direct-fallback response.
        code_responses=[
            _OFF_BY_TWO_COUNTER_PY,
            _OFF_BY_TWO_COUNTER_PY,
            _OFF_BY_TWO_COUNTER_PY,
            _MATCHING_COUNTER_PY,
        ],
    )
    agent = OracleGenAgent(
        router=router, store=store, design_id="d0",
        max_self_check_attempts=2,
    )
    oracle = agent.generate(_contract_counter(), _module_counter())

    # 3 self-check-aware attempts + 1 direct-fallback = 4 code calls.
    code_calls = [c for c in router.calls if c["task"] == TaskType.ORACLE_GEN]
    assert len(code_calls) == 4
    # The fallback (last) prompt does NOT mention the plan.
    fallback_prompt = code_calls[-1]["context"]["prompt"]
    assert "Your previously-emitted PLAN" not in fallback_prompt
    # An oracle WAS persisted (graceful degradation).
    assert oracle.kind.value == "oracle"


def test_generate_falls_back_to_direct_on_unparseable_plan(
    store: SqliteArtifactStore,
) -> None:
    """A plan stage that doesn't return JSON triggers graceful fallback
    to the direct code-gen path. The OracleArtifact still lands but
    rationale_notes has no ``plan:`` prefix entry."""
    router = _SequencedRouter(
        plan_responses=["not valid json at all"],
        code_responses=[_TRIVIAL_REFERENCE_PY],
    )
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())

    # No plan note in rationale_notes.
    plan_notes = [n for n in oracle.rationale_notes if n.startswith("plan: ")]
    assert plan_notes == []


def test_generate_falls_back_to_direct_on_router_exception_in_plan(
    store: SqliteArtifactStore,
) -> None:
    """If the plan-stage router call raises, the agent degrades to
    direct code-gen without surfacing the exception. This is the
    stub-backend path: ``StubBackend`` raises ``AssertionError`` for
    an unknown system prompt, which existing demos (which don't carry
    a plan-stage matcher) rely on for back-compat. F19.4b's stub adds
    such a matcher; this test pins the fallback for fixtures that
    don't.
    """
    @dataclass
    class _RaisingPlanRouter:
        code_response: str
        calls: list[dict[str, Any]] = field(default_factory=list)
        invocation: ModelInvocation = field(
            default_factory=lambda: ModelInvocation(
                provider="anthropic", model="claude-opus-4-7",
                temperature=0.0, seed=None,
                prompt_tokens=400, completion_tokens=100, cost_usd=0.001,
            ),
        )

        def generate(self, task, *, context, failure=None,
                     escalation=EscalationLevel.INNER, n=None):
            self.calls.append({"task": task, "context": dict(context)})
            if task is TaskType.ORACLE_PLAN:
                raise AssertionError(
                    "StubBackend received an unrecognised system prompt",
                )
            return GenerationResult(
                candidates=[self.code_response],
                chosen=self.code_response,
                invocation=self.invocation,
            )

    router = _RaisingPlanRouter(code_response=_TRIVIAL_REFERENCE_PY)
    agent = OracleGenAgent(router=router, store=store, design_id="d0")
    oracle = agent.generate(_contract_counter(), _module_counter())

    # The plan call was attempted but raised; agent didn't propagate.
    plan_calls = [c for c in router.calls if c["task"] == TaskType.ORACLE_PLAN]
    assert len(plan_calls) == 1
    # No plan in rationale_notes.
    plan_notes = [n for n in oracle.rationale_notes if n.startswith("plan: ")]
    assert plan_notes == []
    # Oracle still persisted from the direct code-gen path.
    assert oracle.kind.value == "oracle"


def test_max_self_check_attempts_validation() -> None:
    """``max_self_check_attempts`` must be >= 0."""
    router = _SequencedRouter()
    with pytest.raises(OracleGenError, match="max_self_check_attempts"):
        # Just constructing the agent should raise; we use a temp store.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            store = SqliteArtifactStore(
                db_path=tmp_p / "s.sqlite", content_dir=tmp_p / "c",
            )
            try:
                OracleGenAgent(
                    router=router, store=store, design_id="d0",
                    max_self_check_attempts=-1,
                )
            finally:
                store.close()


# --------------------------------------------------------------------------- #
# F19.4c — per-cycle oracle contract: deterministic guard against
# clock-edge-detection patterns in the generated oracle. Driven by the
# live shift_register failure where a `prev_clk` reference function
# produced an all-zeros oracle (stim_ramp pins clk=1 every row, so
# edge detection never fired past row 0).
# --------------------------------------------------------------------------- #

# The exact buggy oracle source the live shift_register run produced —
# uses `prev_clk == 0 and clk == 1` edge detection that breaks under
# the per-row stim contract.
_BUGGY_PREV_CLK_ORACLE_PY = """\
def reference(stim):
    DATA_BITS = 8
    RESET_VALUE = 0x00
    MASK = (1 << DATA_BITS) - 1
    results = []
    reg = RESET_VALUE
    prev_clk = 0
    for cycle in stim:
        clk = int(cycle.get('clk', 0))
        rst_n = int(cycle.get('rst_n', 1))
        en = int(cycle.get('en', 0))
        sin = int(cycle.get('sin', 0))
        if rst_n == 0:
            reg = RESET_VALUE
        else:
            if prev_clk == 0 and clk == 1:
                if en == 1:
                    reg = ((reg << 1) | sin) & MASK
        results.append({'pout': reg})
        prev_clk = clk
    return results
"""


def test_detect_edge_detection_pattern_flags_prev_clk() -> None:
    """The live-failure shape: source containing ``prev_clk`` matches
    the prev_clk pattern."""
    from chip_agent.agents.oracle_gen import _detect_edge_detection_patterns
    assert _detect_edge_detection_patterns(_BUGGY_PREV_CLK_ORACLE_PY) == "prev_clk"
    # Simpler shape also matches.
    assert _detect_edge_detection_patterns(
        "def reference(stim):\n    prev_clk = 0\n",
    ) == "prev_clk"


def test_detect_edge_detection_pattern_flags_cyc_get_clk() -> None:
    """``cyc.get('clk', ...)`` and ``cycle["clk"]`` both flag as
    forbidden reads of the clk field from the stim row."""
    from chip_agent.agents.oracle_gen import _detect_edge_detection_patterns
    src_get = 'def reference(stim):\n    for cyc in stim:\n        x = cyc.get("clk", 0)\n'
    assert _detect_edge_detection_patterns(src_get) == "cyc.get('clk'"
    src_index = "def reference(stim):\n    for cycle in stim:\n        x = cycle['clk']\n"
    assert _detect_edge_detection_patterns(src_index) == "cyc.get('clk'"
    # `row[...]` and `step.get(...)` variants also flag.
    src_row = 'def reference(stim):\n    for row in stim:\n        x = row["clk"]\n'
    assert _detect_edge_detection_patterns(src_row) == "cyc.get('clk'"


def test_detect_edge_detection_pattern_clean_source_returns_none() -> None:
    """The demo stub counter oracle (the canonical clean shape) passes
    the guard. Mirrors tests/_routing_stub.py:COUNTER_ORACLE_PY so
    demo flows are guaranteed not to fire this guard."""
    from chip_agent.agents.oracle_gen import _detect_edge_detection_patterns
    assert _detect_edge_detection_patterns(_MATCHING_COUNTER_PY) is None
    # The demo stub counter oracle from _routing_stub also passes —
    # iterates `for cyc in stim` with no clk reference.
    clean = (
        "def reference(stim):\n"
        "    q = 0\n"
        "    out = []\n"
        "    for cyc in stim:\n"
        "        if cyc.get('rst_n', 1) == 0:\n"
        "            q = 0\n"
        "        elif cyc.get('en', 0) == 1:\n"
        "            q = (q + 1) % 256\n"
        "        out.append({'q': q})\n"
        "    return out\n"
    )
    assert _detect_edge_detection_patterns(clean) is None


def test_self_check_flags_edge_detection_before_worked_example_diff(
    store: SqliteArtifactStore,
) -> None:
    """When an oracle contains BOTH a forbidden `prev_clk` pattern AND
    would diverge from the worked_example, _self_check returns the
    guard's ``kind: 'edge_detection'`` diagnostic — the cheap guard
    runs FIRST so the model gets a clean correction signal."""
    import json as _json
    plan = _json.loads(_COUNTER_PLAN_JSON)
    router = _SequencedRouter()
    agent = OracleGenAgent(
        router=router, store=store, design_id="d0",
        max_self_check_attempts=1,
    )
    diagnostic = agent._self_check(_BUGGY_PREV_CLK_ORACLE_PY, plan)
    assert diagnostic is not None
    assert diagnostic["kind"] == "edge_detection"
    assert diagnostic["matched_pattern"] == "prev_clk"
    assert "do NOT implement edge detection" in diagnostic["message"]


def test_oracle_system_prompt_contains_per_row_contract_rule() -> None:
    """Pin the prompt rule against silent removal. The model has to
    read this contract to know it can't implement edge detection.
    """
    from chip_agent.agents.oracle_gen import _ORACLE_SYSTEM_PROMPT
    assert "Each STIM row IS one rising clock edge" in _ORACLE_SYSTEM_PROMPT
    assert "DO NOT implement edge detection" in _ORACLE_SYSTEM_PROMPT
    assert "DO NOT track `prev_clk`" in _ORACLE_SYSTEM_PROMPT
    # The correct shape is shown inline as a worked example.
    assert "for cyc in stim" in _ORACLE_SYSTEM_PROMPT


def test_generate_retries_after_edge_detection_guard_fires(
    store: SqliteArtifactStore,
) -> None:
    """**AC**: a buggy `prev_clk`-based first attempt fires the
    deterministic guard; the retry prompt surfaces the matched
    pattern; the second attempt is a clean `for cyc in stim` oracle
    that satisfies the worked_example. Persisted oracle is the
    corrected version.
    """
    router = _SequencedRouter(
        plan_responses=[_COUNTER_PLAN_JSON],
        code_responses=[_BUGGY_PREV_CLK_ORACLE_PY, _MATCHING_COUNTER_PY],
    )
    agent = OracleGenAgent(
        router=router, store=store, design_id="d0",
        max_self_check_attempts=2,
    )
    oracle = agent.generate(_contract_counter(), _module_counter())

    code_calls = [c for c in router.calls if c["task"] == TaskType.ORACLE_GEN]
    assert len(code_calls) == 2
    # The retry prompt surfaces the edge-detection correction signal.
    retry_prompt = code_calls[1]["context"]["prompt"]
    assert "edge-detection guard" in retry_prompt
    assert "prev_clk" in retry_prompt
    # Persisted oracle is the clean second attempt, not the buggy first.
    source = store.get_blob(oracle.source).decode()
    assert "prev_clk" not in source
    assert "(q + 1) % 256" in source


# Silence unused-import warnings.
_ = ProcessResult
