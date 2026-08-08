"""LangGraph state graph over :class:`DesignState` (F5.1 + F5.3 + F9.1).

This module wires the macro pipeline (spec_intake -> planner -> rtl -> synth
-> physical -> signoff -> await_human -> gdsii_emit) into a LangGraph
``StateGraph`` with disk-backed checkpointing.

Two node modes coexist:

* **Placeholder mode** (F5.1 + F5.3 — when ``stage_context is None``).
  Each stage node is an ``_advance(stage)`` closure that only updates
  ``current_stage`` (and ``status`` at the terminal). The F5.1 AC tests
  rely on this: a fresh graph + saver loaded against the same ``thread_id``
  must return the **identical** :class:`DesignState`.
* **Driver mode** (F9.1 — when a :class:`StageContext` is passed).
  RTL / SYNTH / PHYSICAL / SIGNOFF / GDSII nodes delegate to the real
  stage drivers via the context's optional driver fields. The SPEC and
  PLAN nodes stay placeholders in both modes — those artifacts are
  produced upstream by the CLI / future planner agent.

F5.3 adds a dedicated ``await_human`` node before ``gdsii_emit`` and a
compile-time ``interrupt_after`` on it. When the graph reaches it, it
writes ``status = AWAITING_HUMAN`` and the runtime persists + halts. A
caller resumes with ``graph.invoke(None, config)``; the framework picks
up from after ``await_human`` and runs ``gdsii_emit``, which (in
placeholder mode) flips ``status`` to ``COMPLETED`` directly, or (in
driver mode) calls the GDSII driver and the handler does the same.
"""

from __future__ import annotations

import graphlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from chip_agent.agents.stim_ramp import build_ramp_stim
from chip_agent.design_state import (
    DesignState,
    DesignStatus,
    FailureDiagnosis,
    HumanHint,
    PendingHumanRepair,
    PhysicalRepairRouteKind,
    Provenance,
    ReflectionRouteKind,
    Stage,
    StageStatus,
)
from chip_agent.graph.blackboard import get_or_create_stage_state
from chip_agent.graph.gdsii_handler import apply_gdsii_outcome
from chip_agent.graph.heads import (
    clear_module_stage_head,
    load_assertion_spec_head,
    load_contract_head,
    load_layout_head,
    load_netlist_head,
    load_oracle_head,
    load_plan,
    load_spec_head,
    set_module_stage_head,
)
from chip_agent.graph.human_repair import (
    grant_human_retry,
    hint_prompt_section,
    route_from_hint,
)
from chip_agent.graph.physical_handler import apply_physical_outcome
from chip_agent.graph.rtl_handler import apply_rtl_outcome
from chip_agent.graph.signoff_handler import apply_signoff_outcome
from chip_agent.graph.stage_context import StageContext
from chip_agent.graph.synth_handler import apply_synth_outcome
from chip_agent.obs.audit_log import EventType
from chip_agent.obs.tracing import SpanKind
from chip_agent.store.sqlite_store import StoreError
from chip_agent.tools.librelane import apply_repair_delta

__all__ = [
    "HUMAN_REPAIR_APPLY_NODE",
    "HUMAN_REPAIR_NODE",
    "HUMAN_REVIEW_NODE",
    "STAGE_ORDER",
    "build_design_graph",
    "open_sqlite_checkpointer",
    "request_human_review",
]


# The macro flow LangGraph wires in order. Conditional edges + RTL fan-out
# arrive in F5.4; F5.1/F5.3 is a linear spine just rich enough to exercise
# the checkpointer and the human-interrupt pause.
STAGE_ORDER: list[Stage] = [
    Stage.SPEC,
    Stage.PLAN,
    # F19.7: M19 Phase 1 stages inserted between PLAN and RTL.
    # ORACLE and ASSERTIONS run as parallel branches after CONTRACT
    # and fan in at ORACLE_VERIFICATION. The list order matters only
    # for the placeholder mode; the real edges are hand-wired below.
    Stage.CONTRACT,
    Stage.ORACLE,
    Stage.ASSERTIONS,
    Stage.ORACLE_VERIFICATION,
    Stage.RTL,
    Stage.SYNTH,
    Stage.PHYSICAL,
    Stage.SIGNOFF,
    Stage.GDSII,
]


_NODE_NAMES: dict[Stage, str] = {
    Stage.SPEC: "spec_intake",
    Stage.PLAN: "planner",
    Stage.CONTRACT: "contract",
    Stage.ORACLE: "oracle",
    Stage.ASSERTIONS: "assertions",
    Stage.ORACLE_VERIFICATION: "oracle_verification",
    Stage.RTL: "rtl",
    Stage.SYNTH: "synth",
    Stage.PHYSICAL: "physical",
    Stage.SIGNOFF: "signoff",
    Stage.GDSII: "gdsii_emit",
}


# Public seam: the name of the human-review node. F5.4's conditional edges
# route to this name when ``decide_transition`` returns ``Transition.HUMAN``.
HUMAN_REVIEW_NODE = "await_human"

# F23.5 (Option B): the interactive-repair interrupt pair. ``human_repair``
# is a distinct pause point from ``await_human`` (which is a SUCCESS pause
# into gdsii_emit); an RTL escalation is a FAILURE pause that must resume
# back into RTL once the operator injects a hint. ``human_repair_apply``
# runs on resume, distils the injected transcript, and re-enters the loop.
HUMAN_REPAIR_NODE = "human_repair"
HUMAN_REPAIR_APPLY_NODE = "human_repair_apply"


def _advance(stage: Stage, *, terminal: bool = False) -> Callable[[DesignState], dict[str, Any]]:
    """Placeholder node: marks the stage as 'currently here' (and DONE at the end).

    Real stage handlers (RTLStageHandler etc.) replace these when the
    caller passes a :class:`StageContext`. The update shape — return a
    dict the framework merges — is what real handlers also use, so
    downstream graph wiring stays unchanged.
    """
    def _node(state: DesignState) -> dict[str, Any]:
        update: dict[str, Any] = {"current_stage": stage}
        if terminal:
            # Terminal stage closes out the run — also clears any AWAITING_HUMAN
            # the pre-GDSII pause may have set on the previous tick.
            update["status"] = DesignStatus.COMPLETED
        return update
    _node.__name__ = f"_advance_to_{stage.value}"
    return _node


def _await_human(state: DesignState) -> dict[str, Any]:
    """Pause point. Marks the run as ``AWAITING_HUMAN`` so the next checkpoint
    captures that explicitly; the compile-time ``interrupt_after`` halts the
    runtime right after this node, leaving callers in control."""
    return {"status": DesignStatus.AWAITING_HUMAN}


def request_human_review() -> dict[str, Any]:
    """Update-dict helper for stage handlers that want to escalate to HUMAN.

    A handler that exhausts its repair budget at ``OUTER`` returns this dict
    merged with a LangGraph ``Command(goto=HUMAN_REVIEW_NODE, update=...)``
    so the graph re-enters the same pause point used pre-GDSII. F5.4 wires
    the real conditional edges; F5.3 just exposes the seam.
    """
    return {"status": DesignStatus.AWAITING_HUMAN}


