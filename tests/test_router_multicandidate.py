"""F3.3 acceptance: multi-candidate sampling returns N distinct candidates
and lets a per-task scorer pick the best."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from chip_agent.design_state import TaskType
from chip_agent.routing import LiteLLMGateway, LiteLLMRouter
from chip_agent.routing.gateway import CompletionResult, CompletionStreamChunk
from chip_agent.settings import LoopBinding, ModelEntry, RoutingSettings, TaskBinding


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _routing(*, rtl_gen_n: int = 4) -> RoutingSettings:
    return RoutingSettings(
        registry={
            "local-coder": ModelEntry(
                provider="ollama", model="qwen2.5-coder:7b",
                endpoint="http://localhost:11434",
            ),
            "frontier": ModelEntry(provider="anthropic", model="claude-opus-4-7"),
        },
        loops={
            "inner": LoopBinding(model="local-coder", temperature=0.4, n=3),
            "outer": LoopBinding(model="frontier", temperature=0.2, n=1),
        },
        tasks={
            "rtl_gen": TaskBinding(model="local-coder", temperature=0.8, n=rtl_gen_n),
            "diagnose": TaskBinding(model="frontier", temperature=0.2, n=1),
        },
    )


@dataclass
class CyclingBackend:
    """Returns texts from a fixed list, cycling once exhausted."""

    texts: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> CompletionResult:
        idx = len(self.calls) % len(self.texts)
        self.calls.append({
            "model": model, "messages": list(messages),
            "temperature": temperature, "seed": seed, "api_base": api_base,
        })
        return CompletionResult(self.texts[idx], 10, 5, 0.001)

    def stream(
        self, *, model: str, messages: list[dict[str, str]],
        temperature: float, seed: int | None, api_base: str | None,
    ) -> Iterator[CompletionStreamChunk]:
        raise NotImplementedError


def _build_router(
    *, texts: list[str], scorers: dict[TaskType, object] | None = None,
    rtl_gen_n: int = 4,
) -> tuple[LiteLLMRouter, CyclingBackend]:
    backend = CyclingBackend(texts=texts)
    gw = LiteLLMGateway(backend=backend)
    router = LiteLLMRouter(
        routing=_routing(rtl_gen_n=rtl_gen_n),
        gateway=gw,
        scorers=scorers,  # type: ignore[arg-type]
    )
    return router, backend


# --------------------------------------------------------------------------- #
# AC: returns N distinct candidates
# --------------------------------------------------------------------------- #
def test_n_calls_made_when_n_explicit() -> None:
    router, backend = _build_router(texts=["a", "b", "c", "d"])
    result = router.generate(
        TaskType.RTL_GEN,
        context={"prompt": "module counter;", "seed": 100},
        n=4,
    )
    assert len(backend.calls) == 4
    assert sorted(result.candidates) == ["a", "b", "c", "d"]
    # Distinct seeds drive each call when a base seed is provided.
    assert [c["seed"] for c in backend.calls] == [100, 101, 102, 103]


def test_n_defaults_to_task_binding_n() -> None:
    # tasks.rtl_gen.n = 4 in _routing.
    router, backend = _build_router(texts=["x", "y", "z", "w"], rtl_gen_n=4)
    router.generate(TaskType.RTL_GEN, context={"prompt": "p"})
    assert len(backend.calls) == 4


def test_duplicates_deduped_preserving_first_order() -> None:
    # The backend returns "x" three times then "y"; only "x" then "y" survive.
    router, backend = _build_router(texts=["x", "x", "x", "y"])
    result = router.generate(
        TaskType.RTL_GEN, context={"prompt": "p"}, n=4,
    )
    assert result.candidates == ["x", "y"]
    assert len(backend.calls) == 4  # still ran 4 calls


# --------------------------------------------------------------------------- #
# AC: best-by-score selectable
# --------------------------------------------------------------------------- #
def test_scorer_picks_best_candidate() -> None:
    # Prefer the candidate that contains "good".
    def scorer(c: str) -> float:
        return 1.0 if "good" in c else 0.0

    router, _backend = _build_router(
        texts=["bad-a", "good-b", "bad-c", "good-d"],
        scorers={TaskType.RTL_GEN: scorer},
    )
    result = router.generate(TaskType.RTL_GEN, context={"prompt": "p"}, n=4)
    assert result.chosen in {"good-b", "good-d"}
    # Tie-break: first encountered wins.
    assert result.chosen == "good-b"
    assert result.candidates == ["bad-a", "good-b", "bad-c", "good-d"]


def test_scorer_ties_resolve_to_first() -> None:
    router, _backend = _build_router(
        texts=["a", "b", "c"],
        scorers={TaskType.RTL_GEN: lambda _c: 0.5},  # all tied
    )
    result = router.generate(TaskType.RTL_GEN, context={"prompt": "p"}, n=3)
    assert result.chosen == "a"


def test_noop_scorer_picks_first_candidate() -> None:
    router, _backend = _build_router(texts=["alpha", "beta", "gamma"])
    result = router.generate(TaskType.RTL_GEN, context={"prompt": "p"}, n=3)
    # Default scorer ties everything; first wins.
    assert result.chosen == "alpha"


def test_scorer_uses_continuous_scores() -> None:
    # Score by candidate length; longest wins.
    router, _backend = _build_router(
        texts=["a", "longer", "longest!"],
        scorers={TaskType.RTL_GEN: len},
    )
    result = router.generate(TaskType.RTL_GEN, context={"prompt": "p"}, n=3)
    assert result.chosen == "longest!"


def test_register_scorer_after_construction() -> None:
    router, _backend = _build_router(texts=["x", "yy", "zzz"])
    router.register_scorer(TaskType.RTL_GEN, lambda c: float(len(c)))
    result = router.generate(TaskType.RTL_GEN, context={"prompt": "p"}, n=3)
    assert result.chosen == "zzz"


# --------------------------------------------------------------------------- #
# Invocation tracking
# --------------------------------------------------------------------------- #
def test_chosen_invocation_is_returned() -> None:
    # The invocation belonging to the *chosen* candidate is the one in the
    # GenerationResult — not, e.g., the last call.
    def scorer(c: str) -> float:
        return 1.0 if c == "winner" else 0.0

    router, backend = _build_router(
        texts=["loser-a", "winner", "loser-c"],
        scorers={TaskType.RTL_GEN: scorer},
    )
    result = router.generate(TaskType.RTL_GEN, context={"prompt": "p"}, n=3)
    assert result.chosen == "winner"
    # All three calls share the same model and provider here, but the chosen
    # candidate is the second call's invocation. (Verify provider/model land
    # on the invocation either way.)
    assert result.invocation.provider == "ollama"
    assert result.invocation.model == "qwen2.5-coder:7b"
    assert len(backend.calls) == 3


# --------------------------------------------------------------------------- #
# Single-call path still works (F3.2 regression)
# --------------------------------------------------------------------------- #
def test_n_one_uses_single_call_path() -> None:
    router, backend = _build_router(texts=["only"])
    result = router.generate(TaskType.DIAGNOSE, context={"prompt": "p"})  # binding.n=1
    assert len(backend.calls) == 1
    assert result.candidates == ["only"]
    assert result.chosen == "only"


def test_n_zero_or_negative_treated_as_one() -> None:
    router, backend = _build_router(texts=["only"])
    result = router.generate(TaskType.DIAGNOSE, context={"prompt": "p"}, n=0)
    assert len(backend.calls) == 1
    assert result.candidates == ["only"]


# --------------------------------------------------------------------------- #
# No base seed -> per-call seeds stay None (provider chooses)
# --------------------------------------------------------------------------- #
def test_no_seed_means_no_per_candidate_seed() -> None:
    router, backend = _build_router(texts=["a", "b", "c", "d"])
    router.generate(TaskType.RTL_GEN, context={"prompt": "p"}, n=4)
    assert [c["seed"] for c in backend.calls] == [None, None, None, None]


# --------------------------------------------------------------------------- #
# Scorer is task-scoped: a DIAGNOSE scorer doesn't affect RTL_GEN selection.
# --------------------------------------------------------------------------- #
def test_scorer_is_task_scoped() -> None:
    def diagnose_scorer(c: str) -> float:
        return 1.0 if c == "z" else 0.0

    router, _backend = _build_router(
        texts=["a", "b", "z", "c"],
        scorers={TaskType.DIAGNOSE: diagnose_scorer},
    )
    # RTL_GEN has no scorer -> noop -> first candidate.
    result = router.generate(TaskType.RTL_GEN, context={"prompt": "p"}, n=4)
    assert result.chosen == "a"
