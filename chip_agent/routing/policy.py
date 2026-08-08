"""Deterministic model-selection policy (F3.2).

The single locus of model choice. ``resolve_model`` is a pure function of
``(task, failure_type, escalation, routing)`` and never touches the network
— that's what keeps the rest of the system testable without live models.

Repair tasks walk through the two-loop bindings (``routing.loops.inner`` /
``routing.loops.outer``); generation tasks walk through ``routing.tasks``.
Escalation can bump a syntactic-loop repair onto the outer loop, matching
``decide_transition``'s ``ESCALATE`` semantics (see ``docs/control_graph.md``).
"""

from __future__ import annotations

from enum import StrEnum

from chip_agent.design_state import EscalationLevel, TaskType
from chip_agent.settings import (
    LoopBinding,
    ModelEntry,
    RoutingSettings,
    TaskBinding,
)

__all__ = [
    "EXHAUSTED_FALLBACK_TASK",
    "LOOP_SLOTS",
    "REPAIR_TASKS",
    "FailureType",
    "ResolutionError",
    "loop_for",
    "params_for",
    "resolve_model",
]


# F12.5: when a repair task hits ``EscalationLevel.EXHAUSTED`` (outer-loop
# budget burned through), the resolver re-uses whichever model is bound to
# this task slot — by convention the project's frontier. The plan task is
# always present in any valid routing config (the settings validator pins
# it as required) so we always have a sensible fallback target.
EXHAUSTED_FALLBACK_TASK = "plan"


class FailureType(StrEnum):
    """Why the repair loop is running.

    ``NONE`` is the generation-task default (no failure to repair).
    ``SYNTACTIC`` routes through the inner loop; everything else routes
    through the outer loop (per ``docs/model_routing.md``).
    """

    NONE = "none"
    SYNTACTIC = "syntactic"
    SEMANTIC = "semantic"
    PHYSICAL = "physical"
    TIMING = "timing"


class ResolutionError(ValueError):
    """The policy cannot route a request: missing binding, HUMAN escalation, etc."""


# Tasks whose model is picked from ``routing.loops`` rather than ``routing.tasks``.
REPAIR_TASKS: frozenset[TaskType] = frozenset({TaskType.RTL_REPAIR})

LOOP_SLOTS: dict[EscalationLevel, str] = {
    EscalationLevel.INNER: "inner",
    EscalationLevel.OUTER: "outer",
}


_OUTER_FAILURES: frozenset[FailureType] = frozenset({
    FailureType.SEMANTIC,
    FailureType.PHYSICAL,
    FailureType.TIMING,
})


def loop_for(failure_type: FailureType) -> EscalationLevel:
    """Which loop slot a failure routes into.

    SYNTACTIC and NONE go to the inner loop (cheap repair); semantic / physical
    / timing failures route through the outer loop (typed FailureDiagnosis + a
    semantically-reasoning model).
    """
    return EscalationLevel.OUTER if failure_type in _OUTER_FAILURES else EscalationLevel.INNER


def resolve_model(
    task: TaskType,
    *,
    failure_type: FailureType = FailureType.NONE,
    escalation: EscalationLevel = EscalationLevel.INNER,
    routing: RoutingSettings,
) -> ModelEntry:
    """Return the registry entry the caller should generate against.

    Implements the policy from ``docs/model_routing.md``:
    ``escalation = max(loop_for(failure_type), escalation)``, so a syntactic
    repair that has already escalated lands on the outer loop's model.

    F12.5: at ``escalation=EXHAUSTED`` (one rung above OUTER), repair tasks
    route to the model bound to ``routing.tasks.plan`` instead of any loop
    binding. The convention is "if the local outer model can't repair this
    in budget, give the frontier one shot before escalating to HUMAN."
    """
    if escalation is EscalationLevel.HUMAN:
        raise ResolutionError(
            "HUMAN escalation does not call a model; the control graph hands off"
        )

    if task in REPAIR_TASKS and escalation is EscalationLevel.EXHAUSTED:
        # F12.5: outer-loop budget burned through. Route to the model bound
        # to the plan task — by convention the frontier model.
        fallback = routing.tasks.get(EXHAUSTED_FALLBACK_TASK)
        if fallback is None:
            raise ResolutionError(
                f"EXHAUSTED escalation needs routing.tasks."
                f"{EXHAUSTED_FALLBACK_TASK!r} but it is not bound; "
                f"available: {sorted(routing.tasks)}"
            )
        registry_name = fallback.model
    elif task in REPAIR_TASKS:
        loop = max(loop_for(failure_type), escalation)
        slot = LOOP_SLOTS[loop]
        loop_binding = routing.loops.get(slot)
        if loop_binding is None:
            raise ResolutionError(
                f"no routing.loops.{slot} bound for task {task.value!r}"
            )
        registry_name = loop_binding.model
    else:
        task_binding = routing.tasks.get(task.value)
        if task_binding is None:
            raise ResolutionError(
                f"no routing.tasks.{task.value} bound (generation task {task.value!r})"
            )
        registry_name = task_binding.model

    entry = routing.registry.get(registry_name)
    if entry is None:
        raise ResolutionError(
            f"binding for {task.value!r} points at missing registry entry "
            f"{registry_name!r}; available: {sorted(routing.registry)}"
        )
    return entry


def params_for(
    task: TaskType,
    *,
    failure_type: FailureType = FailureType.NONE,
    escalation: EscalationLevel = EscalationLevel.INNER,
    routing: RoutingSettings,
) -> LoopBinding | TaskBinding:
    """Return the binding (carrying temperature / n) that resolves ``task``.

    Tools above the router (multi-candidate sampling, cost guard, etc.) read
    temperature / n from here so the policy stays the single locus of choice.
    """
    if escalation is EscalationLevel.HUMAN:
        raise ResolutionError(
            "HUMAN escalation does not call a model; the control graph hands off"
        )
    if task in REPAIR_TASKS and escalation is EscalationLevel.EXHAUSTED:
        # F12.5: re-use the plan binding (temperature + n) for the frontier
        # fallback attempt. Plan tasks typically run at low temperature with
        # n=1, which matches what we want for the one-shot final attempt.
        fallback = routing.tasks.get(EXHAUSTED_FALLBACK_TASK)
        if fallback is None:
            raise ResolutionError(
                f"EXHAUSTED escalation needs routing.tasks."
                f"{EXHAUSTED_FALLBACK_TASK!r} but it is not bound",
            )
        return fallback
    if task in REPAIR_TASKS:
        loop = max(loop_for(failure_type), escalation)
        slot = LOOP_SLOTS[loop]
        loop_binding = routing.loops.get(slot)
        if loop_binding is None:
            raise ResolutionError(f"no routing.loops.{slot} bound for task {task.value!r}")
        return loop_binding
    task_binding = routing.tasks.get(task.value)
    if task_binding is None:
        raise ResolutionError(f"no routing.tasks.{task.value} bound")
    return task_binding