# --------------------------------------------------------------------------- #
# F9.1 — driver-mode node factories
# --------------------------------------------------------------------------- #
def _audit(ctx: StageContext, design_id: str, stage: Stage, ref: Any) -> None:
    """Emit one ``ARTIFACT_PROMOTED`` event per stage-head transition."""
    if ctx.audit_log is None:
        return
    ctx.audit_log.append(
        design_id=design_id,
        event_type=EventType.ARTIFACT_PROMOTED,
        payload={
            "stage": stage.value,
            "ref": {
                "artifact_id": ref.artifact_id,
                "version": ref.version,
                "kind": ref.kind.value,
                "content_hash": ref.content_hash,
            },
        },
    )


def _module_update(state: DesignState) -> dict[str, Any]:
    """Snapshot of fields a per-module RTL node mutates."""
    return {"modules": state.modules}


def _design_stage_update(state: DesignState) -> dict[str, Any]:
    """Snapshot of fields a design-level (non-RTL) stage mutates."""
    return {"stages": state.stages}


def _make_rtl_node(ctx: StageContext) -> Callable[[DesignState], Any]:
    """Drive ``ctx.rtl_driver`` over every module in the plan (topo order).

    On per-module failure the node first consults the F19.9 reflection
    router (when wired + budget remaining) to pick a recovery route;
    otherwise it routes to the human gate. The reflection routes
    re-enter upstream nodes via ``Command(goto=...)`` after clearing
    the relevant per-module heads so the rerun is a true regeneration
    rather than a cache-hit on the existing head.
    """
    def _node(state: DesignState) -> Any:
        if ctx.rtl_driver is None or ctx.tb_for_module is None:
            return {"current_stage": Stage.RTL}
        # F20.8 — try-imports are local so non-M19 fixtures that don't
        # have the M19 imports wired stay green.
        from chip_agent.graph.heads import MissingHeadError as _MissingHeadError
        with ctx.tracer.span(Stage.RTL.value, kind=SpanKind.STAGE):
            plan = load_plan(state, ctx.store)
            for module_id in _module_order(plan):
                tb = ctx.tb_for_module(state, module_id)
                # F20.8 — anchor the outer-repair prompt on the
                # ContractArtifact head when available so the model
                # cannot silently regress a spec constraint
                # (sync/async reset, SE polarity, etc.) across attempts.
                # Trivial-module fast-path + pre-M19 fixtures don't
                # have a contract head; the driver tolerates None
                # with a byte-identical prompt.
                try:
                    contract = load_contract_head(
                        state, module_id, ctx.store,
                    )
                except _MissingHeadError:
                    contract = None
                # F23.3 — inline any operator guidance from a prior
                # interactive HUMAN turn on this module into the repair
                # prompt. Best-effort: absent hint → hint_text=None →
                # byte-identical to the pre-F23 prompt.
                hint_text = _load_hint_text(ctx, state.design_id, module_id)
                outcome = ctx.rtl_driver.drive_module(
                    plan, module_id, tb, contract=contract,
                    hint_text=hint_text,
                )
                apply_rtl_outcome(
                    state, outcome, module_id=module_id, store=ctx.store,
                )
                if outcome.passed:
                    # F23.6 — refuse to advance a *vacuous* pass (green sim
                    # whose window never let the design finish); open an
                    # interactive-repair turn instead.
                    vac_cmd = _check_vacuous_pass(
                        ctx, state, plan, module_id, tb,
                    )
                    if vac_cmd is not None:
                        return vac_cmd
                    _audit(ctx, state.design_id, Stage.RTL, outcome.rtl_ref)
                    continue
                # Failure — F19.9 dispatcher.
                cmd = _dispatch_rtl_failure(
                    ctx, state, plan, module_id, outcome,
                )
                if cmd is not None:
                    return cmd
        return {**_module_update(state), "current_stage": Stage.RTL}
    _node.__name__ = "_driver_rtl_node"
    return _node


def _dispatch_rtl_failure(
    ctx: StageContext,
    state: DesignState,
    plan: Any,
    module_id: str,
    outcome: Any,
) -> Command[Any] | None:
    """F19.9 — pick a recovery route for an RTL failure.

    Returns a :class:`Command` (escalate / re-enter upstream) when the
    spine must leave the RTL node; ``None`` means "keep iterating"
    (currently unused — every failure escalates somewhere, but the
    seam stays so the caller code remains uniform).
    """
    # Fall through to the human gate when reflection isn't wired or
    # the budget is consumed. This preserves the pre-F19.9 behaviour
    # for fixtures that don't set ``reflection_router``.
    if ctx.reflection_router is None or state.global_feedback_budget <= 0:
        return Command(
            goto=HUMAN_REVIEW_NODE,
            update={
                **request_human_review(),
                **_module_update(state),
                "current_stage": Stage.RTL,
            },
        )

    from chip_agent.graph.heads import MissingHeadError, load_rtl_head
    try:
        contract = load_contract_head(state, module_id, ctx.store)
        oracle = load_oracle_head(state, module_id, ctx.store)
        assertion_spec = load_assertion_spec_head(state, module_id, ctx.store)
        rtl_art = load_rtl_head(state, module_id, ctx.store)
    except MissingHeadError:
        # Reflection routing needs the full M19 + RTL lineage to make
        # an informed decision. When any head is missing (e.g. the
        # F9.1 driver tests that exercise a non-M19 RTL failure path),
        # fall back to the existing human-gate escalation.
        return Command(
            goto=HUMAN_REVIEW_NODE,
            update={
                **request_human_review(),
                **_module_update(state),
                "current_stage": Stage.RTL,
            },
        )
    diagnosis = outcome.diagnosis

    siblings = [
        m.module_id for m in plan.modules if m.module_id != module_id
    ]
    # ``attempts_so_far`` is an informational signal for the prompt
    # (the dispatcher's authoritative cap is ``global_feedback_budget``).
    # Default initial budget on a fresh ``DesignState`` is 2, so the
    # delta tells the model how many turns it has already used.
    attempts_so_far = max(0, 2 - state.global_feedback_budget)
    route = ctx.reflection_router.classify(
        diagnosis,
        contract=contract,
        oracle=oracle,
        assertion_spec=assertion_spec,
        rtl=rtl_art,
        sibling_module_ids=siblings,
        attempts_so_far=attempts_so_far,
        max_attempts=2,
    )
    state.global_feedback_budget -= 1

    if route.kind is ReflectionRouteKind.ESCALATE_HUMAN:
        # F23.3 — before dead-ending, try an interactive HUMAN turn: let the
        # operator's guidance re-seed one more bounded retry. Returns None
        # (fall back to the human gate) when not wired / declined / budget
        # spent / the hint can't resolve a helpful route.
        human_cmd = _try_interactive_human_turn(
            ctx, state, module_id, outcome, siblings,
        )
        if human_cmd is not None:
            return human_cmd
        # F23.5 Option B — no blocking provider: park an interactive-repair
        # request across a checkpoint interrupt so a TUI / `resume --hint`
        # can inject guidance out-of-band and resume back into RTL.
        pause_cmd = _open_human_repair_pause(ctx, state, module_id, outcome)
        if pause_cmd is not None:
            return pause_cmd
        return Command(
            goto=HUMAN_REVIEW_NODE,
            update={
                **request_human_review(),
                **_module_update(state),
                "current_stage": Stage.RTL,
            },
        )

    if route.kind is ReflectionRouteKind.REGEN_CURRENT_RTL:
        clear_module_stage_head(state, module_id, Stage.RTL)
        return Command(
            goto=_NODE_NAMES[Stage.RTL],
            update={
                **_module_update(state),
                "current_stage": Stage.RTL,
            },
        )

    if route.kind is ReflectionRouteKind.REVISIT_SIBLING_RTL:
        target = route.target_module
        if target is None or target not in siblings:
            # The agent's parser already drops hallucinated siblings;
            # belt-and-braces defence here for the same case.
            return Command(
                goto=HUMAN_REVIEW_NODE,
                update={
                    **request_human_review(),
                    **_module_update(state),
                    "current_stage": Stage.RTL,
                },
            )
        clear_module_stage_head(state, target, Stage.RTL)
        clear_module_stage_head(state, module_id, Stage.RTL)
        return Command(
            goto=_NODE_NAMES[Stage.RTL],
            update={
                **_module_update(state),
                "current_stage": Stage.RTL,
            },
        )

    # RE_EXTRACT_CONTRACT — clear the full M19 lineage for the failing
    # module and re-enter the CONTRACT node. CONTRACT->ORACLE->
    # ASSERTIONS->ORACLE_VERIFICATION->RTL flows from there.
    for stage in (
        Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS,
        Stage.ORACLE_VERIFICATION, Stage.RTL,
    ):
        clear_module_stage_head(state, module_id, stage)
    return Command(
        goto=_NODE_NAMES[Stage.CONTRACT],
        update={
            **_module_update(state),
            "current_stage": Stage.CONTRACT,
            "global_feedback_budget": state.global_feedback_budget,
        },
    )


