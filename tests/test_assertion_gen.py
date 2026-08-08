"""F19.5 acceptance: AssertionGenAgent produces an AssertionSpec via
the three-stage AssertLLM pipeline (extract / map / translate).

The stub router responds per TaskType so the three sequential calls
each get the right canned content. The AC test fetches the persisted
Python blob, exec's it in-process, and verifies the three resulting
``assert_*`` functions are callable against a (stim, observed) tuple
and that each correctly distinguishes a passing case from a violating
one. Production sandboxed execution lives in F19.6.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.agents.assertion_gen import (
    AssertionGenAgent,
    AssertionGenError,
    SignalDefinitions,
    SignalMap,
)
from chip_agent.design_state import (
    ArtifactKind,
    AssertionSpec,
    BehaviorInvariant,
    ClockDomain,
    ContractArtifact,
    DesignConstraints,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    ModuleDecl,
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
# SequencedStubRouter — keyed by TaskType. Each task gets exactly one canned
# response; calls are recorded in dispatch order.
# --------------------------------------------------------------------------- #
@dataclass
class SequencedStubRouter:
    by_task: dict[TaskType, str]
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic",
            model="claude-opus-4-7",
            temperature=0.2,
            seed=None,
            prompt_tokens=420,
            completion_tokens=260,
            cost_usd=0.0065,
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
        if task not in self.by_task:
            raise AssertionError(
                f"SequencedStubRouter has no canned response for task {task!r}"
            )
        chosen = self.by_task[task]
        return GenerationResult(
            candidates=[chosen],
            chosen=chosen,
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
                name="reset_clears_count",
                description="When rst_n low, q is forced to 0.",
                condition="!rst_n -> q == 0",
            ),
            BehaviorInvariant(
                name="increment_by_one",
                description="On rising clk while rst_n high and en high, q advances.",
                condition="(en && rst_n) -> next(q) == (q + 1) mod 256",
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
        normalized="* 8-bit counter\n* async active-low reset\n* en gates increment\n",
        requirements=["Counter increments while en is high"],
        constraints=DesignConstraints(pdk="sky130A"),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


# --------------------------------------------------------------------------- #
# Canned stage outputs.
# --------------------------------------------------------------------------- #
_SIGNAL_MAP_JSON = json.dumps({
    "signals": [
        {"name": "clk", "direction": "in", "width": 1, "role": "clock"},
        {"name": "rst_n", "direction": "in", "width": 1, "role": "reset"},
        {"name": "en", "direction": "in", "width": 1, "role": "enable"},
        {"name": "q", "direction": "out", "width": 8, "role": "count"},
    ],
    "referenced_invariants": [
        "reset_clears_count",
        "increment_by_one",
        "count_wraps_at_2_to_n",
    ],
})


_SIGNAL_DEFINITIONS_JSON = json.dumps({
    "definitions": [
        {
            "signal_name": "rst_n",
            "semantic_role": "active-low async reset; forces q to 0",
            "constraints": [],
        },
        {
            "signal_name": "en",
            "semantic_role": "synchronous increment enable",
            "constraints": [],
        },
        {
            "signal_name": "q",
            "semantic_role": "8-bit unsigned count value",
            "constraints": ["range [0, 255]", "wraps modulo 256"],
        },
    ],
})


_COUNTER_ASSERTIONS_PY = '''\
# rationale: counter assertions check reset / increment / wrap per contract
# rationale: each function takes (stim, observed) and returns (passed, message)

def assert_reset_clears_count(args):
    """When rst_n is low, q must be 0 on that cycle."""
    stim, observed = args
    for s, o in zip(stim, observed):
        if s.get("rst_n", 1) == 0 and o.get("q", 0) != 0:
            return (False, f"rst_n low but q={o['q']}")
    return (True, "reset always clears q")


def assert_increment_by_one(args):
    """When rst_n high and en high, q advances by 1 each cycle."""
    stim, observed = args
    prev_q = None
    for s, o in zip(stim, observed):
        if s.get("rst_n", 1) == 1 and s.get("en", 0) == 1 and prev_q is not None:
            expected = (prev_q + 1) % 256
            if o.get("q", 0) != expected:
                return (False, f"expected q={expected}, got {o['q']}")
        prev_q = o.get("q", 0)
    return (True, "increment behaviour holds")


def assert_count_wraps_at_2_to_n(args):
    """When q is 255 and en is high, next q must be 0."""
    stim, observed = args
    prev_q = None
    for s, o in zip(stim, observed):
        if (prev_q == 255 and s.get("en", 0) == 1
                and s.get("rst_n", 1) == 1
                and o.get("q", 0) != 0):
            return (False, f"q was 255 but next q was {o['q']}, not 0")
        prev_q = o.get("q", 0)
    return (True, "wrap behaviour holds")
'''


def _full_router() -> SequencedStubRouter:
    """Stub router primed with all three canned responses for the
    happy path. Tests that exercise an error path override one entry."""
    return SequencedStubRouter(by_task={
        TaskType.ASSERTION_EXTRACT: _SIGNAL_MAP_JSON,
        TaskType.ASSERTION_MAP: _SIGNAL_DEFINITIONS_JSON,
        TaskType.ASSERTION_TRANSLATE: _COUNTER_ASSERTIONS_PY,
    })


# --------------------------------------------------------------------------- #
# Tests — plumbing
# --------------------------------------------------------------------------- #
def test_generate_returns_assertion_spec(store: SqliteArtifactStore) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    spec = agent.generate(_contract_counter(), _module_counter())

    assert isinstance(spec, AssertionSpec)
    assert spec.kind is ArtifactKind.ASSERTIONS
    assert spec.module_id == "counter"
    assert spec.design_id == "d0"
    assert spec.artifact_id == "d0.counter.assertions"


def test_generate_dispatches_three_calls_in_order(
    store: SqliteArtifactStore,
) -> None:
    """AssertLLM order: EXTRACT then MAP then TRANSLATE. Pins the
    pipeline sequence so a future refactor can't shuffle stages."""
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    assert len(router.calls) == 3
    assert [c["task"] for c in router.calls] == [
        TaskType.ASSERTION_EXTRACT,
        TaskType.ASSERTION_MAP,
        TaskType.ASSERTION_TRANSLATE,
    ]


