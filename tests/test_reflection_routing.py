"""F19.9 acceptance: ReflectionRoutingAgent picks recovery routes.

Mirrors :mod:`tests.test_contract_extraction`'s :class:`StubRouter`
pattern: the agent gets a canned JSON ``chosen`` string from the
router; the test asserts the typed :class:`ReflectionRoute` that
emerges. The agent never raises on bad model output — every
defensive path falls back to ESCALATE_HUMAN with a non-empty
``reason``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.agents.reflection_routing import (
    ReflectionRoutingAgent,
    ReflectionRoutingError,
)
from chip_agent.design_state import (
    ArtifactRef,
    AssertionSpec,
    BehaviorInvariant,
    ContractArtifact,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    OracleArtifact,
    Provenance,
    ReflectionRouteKind,
    RTLArtifact,
    Stage,
    StructuredInvariant,
    TaskType,
)
from chip_agent.store import SqliteArtifactStore


# --------------------------------------------------------------------------- #
# StubRouter — copy of the F19.3 pattern.
# --------------------------------------------------------------------------- #
@dataclass
class StubRouter:
    chosen: str
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic",
            model="claude-opus-4-7",
            temperature=0.0,
            seed=None,
            prompt_tokens=240,
            completion_tokens=64,
            cost_usd=0.0021,
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
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


def _make_diagnosis() -> FailureDiagnosis:
    return FailureDiagnosis(
        artifact_id="d0.counter.diagnosis",
        design_id="d0",
        module_id="counter",
        target_module="counter",
        cycle=4,
        failing_signal="q",
        expected="5",
        actual="10",
        nl_summary="Counter q advanced by 2 instead of 1 from cycle 3 to 4.",
        provenance=Provenance(produced_by=Stage.RTL),
    )


def _make_contract(store: SqliteArtifactStore) -> ContractArtifact:
    c = ContractArtifact(
        artifact_id="d0.counter.contract",
        design_id="d0",
        module_id="counter",
        behavior_invariants=[
            BehaviorInvariant(
                name="increment_by_one",
                description="q advances by 1 per enabled cycle",
                condition="(en && rst_n) -> next(q) == (q + 1) mod 256",
            ),
        ],
        ambiguity_notes=["wrap mod 256 assumed"],
        provenance=Provenance(produced_by=Stage.PLAN, agent="contract_extractor"),
    )
    store.put(c)
    loaded = store.get_by_id(c.artifact_id)
    assert isinstance(loaded, ContractArtifact)
    return loaded


def _make_oracle(store: SqliteArtifactStore) -> OracleArtifact:
    blob = store.put_blob(b"def reference(stim): return []\n",
                          media_type="text/x-python")
    o = OracleArtifact(
        artifact_id="d0.counter.oracle",
        design_id="d0",
        module_id="counter",
        source=blob,
        reference_fn_name="reference",
        provenance=Provenance(produced_by=Stage.PLAN, agent="oracle_gen"),
    )
    store.put(o)
    loaded = store.get_by_id(o.artifact_id)
    assert isinstance(loaded, OracleArtifact)
    return loaded


def _make_assertion_spec(store: SqliteArtifactStore) -> AssertionSpec:
    blob = store.put_blob(b"def assert_x(args): return (True, '')\n",
                          media_type="text/x-python")
    s = AssertionSpec(
        artifact_id="d0.counter.assertions",
        design_id="d0",
        module_id="counter",
        source=blob,
        assertions=[
            StructuredInvariant(
                name="increment_by_one", callsite="assert_x",
                description="q increments by 1",
            ),
        ],
        provenance=Provenance(produced_by=Stage.PLAN, agent="assertion_gen"),
    )
    store.put(s)
    loaded = store.get_by_id(s.artifact_id)
    assert isinstance(loaded, AssertionSpec)
    return loaded


def _make_rtl(store: SqliteArtifactStore) -> RTLArtifact:
    blob = store.put_blob(b"module counter(); endmodule\n",
                          media_type="text/x-verilog")
    r = RTLArtifact(
        artifact_id="d0.counter.rtl",
        design_id="d0",
        module_id="counter",
        top_module="counter",
        source=blob,
        provenance=Provenance(produced_by=Stage.RTL),
    )
    store.put(r)
    loaded = store.get_by_id(r.artifact_id)
    assert isinstance(loaded, RTLArtifact)
    return loaded


def _classify(
    router: StubRouter,
    *,
    store: SqliteArtifactStore,
    sibling_module_ids: list[str] | None = None,
    attempts_so_far: int = 0,
    max_attempts: int = 2,
):
    agent = ReflectionRoutingAgent(router=router, design_id="d0")
    return agent.classify(
        _make_diagnosis(),
        contract=_make_contract(store),
        oracle=_make_oracle(store),
        assertion_spec=_make_assertion_spec(store),
        rtl=_make_rtl(store),
        sibling_module_ids=sibling_module_ids or [],
        attempts_so_far=attempts_so_far,
        max_attempts=max_attempts,
    )


# --------------------------------------------------------------------------- #
# Happy paths — one per route kind
# --------------------------------------------------------------------------- #
def test_classify_returns_re_extract_contract_for_contract_bug_diagnosis(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(
        chosen='{"route": "re_extract_contract", '
               '"reason": "contract says active-high but spec is active-low"}',
    )
    route = _classify(router, store=store)
    assert route.kind is ReflectionRouteKind.RE_EXTRACT_CONTRACT
    assert route.target_module is None
    assert "active-low" in route.reason


def test_classify_returns_regen_current_rtl_for_rtl_bug_diagnosis(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(
        chosen='{"route": "regen_current_rtl", '
               '"reason": "off-by-one on q increment"}',
    )
    route = _classify(router, store=store)
    assert route.kind is ReflectionRouteKind.REGEN_CURRENT_RTL
    assert route.target_module is None
    assert "off-by-one" in route.reason


def test_classify_target_module_for_sibling_revisit(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(
        chosen='{"route": "revisit_sibling_rtl", '
               '"target_module": "baud_gen", '
               '"reason": "rx_sync consumes a bad baud tick"}',
    )
    route = _classify(
        router, store=store,
        sibling_module_ids=["baud_gen", "rx_fsm"],
    )
    assert route.kind is ReflectionRouteKind.REVISIT_SIBLING_RTL
    assert route.target_module == "baud_gen"


# --------------------------------------------------------------------------- #
# Defensive paths — every bad input becomes ESCALATE_HUMAN with a reason
# --------------------------------------------------------------------------- #
def test_classify_returns_escalate_human_when_attempts_exhausted(
    store: SqliteArtifactStore,
) -> None:
    """``attempts_so_far >= max_attempts`` short-circuits without
    calling the router."""
    router = StubRouter(chosen='{"route": "regen_current_rtl"}')
    route = _classify(
        router, store=store, attempts_so_far=2, max_attempts=2,
    )
    assert route.kind is ReflectionRouteKind.ESCALATE_HUMAN
    assert "exhausted" in route.reason.lower()
    assert len(router.calls) == 0


def test_classify_falls_back_to_escalate_human_on_unparseable_json(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen="this is not json at all")
    route = _classify(router, store=store)
    assert route.kind is ReflectionRouteKind.ESCALATE_HUMAN
    assert "unparseable" in route.reason


def test_classify_falls_back_to_escalate_human_on_unknown_kind(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen='{"route": "do_something_weird"}')
    route = _classify(router, store=store)
    assert route.kind is ReflectionRouteKind.ESCALATE_HUMAN
    assert "unknown route value" in route.reason


def test_classify_rejects_hallucinated_sibling_module(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(
        chosen='{"route": "revisit_sibling_rtl", '
               '"target_module": "not_a_sibling"}',
    )
    route = _classify(
        router, store=store, sibling_module_ids=["baud_gen"],
    )
    assert route.kind is ReflectionRouteKind.ESCALATE_HUMAN
    assert "not_a_sibling" in route.reason


# --------------------------------------------------------------------------- #
# Plumbing — the agent dispatches to the right task + inlines lineage
# --------------------------------------------------------------------------- #
def test_classify_dispatches_to_reflection_routing_task(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen='{"route": "regen_current_rtl"}')
    _classify(router, store=store)
    assert len(router.calls) == 1
    assert router.calls[0]["task"] is TaskType.REFLECTION_ROUTING


def test_classify_user_prompt_inlines_diagnosis_and_lineage(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(chosen='{"route": "regen_current_rtl"}')
    _classify(
        router, store=store,
        sibling_module_ids=["baud_gen", "rx_fsm"],
    )
    prompt = router.calls[0]["context"]["prompt"]
    # Diagnosis structured fields.
    assert "cycle: 4" in prompt
    assert "'q'" in prompt
    assert "'5'" in prompt
    assert "'10'" in prompt
    # Contract invariant + ambiguity_notes.
    assert "increment_by_one" in prompt
    assert "wrap mod 256" in prompt
    # Sibling list.
    assert "baud_gen" in prompt
    assert "rx_fsm" in prompt


def test_classify_persists_reason_into_route_object(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(
        chosen='{"route": "regen_current_rtl", '
               '"reason": "the contract is fine; rerun RTL"}',
    )
    route = _classify(router, store=store)
    assert route.reason == "the contract is fine; rerun RTL"


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #
def test_agent_rejects_empty_design_id() -> None:
    router = StubRouter(chosen="{}")
    with pytest.raises(ReflectionRoutingError, match="design_id"):
        ReflectionRoutingAgent(router=router, design_id="")


# ``ArtifactRef`` import is silenced by a usage below so the type
# resolves at import time even on the no-content-hash side.
_ = ArtifactRef