def _load_hint_text(
    ctx: StageContext, design_id: str, module_id: str,
) -> str | None:
    """Latest ``HumanHint`` for ``module_id`` rendered for the repair prompt.

    Best-effort: no hint on file (the common case) → ``None`` → the prompt
    is byte-identical to pre-F23. Deterministic artifact id per the
    distiller (``<design>.<module>.hint``); ``get_by_id`` returns the
    newest version.
    """
    try:
        hint = ctx.store.get_by_id(f"{design_id}.{module_id}.hint")
    except StoreError:
        return None
    if not isinstance(hint, HumanHint):
        return None
    return hint_prompt_section(hint)


def _audit_human(
    ctx: StageContext, design_id: str, event_type: EventType,
    payload: dict[str, Any],
) -> None:
    """Emit one interactive-human-turn audit event (no-op without a log)."""
    if ctx.audit_log is None:
        return
    ctx.audit_log.append(
        design_id=design_id, event_type=event_type, payload=payload,
    )


def _try_interactive_human_turn(
    ctx: StageContext,
    state: DesignState,
    module_id: str,
    outcome: Any,
    siblings: list[str],
) -> Command[Any] | None:
    """F23.3 — open an interactive HUMAN turn on an RTL escalation.

    Returns a re-entry :class:`Command` when an operator hint re-seeds a
    bounded retry, or ``None`` (caller falls back to the human gate) when
    the turn isn't wired, the operator declines (no transcript), the
    per-stage human-turn budget is spent, or the distilled hint resolves
    to ESCALATE_HUMAN. The stage gate stays binding throughout — this only
    clears the head + arms another attempt (see ``graph.human_repair``).
    """
    if ctx.human_hint_distiller is None or ctx.human_transcript_for is None:
        return None
    diagnosis = outcome.diagnosis
    if diagnosis is None:
        return None
    ss = get_or_create_stage_state(state, Stage.RTL, module_id=module_id)
    if ss.human_turns_left() <= 0:
        return None
    transcript = ctx.human_transcript_for(state, module_id, diagnosis)
    if not transcript:
        return None

    _audit_human(
        ctx, state.design_id, EventType.HUMAN_TURN_OPENED,
        {"module_id": module_id, "stage": Stage.RTL.value},
    )
    hint = ctx.human_hint_distiller.distill(
        transcript, diagnosis,
        target_stage=Stage.RTL, target_module=module_id,
    )
    hint_ref = ctx.store.put(hint)
    _audit_human(
        ctx, state.design_id, EventType.HUMAN_HINT_RECORDED,
        {
            "module_id": module_id,
            "hint_kind": hint.hint_kind.value,
            "ref": {
                "artifact_id": hint_ref.artifact_id,
                "version": hint_ref.version,
                "content_hash": hint_ref.content_hash,
            },
        },
    )

    # No structured sibling target on a hint, so REVISIT_SIBLING_RTL degrades
    # to ESCALATE_HUMAN here → fall back to the human gate.
    hroute = route_from_hint(hint, siblings=siblings)
    if hroute.kind is ReflectionRouteKind.ESCALATE_HUMAN:
        return None
    if not grant_human_retry(ss):
        return None
    _audit_human(
        ctx, state.design_id, EventType.HUMAN_RETRY_DISPATCHED,
        {
            "module_id": module_id,
            "route": hroute.kind.value,
            "human_turns_used": ss.human_turns_used,
        },
    )

    if hroute.kind is ReflectionRouteKind.RE_EXTRACT_CONTRACT:
        for stage in (
            Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS,
            Stage.ORACLE_VERIFICATION, Stage.RTL,
        ):
            clear_module_stage_head(state, module_id, stage)
        return Command(
            goto=_NODE_NAMES[Stage.CONTRACT],
            update={**_module_update(state), "current_stage": Stage.CONTRACT},
        )

    # Default: REGEN_CURRENT_RTL — clear the RTL head and re-drive it.
    clear_module_stage_head(state, module_id, Stage.RTL)
    return Command(
        goto=_NODE_NAMES[Stage.RTL],
        update={**_module_update(state), "current_stage": Stage.RTL},
    )