def test_generate_passes_correct_system_prompt_per_stage(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    assert router.calls[0]["context"]["system"] == (
        AssertionGenAgent.EXTRACT_SYSTEM_PROMPT
    )
    assert router.calls[1]["context"]["system"] == (
        AssertionGenAgent.MAP_SYSTEM_PROMPT
    )
    assert router.calls[2]["context"]["system"] == (
        AssertionGenAgent.TRANSLATE_SYSTEM_PROMPT
    )


def test_extract_user_prompt_contains_contract_and_module(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    extract_prompt = router.calls[0]["context"]["prompt"]
    assert "reset_clears_count" in extract_prompt
    assert "increment_by_one" in extract_prompt
    assert "count_wraps_at_2_to_n" in extract_prompt
    for port in ("clk", "rst_n", "en", "q"):
        assert port in extract_prompt


def test_map_user_prompt_inlines_signal_map_from_extract_stage(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    map_prompt = router.calls[1]["context"]["prompt"]
    # The signal-map JSON from stage A must appear in stage B's prompt,
    # otherwise the pipeline is broken (stage B has no input to map).
    assert "SignalMap" in map_prompt
    assert "rst_n" in map_prompt
    assert "referenced_invariants" in map_prompt


def test_translate_user_prompt_inlines_signal_definitions(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    translate_prompt = router.calls[2]["context"]["prompt"]
    assert "SignalDefinitions" in translate_prompt
    assert "active-low async reset" in translate_prompt
    assert "8-bit unsigned count" in translate_prompt


def test_generate_inlines_spec_when_provided(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter(), spec=_spec_counter())

    for call in router.calls:
        prompt = call["context"]["prompt"]
        assert "Raw spec" in prompt
        assert "en gates increment" in prompt


def test_generate_omits_spec_when_not_provided(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    agent.generate(_contract_counter(), _module_counter())

    for call in router.calls:
        prompt = call["context"]["prompt"]
        assert "Raw spec" not in prompt
        assert "en gates increment" not in prompt


# --------------------------------------------------------------------------- #
# Tests — intermediate parsing
# --------------------------------------------------------------------------- #
def test_signal_map_parses_canned_json() -> None:
    """The SignalMap Pydantic model rejects malformed shapes loudly —
    pin the happy path so a sub-model schema change can't silently
    drift the contract."""
    data = json.loads(_SIGNAL_MAP_JSON)
    sm = SignalMap.model_validate(data)
    assert len(sm.signals) == 4
    assert {s.name for s in sm.signals} == {"clk", "rst_n", "en", "q"}
    assert sm.referenced_invariants == [
        "reset_clears_count", "increment_by_one", "count_wraps_at_2_to_n",
    ]


def test_signal_definitions_parses_canned_json() -> None:
    data = json.loads(_SIGNAL_DEFINITIONS_JSON)
    sd = SignalDefinitions.model_validate(data)
    assert len(sd.definitions) == 3
    assert {d.signal_name for d in sd.definitions} == {"rst_n", "en", "q"}


def test_extract_stage_raises_on_invalid_json(
    store: SqliteArtifactStore,
) -> None:
    router = SequencedStubRouter(by_task={
        TaskType.ASSERTION_EXTRACT: "not json {",
        TaskType.ASSERTION_MAP: _SIGNAL_DEFINITIONS_JSON,
        TaskType.ASSERTION_TRANSLATE: _COUNTER_ASSERTIONS_PY,
    })
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    with pytest.raises(AssertionGenError, match=r"extract.*JSON"):
        agent.generate(_contract_counter(), _module_counter())
    # Nothing persisted on early failure.
    assert store.list(design_id="d0", kind=ArtifactKind.ASSERTIONS) == []


def test_map_stage_raises_on_invalid_definitions_shape(
    store: SqliteArtifactStore,
) -> None:
    # Map stage returns JSON without ``definitions`` key (it's an empty
    # object); Pydantic accepts that (default factory), so use a clearly
    # wrong type for the field.
    bogus_map = json.dumps({"definitions": "not a list"})
    router = SequencedStubRouter(by_task={
        TaskType.ASSERTION_EXTRACT: _SIGNAL_MAP_JSON,
        TaskType.ASSERTION_MAP: bogus_map,
        TaskType.ASSERTION_TRANSLATE: _COUNTER_ASSERTIONS_PY,
    })
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    with pytest.raises(AssertionGenError, match="map stage"):
        agent.generate(_contract_counter(), _module_counter())
    assert store.list(design_id="d0", kind=ArtifactKind.ASSERTIONS) == []


# --------------------------------------------------------------------------- #
# Tests — translate stage parsing
# --------------------------------------------------------------------------- #
def test_translate_stage_strips_python_fences(
    store: SqliteArtifactStore,
) -> None:
    fenced = "```python\n" + _COUNTER_ASSERTIONS_PY + "\n```"
    router = SequencedStubRouter(by_task={
        TaskType.ASSERTION_EXTRACT: _SIGNAL_MAP_JSON,
        TaskType.ASSERTION_MAP: _SIGNAL_DEFINITIONS_JSON,
        TaskType.ASSERTION_TRANSLATE: fenced,
    })
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    spec = agent.generate(_contract_counter(), _module_counter())

    blob = store.get_blob(spec.source)
    assert b"```" not in blob


def test_translate_stage_raises_on_syntax_error(
    store: SqliteArtifactStore,
) -> None:
    """A SyntaxError MUST surface as AssertionGenError before anything
    is persisted — no orphan blob, no orphan AssertionSpec row."""
    router = SequencedStubRouter(by_task={
        TaskType.ASSERTION_EXTRACT: _SIGNAL_MAP_JSON,
        TaskType.ASSERTION_MAP: _SIGNAL_DEFINITIONS_JSON,
        TaskType.ASSERTION_TRANSLATE: "def assert_reset(",
    })
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    with pytest.raises(AssertionGenError, match="parse"):
        agent.generate(_contract_counter(), _module_counter())
    assert store.list(design_id="d0", kind=ArtifactKind.ASSERTIONS) == []


def test_translate_stage_raises_when_no_assert_functions(
    store: SqliteArtifactStore,
) -> None:
    """The AC requires >=1 assertion. A spec with no `def assert_*`
    callsites can't be useful — fail loudly so the upstream caller
    gets a clean retry signal."""
    no_asserts = "def helper(x):\n    return x + 1\n"
    router = SequencedStubRouter(by_task={
        TaskType.ASSERTION_EXTRACT: _SIGNAL_MAP_JSON,
        TaskType.ASSERTION_MAP: _SIGNAL_DEFINITIONS_JSON,
        TaskType.ASSERTION_TRANSLATE: no_asserts,
    })
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    with pytest.raises(AssertionGenError, match="no `def assert_"):
        agent.generate(_contract_counter(), _module_counter())
    assert store.list(design_id="d0", kind=ArtifactKind.ASSERTIONS) == []


def test_translate_stage_extracts_assert_callsites_via_ast(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    spec = agent.generate(_contract_counter(), _module_counter())

    callsites = {inv.callsite for inv in spec.assertions}
    names = {inv.name for inv in spec.assertions}
    assert callsites == {
        "assert_reset_clears_count",
        "assert_increment_by_one",
        "assert_count_wraps_at_2_to_n",
    }
    assert names == {
        "reset_clears_count",
        "increment_by_one",
        "count_wraps_at_2_to_n",
    }
    # Descriptions come from each function's docstring.
    desc_by_name = {inv.name: inv.description for inv in spec.assertions}
    assert "rst_n is low" in desc_by_name["reset_clears_count"]


# --------------------------------------------------------------------------- #
# Tests — persistence + provenance
# --------------------------------------------------------------------------- #
def test_generate_persists_source_as_text_x_python_blob(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    spec = agent.generate(_contract_counter(), _module_counter())

    assert spec.source.media_type == "text/x-python"
    blob = store.get_blob(spec.source)
    assert blob.endswith(b"\n")
    assert b"def assert_reset_clears_count" in blob


def test_generate_extracts_rationale_notes_from_leading_comments(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    spec = agent.generate(_contract_counter(), _module_counter())

    assert spec.rationale_notes == [
        "counter assertions check reset / increment / wrap per contract",
        "each function takes (stim, observed) and returns (passed, message)",
    ]


def test_provenance_records_contract_only_by_default(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    contract = _contract_counter()
    spec = agent.generate(contract, _module_counter())

    assert spec.provenance.agent == "assertion_gen"
    assert spec.provenance.produced_by is Stage.PLAN
    assert spec.provenance.model is not None
    assert spec.provenance.model.model == "claude-opus-4-7"
    assert len(spec.provenance.inputs) == 1
    assert spec.provenance.inputs[0].artifact_id == "d0.counter.contract"


def test_provenance_appends_spec_ref_when_spec_provided(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    spec = agent.generate(
        _contract_counter(), _module_counter(), spec=_spec_counter(),
    )

    ids = [r.artifact_id for r in spec.provenance.inputs]
    assert ids == ["d0.counter.contract", "d0.spec"]


# --------------------------------------------------------------------------- #
# Tests — error paths
# --------------------------------------------------------------------------- #
def test_generate_raises_on_design_id_mismatch(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    contract = _contract_counter().model_copy(update={"design_id": "d1"})
    with pytest.raises(AssertionGenError, match="design_id"):
        agent.generate(contract, _module_counter())


def test_generate_raises_on_contract_module_mismatch(
    store: SqliteArtifactStore,
) -> None:
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    other = ModuleDecl(
        module_id="not_counter", name="not_counter",
        description="something else",
        ports=[Port(name="clk", direction="in")],
    )
    with pytest.raises(AssertionGenError, match="module_id"):
        agent.generate(_contract_counter(), other)


# --------------------------------------------------------------------------- #
# AC: counter contract produces >=3 callable assertions covering
# reset / increment / wrap; each function returns (bool, str).
# --------------------------------------------------------------------------- #
def test_counter_contract_produces_three_assertions_callable_against_stim(
    store: SqliteArtifactStore,
) -> None:
    """F19.5 AC: 'counter contract produces >=3 distinct assertions
    covering reset / increment / wrap; each assertion is a callable
    Python function consuming a (stim, observed) pair and returning
    (passed: bool, message: str).'"""
    router = _full_router()
    agent = AssertionGenAgent(router=router, store=store, design_id="d0")
    spec = agent.generate(_contract_counter(), _module_counter())

    # >=3 distinct assertions.
    assert len(spec.assertions) >= 3
    names = {a.name for a in spec.assertions}
    assert any("reset" in n for n in names), f"missing reset assertion in {names}"
    assert any("increment" in n for n in names), (
        f"missing increment assertion in {names}"
    )
    assert any("wrap" in n for n in names), f"missing wrap assertion in {names}"

    # Each function is callable and returns (bool, str).
    source = store.get_blob(spec.source)
    namespace: dict[str, object] = {}
    code = compile(source, "<assertions>", "exec")
    exec(code, namespace)

    for inv in spec.assertions:
        fn = namespace[inv.callsite]
        assert callable(fn)
        # Happy path: feed a stim/observed pair that satisfies the contract.
        happy_args = (
            [{"rst_n": 0, "en": 0}],  # stim
            [{"q": 0}],                # observed: reset behaviour holds
        )
        result = fn(happy_args)
        assert isinstance(result, tuple) and len(result) == 2
        passed, message = result
        assert isinstance(passed, bool)
        assert isinstance(message, str)

    # Negative path: feed a stim/observed that VIOLATES reset. The reset
    # assertion must reject it. This pins that the function actually
    # checks something (a stub returning (True, "") would pass the
    # callable check but fail this stronger probe).
    reset_inv = next(a for a in spec.assertions if "reset" in a.name)
    bad = (
        [{"rst_n": 0, "en": 0}],
        [{"q": 42}],   # contract says q must be 0 when rst_n is low
    )
    passed, message = namespace[reset_inv.callsite](bad)
    assert passed is False
    assert "q" in message  # mentions the violating signal
