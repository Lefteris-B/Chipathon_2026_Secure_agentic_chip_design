"""LiteLLMRouter — the deterministic :class:`ModelRouter` implementation.

Composes :func:`resolve_model` (policy, F3.2) with :class:`LiteLLMGateway`
(transport, F3.1) so the rest of the system calls ``router.generate(task, ...)``
and stays model-agnostic.

When ``n > 1``, the router runs N concurrent-style gateway calls with varied
seeds, dedupes by candidate text, then picks the best via the per-task
:class:`Scorer` registered with the router (F3.3). The scorer is a pure
``Callable[[str], float]`` — no model in the selection — so the choice of
candidate replays deterministically given the same inputs.

F11.5 adds transparent fallback: when the gateway raises
:class:`BackendUnreachableError` (DNS / connect-timeout / refused), the
router consults ``routing.fallback`` for an alternative registry entry,
retries the call against that entry, and records a
:class:`EventType.BACKEND_FALLBACK` audit event so the demotion is
queryable. A stderr warning fires on the first fallback per
:class:`LiteLLMRouter` instance.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from typing import Any

from chip_agent.design_state import (
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    TaskType,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.routing.gateway import (
    BackendUnreachableError,
    LiteLLMGateway,
    StreamChunk,
)
from chip_agent.routing.policy import (
    REPAIR_TASKS,
    FailureType,
    params_for,
    resolve_model,
)
from chip_agent.settings import ModelEntry, RoutingSettings

__all__ = ["LiteLLMRouter", "Scorer"]


# A Scorer is a pure function over a candidate's text. Higher is better.
# Ties resolve to first-encountered (stable order preserved by the dedupe pass).
Scorer = Callable[[str], float]


def _noop_scorer(_: str) -> float:
    """Default scorer: every candidate ties; first wins."""
    return 0.0


class LiteLLMRouter:
    """Implements :class:`chip_agent.design_state.ModelRouter`."""

    def __init__(
        self,
        *,
        routing: RoutingSettings,
        gateway: LiteLLMGateway | None = None,
        scorers: dict[TaskType, Scorer] | None = None,
        audit_log: SqliteAuditLog | None = None,
        design_id: str | None = None,
    ) -> None:
        self.routing = routing
        self._gw = gateway or LiteLLMGateway()
        self.scorers: dict[TaskType, Scorer] = dict(scorers or {})
        # F11.5: optional audit + per-run id so backend fallbacks are
        # recorded into the same provenance trail as everything else.
        self.audit_log = audit_log
        self.design_id = design_id
        self._fallback_announced = False

    def register_scorer(self, task: TaskType, scorer: Scorer) -> None:
        """Bind ``scorer`` to ``task`` for multi-candidate selection."""
        self.scorers[task] = scorer

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        """Resolve a model from the policy and generate one or more candidates.

        Reads from ``context``:

        * ``prompt`` (str, required) — the user prompt.
        * ``system`` (str, optional) — the system prompt.
        * ``seed`` (int, optional) — base seed. The i-th candidate uses
          ``seed + i`` so providers that honour the seed return distinct draws.
        * ``failure_type`` (:class:`FailureType` or str, optional) — explicit
          override; otherwise inferred from ``failure``.

        ``n`` defaults to ``binding.n`` from the routing config. Multi-candidate
        results are deduped by text (stable order); the highest-scored
        candidate becomes ``chosen`` and its :class:`ModelInvocation` rides on
        the returned :class:`GenerationResult`.
        """
        failure_type = self._infer_failure_type(task, failure, context)
        entry = resolve_model(
            task,
            failure_type=failure_type,
            escalation=escalation,
            routing=self.routing,
        )
        binding = params_for(
            task,
            failure_type=failure_type,
            escalation=escalation,
            routing=self.routing,
        )
        effective_n = max(1, n if n is not None else binding.n)
        prompt = str(context.get("prompt", ""))
        system_raw = context.get("system")
        system = str(system_raw) if system_raw is not None else None
        seed_raw = context.get("seed")
        base_seed = seed_raw if isinstance(seed_raw, int) else None

        if effective_n == 1:
            return self._single(
                entry, binding.temperature, prompt, system, base_seed, task=task,
            )
        return self._multi(
            task, entry, binding.temperature, prompt, system, base_seed, effective_n,
        )

    def stream(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
    ) -> Iterator[StreamChunk]:
        """Stream tokens for ``task`` through the resolved model.

        Resolution rules match :meth:`generate`: the policy + escalation pick
        a registry entry and a binding, and the binding's ``temperature`` is
        applied. Multi-candidate sampling (``n > 1``) is meaningless for an
        interactive stream — a single stream is always produced.

        Reads from ``context``:

        * ``prompt`` (str, required) — the user prompt.
        * ``system`` (str, optional) — the system prompt.
        * ``seed`` (int, optional) — seed forwarded to the backend.
        * ``failure_type`` (:class:`FailureType` or str, optional) — explicit
          override; otherwise inferred from ``failure``.

        Yields :class:`StreamChunk`s; the terminal chunk
        (``finish_reason != None``) carries a finalised
        :class:`ModelInvocation` mirroring what :meth:`generate` returns.
        """
        failure_type = self._infer_failure_type(task, failure, context)
        entry = resolve_model(
            task,
            failure_type=failure_type,
            escalation=escalation,
            routing=self.routing,
        )
        binding = params_for(
            task,
            failure_type=failure_type,
            escalation=escalation,
            routing=self.routing,
        )
        prompt = str(context.get("prompt", ""))
        system_raw = context.get("system")
        system = str(system_raw) if system_raw is not None else None
        seed_raw = context.get("seed")
        seed = seed_raw if isinstance(seed_raw, int) else None

        yield from self._stream_with_fallback(
            entry, prompt=prompt, system=system,
            temperature=binding.temperature, seed=seed, task=task,
        )

    def _stream_with_fallback(
        self,
        entry: ModelEntry,
        *,
        prompt: str,
        system: str | None,
        temperature: float,
        seed: int | None,
        task: TaskType,
    ) -> Iterator[StreamChunk]:
        """Stream with transparent fallback on :class:`BackendUnreachableError`.

        We try to pull the first chunk eagerly: connection-time failures
        (DNS / refused / timeout) surface there. If the primary is unreachable
        and a fallback is configured, the stream restarts against the
        fallback entry. Errors mid-stream are NOT retried — the partial
        output would be wasted and the caller has already seen chunks.
        """
        try:
            primary_iter = self._gw.stream(
                entry, prompt=prompt, system=system,
                temperature=temperature, seed=seed,
            )
            first = next(primary_iter)
        except BackendUnreachableError as e:
            fb = self._lookup_fallback(entry)
            if fb is None:
                raise
            fb_name, fb_entry = fb
            primary_name = self._registry_name(entry)
            self._announce_fallback(primary_name, fb_name, str(e))
            fb_iter = self._gw.stream(
                fb_entry, prompt=prompt, system=system,
                temperature=temperature, seed=seed,
            )
            self._emit_fallback_audit(
                task=task, primary_name=primary_name, primary=entry,
                fallback_name=fb_name, fallback=fb_entry, reason=str(e),
            )
            yield from fb_iter
            return
        except StopIteration:
            return
        yield first
        yield from primary_iter

    def _single(
        self,
        entry: ModelEntry,
        temperature: float,
        prompt: str,
        system: str | None,
        seed: int | None,
        *,
        task: TaskType,
    ) -> GenerationResult:
        text, invocation = self._call_with_fallback(
            entry, prompt=prompt, system=system,
            temperature=temperature, seed=seed, task=task,
        )
        return GenerationResult(
            candidates=[text], chosen=text, invocation=invocation,
        )

    def _multi(
        self,
        task: TaskType,
        entry: ModelEntry,
        temperature: float,
        prompt: str,
        system: str | None,
        base_seed: int | None,
        n: int,
    ) -> GenerationResult:
        scorer = self.scorers.get(task, _noop_scorer)
        # Vary the per-candidate seed so providers that honour it return draws.
        seeds: list[int | None] = (
            [base_seed + i for i in range(n)] if base_seed is not None else [None] * n
        )
        # Dedupe by text, preserving first-call order; keep the invocation that
        # produced each unique candidate so the chosen one's call is traceable.
        candidates: list[str] = []
        invocations: dict[str, ModelInvocation] = {}
        for s in seeds:
            text, invocation = self._call_with_fallback(
                entry, prompt=prompt, system=system,
                temperature=temperature, seed=s, task=task,
            )
            if text not in invocations:
                candidates.append(text)
                invocations[text] = invocation
        chosen = _best(candidates, scorer)
        return GenerationResult(
            candidates=candidates,
            chosen=chosen,
            invocation=invocations[chosen],
        )

    # ------------------------------------------------------------------ fallback
    def _call_with_fallback(
        self,
        entry: ModelEntry,
        *,
        prompt: str,
        system: str | None,
        temperature: float,
        seed: int | None,
        task: TaskType,
    ) -> tuple[str, ModelInvocation]:
        """One gateway call, transparently retrying on
        :class:`BackendUnreachableError` via ``routing.fallback``."""
        try:
            return self._gw.call(
                entry, prompt=prompt, system=system,
                temperature=temperature, seed=seed,
            )
        except BackendUnreachableError as e:
            fb = self._lookup_fallback(entry)
            if fb is None:
                raise
            fb_name, fb_entry = fb
            primary_name = self._registry_name(entry)
            self._announce_fallback(primary_name, fb_name, str(e))
            text, invocation = self._gw.call(
                fb_entry, prompt=prompt, system=system,
                temperature=temperature, seed=seed,
            )
            self._emit_fallback_audit(
                task=task, primary_name=primary_name, primary=entry,
                fallback_name=fb_name, fallback=fb_entry, reason=str(e),
            )
            return text, invocation

    def _lookup_fallback(
        self, primary: ModelEntry,
    ) -> tuple[str, ModelEntry] | None:
        """Resolve the fallback ``(name, entry)`` for ``primary``, or ``None``.

        Returns ``None`` when no fallback is configured for the registry
        name that owns ``primary``, so the original error is propagated.
        """
        try:
            primary_name = self._registry_name(primary)
        except ValueError:
            return None
        fb_name = self.routing.fallback.get(primary_name)
        if fb_name is None:
            return None
        fb_entry = self.routing.registry.get(fb_name)
        if fb_entry is None:  # pragma: no cover — settings validator forbids this
            return None
        return fb_name, fb_entry

    def _registry_name(self, entry: ModelEntry) -> str:
        """Reverse-lookup ``entry`` to its registry name (identity match)."""
        for name, e in self.routing.registry.items():
            if e is entry:
                return name
        # Fallback to value-equality if identity didn't match (defensive).
        for name, e in self.routing.registry.items():
            if e == entry:
                return name
        raise ValueError(
            f"ModelEntry {entry!r} not found in routing.registry",
        )

    def _announce_fallback(
        self, primary_name: str, fb_name: str, reason: str,
    ) -> None:
        """Print a one-time stderr warning so operators see the demotion live."""
        if self._fallback_announced:
            return
        self._fallback_announced = True
        print(
            f"chip-agent: WARNING — model {primary_name!r} unreachable "
            f"({reason}); falling back to {fb_name!r} for this run.",
            file=sys.stderr,
        )

    def _emit_fallback_audit(
        self,
        *,
        task: TaskType,
        primary_name: str,
        primary: ModelEntry,
        fallback_name: str,
        fallback: ModelEntry,
        reason: str,
    ) -> None:
        if self.audit_log is None or self.design_id is None:
            return
        self.audit_log.append(
            design_id=self.design_id,
            event_type=EventType.BACKEND_FALLBACK,
            payload={
                "task": task.value,
                "primary": {
                    "name": primary_name,
                    "provider": primary.provider,
                    "model": primary.model,
                },
                "fallback": {
                    "name": fallback_name,
                    "provider": fallback.provider,
                    "model": fallback.model,
                },
                "reason": reason,
            },
        )

    @staticmethod
    def _infer_failure_type(
        task: TaskType,
        failure: FailureDiagnosis | None,
        context: dict[str, Any],
    ) -> FailureType:
        override = context.get("failure_type")
        if isinstance(override, FailureType):
            return override
        if isinstance(override, str):
            try:
                return FailureType(override)
            except ValueError:
                pass
        if task in REPAIR_TASKS:
            # A FailureDiagnosis exists only when the semantic check produced
            # a typed trace — that's the outer-loop signal.
            return FailureType.SEMANTIC if failure is not None else FailureType.SYNTACTIC
        return FailureType.NONE


def _best(candidates: list[str], scorer: Scorer) -> str:
    """Pick the highest-scored candidate; ties resolve to first-encountered."""
    if not candidates:
        raise RuntimeError("router: no candidates to pick from")
    best_idx = 0
    best_score = scorer(candidates[0])
    for i in range(1, len(candidates)):
        s = scorer(candidates[i])
        if s > best_score:
            best_score = s
            best_idx = i
    return candidates[best_idx]