# --------------------------------------------------------------------------- #
# F23.5 Option B — interactive-repair interrupt/resume (TUI + checkpoint)
# --------------------------------------------------------------------------- #
def _open_human_repair_pause(
    ctx: StageContext,
    state: DesignState,
    module_id: str,
    outcome: Any,
) -> Command[Any] | None:
    """Park an interactive-repair request + pause (Option B).

    Returns a ``Command`` routing to :data:`HUMAN_REPAIR_NODE` (which the
    compiled graph interrupts after) when the distiller is wired, there is
    NO blocking provider (that is Option A's job), the stage has a human
    turn left, and a diagnosis exists. ``None`` otherwise → caller falls
    back to the plain human gate.
    """
    if ctx.human_hint_distiller is None or ctx.human_transcript_for is not None:
        return None
    diagnosis = outcome.diagnosis
    if diagnosis is None:
        return None
    ss = get_or_create_stage_state(state, Stage.RTL, module_id=module_id)
    if ss.human_turns_left() <= 0:
        return None

    # Persist the diagnosis so the resume leg can reload it by ref.
    diag_ref = diagnosis.ref() if diagnosis.content_hash else ctx.store.put(diagnosis)
    pending = PendingHumanRepair(
        module_id=module_id, stage=Stage.RTL, diagnosis_ref=diag_ref,
    )
    _audit_human(
        ctx, state.design_id, EventType.HUMAN_TURN_OPENED,
        {"module_id": module_id, "stage": Stage.RTL.value, "mode": "interrupt"},
    )
    return Command(
        goto=HUMAN_REPAIR_NODE,
        update={
            "pending_human_repair": pending,
            "status": DesignStatus.AWAITING_HUMAN,
            **_module_update(state),
            "current_stage": Stage.RTL,
        },
    )


def _human_repair_pause(state: DesignState) -> dict[str, Any]:
    """Interrupt point for an interactive-repair turn. The runtime halts
    right after this node (``interrupt_after``); the caller injects a
    transcript into ``pending_human_repair`` and resumes."""
    return {"status": DesignStatus.AWAITING_HUMAN}


def _route_after_human_repair(state: DesignState) -> str:
    """On resume: apply the hint if the operator supplied one, else end."""
    p = state.pending_human_repair
    if p is not None and p.transcript:
        return HUMAN_REPAIR_APPLY_NODE
    return END


def _make_human_repair_apply(
    ctx: StageContext | None,
) -> Callable[[DesignState], Any]:
    """Node that consumes an injected transcript and re-enters the loop.

    Distils the operator's transcript into a ``HumanHint``, validates a
    route, consumes one bounded human turn, clears the relevant head, and
    ``Command(goto=…)`` back into RTL / CONTRACT. Any decline / spent
    budget / unresolvable route ends the run blocked (``END``) — an RTL
    escalation never falls through to gdsii.
    """
    def _node(state: DesignState) -> Command[Any]:
        p = state.pending_human_repair
        clear = {"pending_human_repair": None}
        if (
            ctx is None
            or ctx.human_hint_distiller is None
            or p is None
            or not p.transcript
        ):
            return Command(goto=END, update=clear)
        diagnosis = None
        if p.diagnosis_ref is not None:
            try:
                diagnosis = ctx.store.get(p.diagnosis_ref)
            except StoreError:
                diagnosis = None
        if not isinstance(diagnosis, FailureDiagnosis):
            return Command(goto=END, update=clear)
        ss = get_or_create_stage_state(state, p.stage, module_id=p.module_id)
        if ss.human_turns_left() <= 0:
            return Command(goto=END, update=clear)

        hint = ctx.human_hint_distiller.distill(
            p.transcript, diagnosis,
            target_stage=p.stage, target_module=p.module_id,
        )
        hint_ref = ctx.store.put(hint)
        _audit_human(
            ctx, state.design_id, EventType.HUMAN_HINT_RECORDED,
            {
                "module_id": p.module_id,
                "hint_kind": hint.hint_kind.value,
                "ref": {
                    "artifact_id": hint_ref.artifact_id,
                    "version": hint_ref.version,
                    "content_hash": hint_ref.content_hash,
                },
            },
        )
        plan = load_plan(state, ctx.store)
        siblings = [m.module_id for m in plan.modules if m.module_id != p.module_id]
        hroute = route_from_hint(hint, siblings=siblings)
        if hroute.kind is ReflectionRouteKind.ESCALATE_HUMAN:
            return Command(goto=END, update=clear)
        if not grant_human_retry(ss):
            return Command(goto=END, update=clear)
        _audit_human(
            ctx, state.design_id, EventType.HUMAN_RETRY_DISPATCHED,
            {
                "module_id": p.module_id,
                "route": hroute.kind.value,
                "human_turns_used": ss.human_turns_used,
            },
        )

        if hroute.kind is ReflectionRouteKind.RE_EXTRACT_CONTRACT:
            for stage in (
                Stage.CONTRACT, Stage.ORACLE, Stage.ASSERTIONS,
                Stage.ORACLE_VERIFICATION, Stage.RTL,
            ):
                clear_module_stage_head(state, p.module_id, stage)
            goto_stage = Stage.CONTRACT
        else:
            clear_module_stage_head(state, p.module_id, Stage.RTL)
            goto_stage = Stage.RTL
        return Command(
            goto=_NODE_NAMES[goto_stage],
            update={
                **_module_update(state),
                "pending_human_repair": None,
                # Back to RUNNING so the edge router doesn't re-route to the
                # human gate on the next hop.
                "status": DesignStatus.RUNNING,
                "current_stage": goto_stage,
            },
        )
    _node.__name__ = "_human_repair_apply_node"
    return _node


def _check_vacuous_pass(
    ctx: StageContext,
    state: DesignState,
    plan: Any,
    module_id: str,
    tb: Any,
) -> Command[Any] | None:
    """F23.6 — open a repair turn on a *vacuous* pass instead of advancing.

    A green differential sim whose window never let the design finish (the
    F23.6 ``vacuous_completion_port`` flag set on the TB by
    ``differential_tb``) is not trustworthy. When interactive repair is
    available (distiller wired + turn budget) we synthesise a diagnosis and
    open a turn — blocking (Option A) or interrupt (Option B) — so a
    dubious pass gets human eyes rather than silently taping out. Returns
    ``None`` (advance as normal) when the pass is genuine or no distiller /
    budget is available.
    """
    port = tb.metadata.get("vacuous_completion_port") if tb.metadata else None
    if not port or ctx.human_hint_distiller is None:
        return None
    ss = get_or_create_stage_state(state, Stage.RTL, module_id=module_id)
    if ss.human_turns_left() <= 0:
        return None

    diagnosis = FailureDiagnosis(
        artifact_id=f"{state.design_id}.{module_id}.diagnosis",
        design_id=state.design_id,
        module_id=module_id,
        failing_signal=port,
        nl_summary=(
            f"Sim PASSED but completion port {port!r} never asserted across "
            "the stimulus window — the differential pass is vacuous (the "
            "design may never finish, or the window is too short to observe "
            "completion). Extend the stimulus / verify the design completes."
        ),
        suspected_cause="stimulus window too short to observe completion",
        provenance=Provenance(produced_by=Stage.RTL, agent="vacuous_pass_gate"),
    )
    ctx.store.put(diagnosis)
    _audit_human(
        ctx, state.design_id, EventType.SIM_VACUOUS_PASS,
        {"module_id": module_id, "completion_port": port},
    )

    carrier = SimpleNamespace(diagnosis=diagnosis)
    siblings = [m.module_id for m in plan.modules if m.module_id != module_id]
    cmd = _try_interactive_human_turn(ctx, state, module_id, carrier, siblings)
    if cmd is not None:
        return cmd
    # Option B: park + pause so a non-interactive run halts for review rather
    # than advancing broken silicon.
    return _open_human_repair_pause(ctx, state, module_id, carrier)


# --------------------------------------------------------------------------- #
# F19.7 — M19 Phase 1 nodes (CONTRACT, ORACLE, ASSERTIONS, ORACLE_VERIFICATION)
#
# All four are per-module, single-shot (no repair budget), and emit one
# artifact per module per stage. The verification gate is warning-only:
# ``gate_ok=False`` is recorded in the artifact and audit log but does NOT
# trigger ``AWAITING_HUMAN`` or block downstream RTL. F19.10 will tighten.
#
# F19.13 — Per-module fast path. Each of the four nodes consults
# :func:`_is_trivial_module` inside its iteration loop and skips trivial
# modules. The CONTRACT node emits one
# ``EventType.M19_FAST_PATH_DECISION`` audit event per module so the trail
# records the dispatch decision.
# --------------------------------------------------------------------------- #

# F19.13: shared name sets, kept in sync with ``stim_ramp.py``.
_M19_CLOCK_NAMES = frozenset({"clk", "clock"})
_M19_RESET_NAMES = frozenset({"rst_n", "rstn", "reset_n", "resetn", "rst", "reset"})
_M19_CLK_OR_RESET_NAMES = _M19_CLOCK_NAMES | _M19_RESET_NAMES


def _is_trivial_module(module: Any, *, max_ports: int) -> bool:
    """F19.13 heuristic: ``True`` when the module's M19 stages should skip.

    The heuristic is two-part and ANDed:

    * ``len(module.ports) <= max_ports`` — small modules don't warrant the
      contract / oracle / assertions overhead.
    * No input port name matches a clock / reset convention — proxy for
      "no state" (pre-RTL, we have no other signal for statefulness).

    Pure function; safe to call multiple times per module. Each M19 node
    re-evaluates rather than persisting the verdict on ``ModuleState``.
    """
    if len(module.ports) > max_ports:
        return False
    for port in module.ports:
        if (
            port.direction == "in"
            and port.name.lower() in _M19_CLK_OR_RESET_NAMES
        ):
            return False
    return True


def _audit_fast_path_decision(
    ctx: StageContext,
    design_id: str,
    *,
    module: Any,
    fast_path_used: bool,
    max_ports: int,
) -> None:
    """Emit one ``M19_FAST_PATH_DECISION`` event per module."""
    if ctx.audit_log is None:
        return
    has_clock_or_reset = any(
        p.direction == "in" and p.name.lower() in _M19_CLK_OR_RESET_NAMES
        for p in module.ports
    )
    ctx.audit_log.append(
        design_id=design_id,
        event_type=EventType.M19_FAST_PATH_DECISION,
        payload={
            "module_id": module.module_id,
            "m19_fast_path_used": fast_path_used,
            "port_count": len(module.ports),
            "has_clock_or_reset": has_clock_or_reset,
            "max_ports": max_ports,
        },
    )


def _make_contract_node(ctx: StageContext) -> Callable[[DesignState], Any]:
    """Per-module: extract a ContractArtifact from (spec, ModuleDecl).

    F19.13: emits one ``M19_FAST_PATH_DECISION`` audit event per module
    (regardless of the verdict) and skips trivial modules' contract
    extraction — they fall through to the LLM TB + single-call RTL gen
    path via the existing ``MissingHeadError`` fallbacks in
    ``tb_for_module`` and ``_dispatch_rtl_failure``.
    """
    def _node(state: DesignState) -> Any:
        if ctx.contract_extractor is None:
            return {"current_stage": Stage.CONTRACT}
        with ctx.tracer.span(Stage.CONTRACT.value, kind=SpanKind.STAGE):
            plan = load_plan(state, ctx.store)
            spec = load_spec_head(state, ctx.store)
            plan_ref = state.plan
            for module_id in _module_order(plan):
                module = next(
                    m for m in plan.modules if m.module_id == module_id
                )
                is_trivial = _is_trivial_module(
                    module, max_ports=ctx.m19_trivial_max_ports,
                )
                _audit_fast_path_decision(
                    ctx, state.design_id,
                    module=module, fast_path_used=is_trivial,
                    max_ports=ctx.m19_trivial_max_ports,
                )
                if is_trivial:
                    continue
                contract = ctx.contract_extractor.extract(
                    spec, module, plan_ref=plan_ref,
                )
                ref = ctx.store.put(contract)
                set_module_stage_head(state, module_id, Stage.CONTRACT, ref)
                _audit(ctx, state.design_id, Stage.CONTRACT, ref)
        return {**_module_update(state), "current_stage": Stage.CONTRACT}
    _node.__name__ = "_driver_contract_node"
    return _node


def _make_oracle_node(ctx: StageContext) -> Callable[[DesignState], Any]:
    """Per-module: generate an OracleArtifact from the module's contract."""
    def _node(state: DesignState) -> Any:
        if ctx.oracle_gen is None:
            return {"current_stage": Stage.ORACLE}
        with ctx.tracer.span(Stage.ORACLE.value, kind=SpanKind.STAGE):
            plan = load_plan(state, ctx.store)
            for module_id in _module_order(plan):
                module = next(
                    m for m in plan.modules if m.module_id == module_id
                )
                # F19.13: skip trivial modules — CONTRACT didn't promote a
                # head for them, so there's no contract to consume.
                if _is_trivial_module(
                    module, max_ports=ctx.m19_trivial_max_ports,
                ):
                    continue
                contract = load_contract_head(state, module_id, ctx.store)
                oracle = ctx.oracle_gen.generate(contract, module)
                set_module_stage_head(state, module_id, Stage.ORACLE, oracle.ref())
                _audit(ctx, state.design_id, Stage.ORACLE, oracle.ref())
        return {**_module_update(state), "current_stage": Stage.ORACLE}
    _node.__name__ = "_driver_oracle_node"
    return _node


def _make_assertions_node(ctx: StageContext) -> Callable[[DesignState], Any]:
    """Per-module: generate an AssertionSpec from the module's contract."""
    def _node(state: DesignState) -> Any:
        if ctx.assertion_gen is None:
            return {"current_stage": Stage.ASSERTIONS}
        with ctx.tracer.span(Stage.ASSERTIONS.value, kind=SpanKind.STAGE):
            plan = load_plan(state, ctx.store)
            for module_id in _module_order(plan):
                module = next(
                    m for m in plan.modules if m.module_id == module_id
                )
                # F19.13: skip trivial modules (no contract was produced).
                if _is_trivial_module(
                    module, max_ports=ctx.m19_trivial_max_ports,
                ):
                    continue
                contract = load_contract_head(state, module_id, ctx.store)
                spec = ctx.assertion_gen.generate(contract, module)
                set_module_stage_head(
                    state, module_id, Stage.ASSERTIONS, spec.ref(),
                )
                _audit(ctx, state.design_id, Stage.ASSERTIONS, spec.ref())
        return {**_module_update(state), "current_stage": Stage.ASSERTIONS}
    _node.__name__ = "_driver_assertions_node"
    return _node


def _make_oracle_verification_node(
    ctx: StageContext,
) -> Callable[[DesignState], Any]:
    """Per-module: run the F19.6 triangulation gate. Warning-only —
    ``gate_ok=False`` is recorded but does NOT escalate or block RTL.
    """
    def _node(state: DesignState) -> Any:
        if ctx.oracle_verifier is None:
            return {"current_stage": Stage.ORACLE_VERIFICATION}
        with ctx.tracer.span(Stage.ORACLE_VERIFICATION.value, kind=SpanKind.STAGE):
            plan = load_plan(state, ctx.store)
            for module_id in _module_order(plan):
                module = next(
                    m for m in plan.modules if m.module_id == module_id
                )
                # F19.13: skip trivial modules (no oracle/assertions to verify).
                if _is_trivial_module(
                    module, max_ports=ctx.m19_trivial_max_ports,
                ):
                    continue
                oracle = load_oracle_head(state, module_id, ctx.store)
                assertion_spec = load_assertion_spec_head(
                    state, module_id, ctx.store,
                )
                stim = build_ramp_stim(module)
                verdict = ctx.oracle_verifier.verify(
                    oracle, assertion_spec, module,
                    stim=stim, spec_ref=state.spec,
                )
                set_module_stage_head(
                    state, module_id, Stage.ORACLE_VERIFICATION, verdict.ref(),
                )
                _audit(
                    ctx, state.design_id, Stage.ORACLE_VERIFICATION,
                    verdict.ref(),
                )
        # Warning-only: never escalate. Just advance.
        return {
            **_module_update(state),
            "current_stage": Stage.ORACLE_VERIFICATION,
        }
    _node.__name__ = "_driver_oracle_verification_node"
    return _node


def _make_synth_node(ctx: StageContext) -> Callable[[DesignState], Any]:
    def _node(state: DesignState) -> Any:
        if ctx.synth_driver is None:
            return {"current_stage": Stage.SYNTH}
        with ctx.tracer.span(Stage.SYNTH.value, kind=SpanKind.STAGE):
            from chip_agent.graph.heads import load_rtl_head
            top_module_id = state.top_module_id or next(iter(state.modules))
            rtl = load_rtl_head(state, top_module_id, ctx.store)
            outcome = ctx.synth_driver.drive(
                rtl, std_cell_lib=state.constraints.std_cell_lib,
            )
            apply_synth_outcome(state, outcome, store=ctx.store)
            if outcome.passed:
                _audit(ctx, state.design_id, Stage.SYNTH, outcome.netlist_ref)
            else:
                return Command(
                    goto=HUMAN_REVIEW_NODE,
                    update={
                        **request_human_review(),
                        **_design_stage_update(state),
                        "current_stage": Stage.SYNTH,
                    },
                )
        return {**_design_stage_update(state), "current_stage": Stage.SYNTH}
    _node.__name__ = "_driver_synth_node"
    return _node


def _make_physical_node(ctx: StageContext) -> Callable[[DesignState], Any]:
    def _node(state: DesignState) -> Any:
        if ctx.physical_driver is None or ctx.physical_config is None:
            return {"current_stage": Stage.PHYSICAL}
        with ctx.tracer.span(Stage.PHYSICAL.value, kind=SpanKind.STAGE):
            netlist = load_netlist_head(state, ctx.store)
            # F21.3: reapply the SIGNOFF-failure dispatcher's route
            # history at entry to produce the active config. Empty
            # history (default) → identity; pre-F21.3 path is byte-
            # identical. The pure function does not mutate
            # ctx.physical_config.
            config = ctx.physical_config
            for route in state.physical_repair_routes:
                config = apply_repair_delta(config, route)
            outcome = ctx.physical_driver.drive(
                netlist, config=config,
            )
            apply_physical_outcome(state, outcome, store=ctx.store)
            if outcome.passed and outcome.layout_ref is not None:
                _audit(ctx, state.design_id, Stage.PHYSICAL, outcome.layout_ref)
            else:
                return Command(
                    goto=HUMAN_REVIEW_NODE,
                    update={
                        **request_human_review(),
                        **_design_stage_update(state),
                        "current_stage": Stage.PHYSICAL,
                    },
                )
        return {**_design_stage_update(state), "current_stage": Stage.PHYSICAL}
    _node.__name__ = "_driver_physical_node"
    return _node


def _human_escalate_signoff(state: DesignState) -> Command[Any]:
    """F21.3: convenience builder for SIGNOFF → HUMAN escalation. Used
    by ``_dispatch_signoff_failure`` for all the non-recoverable branches
    (no agent wired, budget exhausted, non-timing failure, ESCALATE_HUMAN
    route picked, etc)."""
    return Command(
        goto=HUMAN_REVIEW_NODE,
        update={
            **request_human_review(),
            **_design_stage_update(state),
            "current_stage": Stage.SIGNOFF,
        },
    )


def _signoff_timing_negative_slack(outcome: Any) -> bool:
    """F21.3: decide whether the SIGNOFF failure is the kind F21.3 can
    repair — negative slack at the worst corner (multi-corner) or at the
    typical corner (single-corner).

    Other timing failure modes (READ_VERILOG / generic STA.ERROR) bypass
    the agent: a structural error won't be fixed by flipping a placement
    knob, and the agent's ESCALATE_HUMAN fallback would just add one
    extra LLM call before reaching the human gate.
    """
    if outcome.multi_corner_timing is not None:
        wns = outcome.multi_corner_timing.worst_wns_ns
        return wns is not None and wns < 0
    if outcome.timing is not None:
        wns = outcome.timing.wns_ns
        return wns is not None and wns < 0
    return False


def _dispatch_signoff_failure(
    ctx: StageContext,
    state: DesignState,
    layout: Any,
    outcome: Any,
) -> Command[Any]:
    """F21.3 — pick a recovery route for a SIGNOFF timing failure.

    Triggers only when SIGNOFF's timing leg specifically failed AND the
    failure shape is "negative slack" (worst-corner WNS < 0). DRC / LVS
    / security failures and structural STA errors bypass to HUMAN —
    matching today's behaviour for those modes.

    Branch shape (mirrors :func:`_dispatch_rtl_failure`):

    * Agent not wired OR ``global_feedback_budget`` ≤ 0 → HUMAN.
    * Failure shape isn't "negative slack" → HUMAN (today's path).
    * Agent picks ``ESCALATE_HUMAN`` → HUMAN.
    * Otherwise: clear PHYSICAL + SIGNOFF stage state, append the route
      to ``state.physical_repair_routes``, decrement
      ``global_feedback_budget``, emit ``PHYSICAL_REPAIR_ROUTED`` audit
      event, route via ``Command(goto=PHYSICAL)``.
    """
    if (
        ctx.physical_repair_router is None
        or state.global_feedback_budget <= 0
        or not _signoff_timing_negative_slack(outcome)
    ):
        return _human_escalate_signoff(state)

    # Reapply the existing history to produce the config the agent saw.
    config = ctx.physical_config
    for route in state.physical_repair_routes:
        config = apply_repair_delta(config, route)

    timing_for_agent = outcome.multi_corner_timing or outcome.timing
    # ``attempts_so_far`` = how many routes have already been applied.
    # The agent's authoritative cap is ``global_feedback_budget``; the
    # delta tells the prompt how many turns have been used.
    attempts_so_far = len(state.physical_repair_routes)
    # F21.3 routes per design are capped by the shared budget (default
    # global_feedback_budget=2). max_attempts=5 caps the per-classify
    # cycle independently — the inner agent layer is defensive.
    route = ctx.physical_repair_router.classify(
        timing_for_agent,
        current_config=config,
        history=list(state.physical_repair_routes),
        attempts_so_far=attempts_so_far,
        max_attempts=5,
    )
    state.global_feedback_budget -= 1

    if route.kind is PhysicalRepairRouteKind.ESCALATE_HUMAN:
        if ctx.audit_log is not None:
            ctx.audit_log.append(
                design_id=state.design_id,
                event_type=EventType.PHYSICAL_REPAIR_ROUTED,
                payload={
                    "kind": route.kind.value,
                    "reason": route.reason,
                    "attempt": attempts_so_far,
                },
            )
        return _human_escalate_signoff(state)

    # Append the route to the history; the next PHYSICAL entry will
    # reapply it (Phase 4's reapply loop).
    state.physical_repair_routes.append(route)

    # Clear PHYSICAL + SIGNOFF stage state so re-entry rebuilds them.
    # We don't touch SYNTH (still valid; same netlist) or any per-module
    # heads (no per-module PHYSICAL state today).
    for stage in (Stage.PHYSICAL, Stage.SIGNOFF):
        if stage in state.stages:
            state.stages[stage].head = None
            state.stages[stage].results = []
            state.stages[stage].status = StageStatus.PENDING
            state.stages[stage].last_failure = None

    if ctx.audit_log is not None:
        ctx.audit_log.append(
            design_id=state.design_id,
            event_type=EventType.PHYSICAL_REPAIR_ROUTED,
            payload={
                "kind": route.kind.value,
                "reason": route.reason,
                # ``attempt`` reads the history length AFTER append, so
                # the first repair is attempt=1.
                "attempt": len(state.physical_repair_routes),
            },
        )
    return Command(
        goto=_NODE_NAMES[Stage.PHYSICAL],
        update={
            **_design_stage_update(state),
            "current_stage": Stage.PHYSICAL,
            "global_feedback_budget": state.global_feedback_budget,
            "physical_repair_routes": list(state.physical_repair_routes),
        },
    )


def _make_signoff_node(ctx: StageContext) -> Callable[[DesignState], Any]:
    def _node(state: DesignState) -> Any:
        if ctx.signoff_driver is None or ctx.clock_period_ns is None:
            return {"current_stage": Stage.SIGNOFF}
        with ctx.tracer.span(Stage.SIGNOFF.value, kind=SpanKind.STAGE):
            netlist = load_netlist_head(state, ctx.store)
            layout = load_layout_head(state, ctx.store)
            outcome = ctx.signoff_driver.drive(
                netlist, layout,
                clock_period_ns=ctx.clock_period_ns,
                layout_netlist_bytes=ctx.layout_netlist_bytes,
                top_module=ctx.top_module_verilog_name,
            )
            apply_signoff_outcome(state, outcome, store=ctx.store)
            # F21.2: surface the sky130A multi-corner segfault fallback so
            # operators can correlate a "wait, why is this single-corner?"
            # without reading the raw librelane log. Only fires when the
            # operator opted in via SignoffSettings.sta_corners AND
            # OpenROAD #6227 tripped during the harvest.
            if outcome.multi_corner_fallback and ctx.audit_log is not None:
                ctx.audit_log.append(
                    design_id=state.design_id,
                    event_type=EventType.MULTI_CORNER_FALLBACK,
                    payload={
                        "reason": "openroad_6227_segfault",
                        "module_id": netlist.module_id,
                    },
                )
            if not outcome.passed:
                # F21.3: route through the timing-failure dispatcher.
                # Falls back to HUMAN escalation when the agent isn't
                # wired, the budget is consumed, the failure shape isn't
                # recoverable, or the agent picks ESCALATE_HUMAN.
                return _dispatch_signoff_failure(
                    ctx, state, layout, outcome,
                )
        return {**_design_stage_update(state), "current_stage": Stage.SIGNOFF}
    _node.__name__ = "_driver_signoff_node"
    return _node


def _make_gdsii_node(
    ctx: StageContext,
) -> Callable[[DesignState], dict[str, Any]]:
    def _node(state: DesignState) -> dict[str, Any]:
        if ctx.gdsii_driver is None:
            # Fall back to terminal placeholder behaviour so the design
            # status still flips to COMPLETED.
            return {
                "current_stage": Stage.GDSII,
                "status": DesignStatus.COMPLETED,
            }
        with ctx.tracer.span(Stage.GDSII.value, kind=SpanKind.STAGE):
            layout = load_layout_head(state, ctx.store)
            # Prefer the resolved Verilog name: ``layout.module_id`` is the
            # planner's ``mod_*`` handle, which Magic's ``load <top>`` step
            # rejects (or silently dereferences against a non-existent
            # cell, producing a 130-byte zero-cell GDS skeleton). The
            # ``or layout.module_id`` fallback keeps F9.1 stub-driver tests
            # — which never set ``top_module_verilog_name`` — passing.
            top_module = (
                ctx.top_module_verilog_name
                or layout.module_id
                or state.top_module_id
                or next(iter(state.modules), None)
            )
            outcome = ctx.gdsii_driver.drive(layout, top_module=top_module)
            apply_gdsii_outcome(state, outcome, store=ctx.store)
            _audit(ctx, state.design_id, Stage.GDSII, outcome.gds_ref)
        return {
            **_design_stage_update(state),
            "current_stage": Stage.GDSII,
            "status": DesignStatus.COMPLETED,
        }
    _node.__name__ = "_driver_gdsii_node"
    return _node


def _module_order(plan: Any) -> list[str]:
    """Topologically sort plan modules by ``depends_on``; fall back to declaration order."""
    deps: dict[str, set[str]] = {
        m.module_id: set(m.depends_on) for m in plan.modules
    }
    if not any(deps.values()):
        return [m.module_id for m in plan.modules]
    try:
        ts: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter(deps)
        return list(ts.static_order())
    except graphlib.CycleError:
        # Cycles aren't legal but keep the spine running; declaration order
        # is at least deterministic.
        return [m.module_id for m in plan.modules]


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #
def build_design_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    stage_context: StageContext | None = None,
) -> CompiledStateGraph[DesignState, Any, DesignState, DesignState]:
    """Compile the linear macro graph over :class:`DesignState`.

    Pass a checkpointer (e.g. via :func:`open_sqlite_checkpointer`) so every
    transition lands on disk and runs can resume against the same ``thread_id``.
    The compiled graph interrupts after :data:`HUMAN_REVIEW_NODE` so callers
    must explicitly resume (``invoke(None, config)``) before GDSII is emitted.

    When ``stage_context`` is provided, RTL / SYNTH / PHYSICAL / SIGNOFF /
    GDSII nodes delegate to the real stage drivers in the context. SPEC and
    PLAN nodes stay placeholders in both modes — those artifacts are
    produced upstream by the CLI / future planner agent. RTL / SYNTH /
    PHYSICAL / SIGNOFF nodes route to :data:`HUMAN_REVIEW_NODE` when they
    flip ``status`` to ``AWAITING_HUMAN``; otherwise they advance linearly.
    """
    graph: StageGraph_ = StateGraph(DesignState)

    last_stage = STAGE_ORDER[-1]
    for stage in STAGE_ORDER:
        node = _node_for_stage(stage, stage_context, terminal=stage is last_stage)
        # LangGraph's `add_node` overloads are tight on the node-callable type;
        # a closure returned by `_advance` matches structurally but not by name.
        graph.add_node(_NODE_NAMES[stage], node)  # type: ignore[call-overload]
    graph.add_node(HUMAN_REVIEW_NODE, _await_human)
    # F23.5 Option B — interactive-repair interrupt/resume nodes. Inert
    # unless an RTL escalation parks a ``pending_human_repair`` (only when a
    # distiller is wired with no blocking provider), so every existing flow
    # is unchanged.
    graph.add_node(HUMAN_REPAIR_NODE, _human_repair_pause)
    # Command-returning node: same add_node typing gap the stage nodes hit.
    graph.add_node(
        HUMAN_REPAIR_APPLY_NODE,
        _make_human_repair_apply(stage_context),  # type: ignore[arg-type]
    )
    graph.add_conditional_edges(
        HUMAN_REPAIR_NODE,
        _route_after_human_repair,
        {HUMAN_REPAIR_APPLY_NODE: HUMAN_REPAIR_APPLY_NODE, END: END},
    )

    graph.add_edge(START, _NODE_NAMES[STAGE_ORDER[0]])
    # F19.7 — M19 Phase 1 stages (CONTRACT, ORACLE, ASSERTIONS,
    # ORACLE_VERIFICATION) are wired sequentially via the same pairwise
    # loop as the rest of the spine. The plan called for a parallel
    # CONTRACT -> {ORACLE || ASSERTIONS} fan-out but LangGraph's
    # ``LastValue`` channel on ``current_stage`` rejects two concurrent
    # writes in the same super-step (it would require an ``Annotated``
    # reducer on the DesignState field, which is out of scope for F19.7
    # and risks perturbing every existing channel test). The AC is
    # warning-only / artifacts-produced, not parallel-timing — see the
    # plan's risk table.
    for prev, nxt in pairwise(STAGE_ORDER):
        if nxt is last_stage:
            # Final hop routes through the human-review pause.
            graph.add_edge(_NODE_NAMES[prev], HUMAN_REVIEW_NODE)
            graph.add_edge(HUMAN_REVIEW_NODE, _NODE_NAMES[last_stage])
        else:
            graph.add_conditional_edges(
                _NODE_NAMES[prev],
                _make_edge_router(next_node=_NODE_NAMES[nxt]),
                {
                    _NODE_NAMES[nxt]: _NODE_NAMES[nxt],
                    HUMAN_REVIEW_NODE: HUMAN_REVIEW_NODE,
                    # F23.6: a vacuous-pass / interactive-repair escalation
                    # routes here so the static edge dedupes with the handler's
                    # Command(goto=HUMAN_REPAIR_NODE) instead of scheduling a
                    # second pause node (concurrent ``status`` write).
                    HUMAN_REPAIR_NODE: HUMAN_REPAIR_NODE,
                },
            )
    graph.add_edge(_NODE_NAMES[last_stage], END)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_after=[HUMAN_REVIEW_NODE, HUMAN_REPAIR_NODE],
    )


def _make_edge_router(*, next_node: str) -> Callable[[DesignState], str]:
    """Return a conditional-edge function: AWAITING_HUMAN → human gate else linear.

    When a stage handler escalates via ``Command(goto=…)`` (e.g. the F23.6
    vacuous-pass gate routes to ``HUMAN_REPAIR_NODE``), LangGraph *also*
    evaluates this static conditional edge on the post-update state — the
    Command's goto and this edge each schedule a task for the next super-step.
    If the two disagree on the target, two pause nodes run in one super-step
    and both write ``status``; the ``LastValue`` channel rejects that with
    ``InvalidUpdateError``. Pointing this edge at the SAME pause node the
    Command chose lets LangGraph dedupe the two into a single task.
    ``pending_human_repair`` is the F23.5/F23.6 signal that the escalation went
    to the interactive-repair pause rather than the pre-GDSII review gate.
    """
    def _route(state: DesignState) -> str:
        if state.status is DesignStatus.AWAITING_HUMAN:
            if state.pending_human_repair is not None:
                return HUMAN_REPAIR_NODE
            return HUMAN_REVIEW_NODE
        return next_node
    return _route


# Type alias kept local so the long generic stays readable.
StageGraph_ = StateGraph[DesignState, Any, DesignState, DesignState]


def _node_for_stage(
    stage: Stage, ctx: StageContext | None, *, terminal: bool,
) -> Callable[[DesignState], dict[str, Any]]:
    """Pick the right node factory for ``stage``.

    Placeholder for SPEC / PLAN regardless of ``ctx`` (upstream-produced
    artifacts). Driver nodes only when a context is supplied; otherwise
    placeholder for everything (F5.1 back-compat).
    """
    if ctx is None or stage in (Stage.SPEC, Stage.PLAN):
        return _advance(stage, terminal=terminal)
    if stage is Stage.CONTRACT:
        return _make_contract_node(ctx)
    if stage is Stage.ORACLE:
        return _make_oracle_node(ctx)
    if stage is Stage.ASSERTIONS:
        return _make_assertions_node(ctx)
    if stage is Stage.ORACLE_VERIFICATION:
        return _make_oracle_verification_node(ctx)
    if stage is Stage.RTL:
        return _make_rtl_node(ctx)
    if stage is Stage.SYNTH:
        return _make_synth_node(ctx)
    if stage is Stage.PHYSICAL:
        return _make_physical_node(ctx)
    if stage is Stage.SIGNOFF:
        return _make_signoff_node(ctx)
    if stage is Stage.GDSII:
        return _make_gdsii_node(ctx)
    # Defensive: any new Stage value would land here.
    return _advance(stage, terminal=terminal)  # pragma: no cover


@contextmanager
def open_sqlite_checkpointer(path: Path | str) -> Iterator[BaseCheckpointSaver[Any]]:
    """Context-managed :class:`SqliteSaver` over ``path``.

    Yields the saver for use as ``compile(checkpointer=...)``. Closes the
    underlying connection on exit so callers don't leak handles between runs.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(p)) as saver:
        yield saver
