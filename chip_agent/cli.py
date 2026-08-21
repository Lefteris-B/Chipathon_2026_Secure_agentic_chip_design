"""F8.1+ ``chip-agent run`` / ``chip-agent resume`` CLI.

The CLI is the first user-facing entry point. It glues together the
spec loader, the LangGraph spine, the artifact store, the audit log,
and the run manifest behind two subcommands:

* ``chip-agent run --spec <file> --config <yaml> --name <NAME> --run-dir <DIR>``
  Reads a natural-language spec from markdown, runs the
  :class:`~chip_agent.agents.spec_intake.SpecIntakeAgent` against the
  configured :class:`~chip_agent.routing.router.LiteLLMRouter` to mint
  a typed :class:`~chip_agent.design_state.Spec` artifact, runs the
  :class:`~chip_agent.agents.planner.PlannerAgent` against the same
  router to mint a :class:`~chip_agent.design_state.DesignPlan`, then
  drives the LangGraph spine. F10.1 deleted the F9.1-era regex + stub
  plan path: ``--config`` with a non-empty ``routing.registry`` is now
  mandatory.

* ``chip-agent resume --design-id <id> --run-dir <DIR>``
  Reloads the checkpoint and resumes the spine past the human gate.
  The GDSII node mints the real :class:`GDSIIArtifact` via the F6.5
  driver; the CLI then walks ``provenance.inputs`` from the GDS head
  and writes a :class:`RunManifest` JSON sibling so an F7.3 replay
  can verify reproducibility.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from langchain_core.runnables import RunnableConfig

from chip_agent.agents.planner import PlannerAgent
from chip_agent.agents.spec_intake import SpecIntakeAgent
from chip_agent.cli_chat import ChatSession
from chip_agent.cli_human_repair import console_transcript_provider
from chip_agent.cli_stubs import build_demo_stage_context
from chip_agent.design_state import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    ArtifactStatus,
    DesignPlan,
    DesignState,
    DesignStatus,
    GDSIIArtifact,
    LayoutArtifact,
    ModelRouter,
    ModuleState,
    NetlistArtifact,
    RTLArtifact,
    Spec,
    Stage,
    StageState,
    TestbenchArtifact,
)
from chip_agent.graph.state_graph import (
    build_design_graph,
    open_sqlite_checkpointer,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.obs.replay import RunManifest, manifest_from_run
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.routing.gateway import LiteLLMGateway
from chip_agent.routing.router import LiteLLMRouter
from chip_agent.settings import Settings
from chip_agent.store.sqlite_store import SqliteArtifactStore, StoreError
from chip_agent.tools._protocols import SandboxLike
from chip_agent.tools.image import ImageProvisioningError, verify_pinned_image
from chip_agent.tools.sandbox import DockerSandbox

__all__ = [
    "CLIError",
    "ChatOutcome",
    "PreflightError",
    "ResumeOutcome",
    "RunArgs",
    "RunOutcome",
    "SandboxKind",
    "TuiOutcome",
    "build_arg_parser",
    "cmd_chat",
    "cmd_resume",
    "cmd_run",
    "cmd_tui",
    "main",
    "preflight_local_models",
]


SandboxKind = str  # "stub" | "docker"


_DEFAULT_HMAC_KEY = b"chip-agent-demo-hmac-key-v0"


class CLIError(ValueError):
    """Surfaced when the CLI is invoked with an incomplete or invalid configuration."""


class PreflightError(CLIError):
    """A local backend (e.g. Ollama) failed its boot-time reachability check.

    Distinct from a generic :class:`CLIError` so callers (tests, scripts)
    can distinguish "I gave you bad config" from "your environment isn't
    ready" — the latter is recoverable by starting a daemon or pulling a
    model, not by editing the YAML.
    """


@dataclass(frozen=True)
class RunArgs:
    """Parsed args for one CLI invocation."""

    cmd: str
    spec_path: Path | None
    name: str | None
    run_dir: Path
    design_id: str | None
    hmac_key: bytes
    tracer: Tracer | None = None  # F8.3: inject InMemoryTracer for tests/observability
    config_path: Path | None = None  # F9.2/F10.1: --config <yaml> (required for run)
    # F9.2: --sandbox {stub,docker}. F14.4: ``None`` on ``resume`` means
    # "inherit the backend the original run recorded in run_meta.json".
    sandbox_kind: str | None = "stub"
    # F11.3: stdin/stdout injection for ``chip-agent chat``. Defaulted to
    # ``None`` so the dataclass stays frozen + hashable; cmd_chat falls
    # back to ``sys.stdin`` / ``sys.stdout`` when these are ``None``.
    chat_stdin: TextIO | None = field(default=None, hash=False, compare=False)
    chat_stdout: TextIO | None = field(default=None, hash=False, compare=False)
    # F23.5 (Option A): when set on ``run``, an interactive HUMAN-repair
    # turn opens on RTL escalation — the operator is prompted on the
    # console for guidance that re-seeds a bounded retry. Default off so
    # non-interactive / CI runs never block on stdin.
    interactive_repair: bool = False
    # F23.5 (Option B): operator guidance injected on ``resume`` into a
    # parked interactive-repair request (``pending_human_repair``) so the
    # graph re-enters RTL with the hint. None = ordinary resume.
    hint: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    """Result of ``cmd_run`` — surfaced to tests + the dispatcher.

    F9.1 moved RTL / SYNTH / PHYSICAL artifact production into the
    LangGraph nodes; F10.1 moved Spec + Plan production into the
    spec-intake + planner agents. The CLI itself only mints the
    design_id, drives the agents, and seeds the graph state. Downstream
    artifact refs land on the paused :class:`DesignState` head pointers.
    """

    design_id: str
    spec_ref: ArtifactRef
    plan_ref: ArtifactRef
    paused_state: DesignState
    exports_dir: Path  # F11.6: named mirrors of every staged artifact


@dataclass(frozen=True)
class ResumeOutcome:
    """Result of ``cmd_resume`` — surfaced to tests + the dispatcher.

    F23.5: a resume can now end WITHOUT a GDS — an interactive-repair
    resume whose hinted retry re-pauses (needs more guidance) or ends
    blocked (budget spent). In those cases ``gds_ref`` / ``manifest`` /
    ``manifest_path`` are ``None`` and ``final_state`` carries the outcome
    (``pending_human_repair`` set ⇒ re-paused; otherwise stuck). The happy
    (completed-to-GDS) path fills all fields as before.
    """

    design_id: str
    final_state: DesignState
    exports_dir: Path  # F11.6: named mirrors including the final GDS (when produced)
    gds_ref: ArtifactRef | None = None
    manifest: RunManifest | None = None
    manifest_path: Path | None = None


@dataclass(frozen=True)
class ChatOutcome:
    """Result of ``cmd_chat`` — surfaced to tests + the dispatcher.

    A successful ``/run`` ends with ``spec_ref`` populated; ``/exit`` or
    EOF returns ``ChatOutcome(spec_ref=None, ...)`` so the caller can
    distinguish "session ended; nothing minted" from "Spec ready for
    handoff."
    """

    design_id: str
    spec_ref: ArtifactRef | None
    transcript_path: Path
    exports_dir: Path  # F11.6: spec.md / spec.json mirror after /run minted


@dataclass(frozen=True)
class TuiOutcome:
    """Result of ``cmd_tui`` — surfaced to tests + the dispatcher.

    Same shape as ``ChatOutcome`` (the F14.1 vertical slice has identical
    semantics: chat → /run mints a Spec → app exits). Later F-features
    will keep the app open past Spec materialisation; the outcome shape
    will gain ``paused_state`` (F14.3) and ``gds_ref`` (post-resume).
    """

    design_id: str
    spec_ref: ArtifactRef | None
    transcript_path: Path
    exports_dir: Path


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    ns = parser.parse_args(argv)
    args = _resolve_args(ns)
    if args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "resume":
        cmd_resume(args)
    elif args.cmd == "chat":
        cmd_chat(args)
    elif args.cmd == "tui":
        cmd_tui(args)
    else:  # pragma: no cover — argparse forbids other values
        parser.error(f"unknown command: {args.cmd}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chip-agent",
        description="Drive a natural-language chip spec through the design flow.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser(
        "run", help="Start a new run from a spec markdown file (or chat-minted Spec).",
    )
    run_p.add_argument(
        "--spec", required=False, type=Path, default=None,
        help="Path to the spec markdown. Omit when --design-id refers to a "
        "Spec already in the store (e.g. one minted via `chip-agent chat`).",
    )
    run_p.add_argument("--name", required=True, help="Human-readable design name.")
    run_p.add_argument(
        "--design-id", default=None, help="Override the minted design_id.",
    )
    run_p.add_argument(
        "--interactive-repair", action="store_true",
        help="On RTL escalation, prompt on the console for repair guidance "
        "that re-seeds one more bounded, gated attempt (F23.5).",
    )

    resume_p = sub.add_parser(
        "resume", help="Resume a paused run past the human gate.",
    )
    resume_p.add_argument("--design-id", required=True, help="Design id to resume.")

    chat_p = sub.add_parser(
        "chat",
        help="Interactive REPL: describe a module conversationally; /run hands off to cmd_run.",
    )
    chat_p.add_argument("--name", required=True, help="Human-readable design name.")
    chat_p.add_argument(
        "--design-id", default=None, help="Override the minted design_id.",
    )

    tui_p = sub.add_parser(
        "tui",
        help="Textual TUI: single-window app for chat + pipeline + audit + exports.",
    )
    tui_p.add_argument(
        "--name", default="untitled", help="Human-readable design name.",
    )
    tui_p.add_argument(
        "--design-id", default=None, help="Override the minted design_id.",
    )
    tui_p.add_argument(
        "--run-dir", type=Path, default=Path("./runs"),
        help="Directory for store, audit log, checkpoint, manifest.",
    )
    tui_p.add_argument(
        "--sandbox", choices=["stub", "docker"], default="docker",
        help="Backend sandbox for tool services. Default 'docker' so the "
        "pipeline produces real synth/physical/signoff output; pass "
        "'--sandbox stub' to skip Docker for a UI smoke test "
        "(GDS + verification reports will be placeholders).",
    )
    tui_p.add_argument(
        "--config", type=Path, default=Path("configs/local-only.yaml"),
        help="Settings YAML with routing.registry + per-task bindings.",
    )

    for sub_parser in (run_p, resume_p, chat_p):
        sub_parser.add_argument(
            "--run-dir", required=True, type=Path,
            help="Directory for store, audit log, checkpoint, manifest.",
        )
        sub_parser.add_argument(
            "--config", type=Path, default=None,
            help="Settings YAML with routing.registry + per-task bindings "
            "(required for run / chat).",
        )
    for sub_parser in (run_p, chat_p):
        sub_parser.add_argument(
            "--sandbox", choices=["stub", "docker"], default="stub",
            help="Backend sandbox for tool services "
            "(default: stub; docker requires --config).",
        )
    # F14.4: ``resume`` defaults to the backend the original run recorded
    # (run_meta.json) instead of ``stub`` — otherwise a docker run silently
    # streams out a STUB placeholder GDS on the in-process stub sandbox. An
    # explicit ``--sandbox`` still overrides the recorded backend.
    resume_p.add_argument(
        "--sandbox", choices=["stub", "docker"], default=None,
        help="Backend sandbox for the GDSII stream-out. Default: inherit the "
        "backend recorded from the original run (docker/stub).",
    )
    resume_p.add_argument(
        "--hint", default=None,
        help="Operator guidance for a parked interactive-repair pause (F23.5 "
        "Option B): injected into the run and resumed back into RTL.",
    )

    return parser


def cmd_run(args: RunArgs) -> RunOutcome:
    """Start a run: build spec + plan via agents, drive the spine to the human gate.

    Spec sources, in priority order:

    1. ``--spec <file>`` — read the markdown, run intake, mint a new Spec.
    2. ``--design-id <id>`` with no ``--spec`` — look up an existing
       ``<design_id>.spec`` in the store and reuse it. This is the handoff
       path from ``chip-agent chat``: that command materialises a Spec
       under the same id, and ``run`` then continues from it without
       re-running intake.
    """
    if args.name is None:
        raise CLIError("cmd_run requires --name")
    if args.spec_path is None and args.design_id is None:
        raise CLIError(
            "cmd_run requires either --spec <file> or --design-id <id> "
            "pointing to an existing Spec in the store.",
        )
    if args.spec_path is not None and not args.spec_path.exists():
        raise FileNotFoundError(f"spec file does not exist: {args.spec_path}")

    raw_text: str | None = None
    if args.spec_path is not None:
        raw_text = args.spec_path.read_text(encoding="utf-8")
        if not raw_text or not raw_text.strip():
            raise CLIError(f"spec file is empty: {args.spec_path}")

    settings = _load_settings(args)
    router = _resolve_router(args, settings=settings)

    design_id = args.design_id or _mint_design_id(args.name)

    args.run_dir.mkdir(parents=True, exist_ok=True)
    paths = _RunPaths(args.run_dir)
    tracer = args.tracer or NoopTracer()
    sandbox = _resolve_sandbox(args, settings=settings)
    # F14.4: record the backend so ``resume`` streams out the GDS on the same
    # sandbox instead of defaulting to the stub (which emits a placeholder GDS).
    _write_run_meta(paths, sandbox_kind=args.sandbox_kind)

    with ExitStack() as stack:
        store = stack.enter_context(_open_store(paths))
        audit = stack.enter_context(_open_audit(paths, hmac_key=args.hmac_key))
        # F11.5: wire the audit log + design_id into the router so any
        # backend fallback during this run lands in the audit trail.
        _attach_audit_to_router(router, audit, design_id)
        stack.enter_context(tracer.run(design_id, name=f"cmd_run:{args.name}"))

        with tracer.span(Stage.SPEC.value, kind=SpanKind.STAGE):
            spec, spec_ref = _spec_for_run(
                store=store,
                router=router,
                settings=settings,
                design_id=design_id,
                raw_text=raw_text,
            )
            audit.append(
                design_id=design_id,
                event_type=EventType.ARTIFACT_PROMOTED,
                payload={"stage": Stage.SPEC.value, "ref": _ref_payload(spec_ref)},
            )

        with tracer.span(Stage.PLAN.value, kind=SpanKind.STAGE):
            planner = PlannerAgent(router=router, design_id=design_id)
            plan = planner.plan(spec)
            plan_ref = store.put(plan)
            audit.append(
                design_id=design_id,
                event_type=EventType.ARTIFACT_PROMOTED,
                payload={"stage": Stage.PLAN.value, "ref": _ref_payload(plan_ref)},
            )

        top_module = _resolve_verilog_top_name(plan)
        use_m19 = settings.routing.use_test_first_workflow
        initial = _seed_initial_state(
            design_id=design_id, name=args.name,
            spec=spec, plan=plan, spec_ref=spec_ref, plan_ref=plan_ref,
            use_test_first_workflow=use_m19,
        )

        # F23.5 (Option A): wire the blocking console provider only when the
        # operator opted in via --interactive-repair. Left None otherwise so
        # the escalation path is unchanged for non-interactive / CI runs.
        human_transcript_for = None
        if args.interactive_repair:
            human_transcript_for = console_transcript_provider(
                stdin=args.chat_stdin, stdout=args.chat_stdout,
            )

        stage_context = build_demo_stage_context(
            store=store, audit_log=audit, tracer=tracer,
            design_id=design_id, top_module=top_module,
            constraints=spec.constraints, sandbox=sandbox, router=router,
            spec=spec, plan=plan,
            use_test_first_workflow=use_m19,
            m19_trivial_max_ports=settings.routing.m19_trivial_max_ports,
            sta_corners=(
                tuple(settings.signoff.sta_corners)
                if settings.signoff.sta_corners else None
            ),
            sta_report_power=settings.signoff.sta_report_power,
            human_transcript_for=human_transcript_for,
        )

        saver = stack.enter_context(open_sqlite_checkpointer(paths.checkpoint))
        graph = build_design_graph(
            checkpointer=saver, stage_context=stage_context,
        )
        paused_raw = graph.invoke(initial, _thread_config(design_id))
        paused = _as_state(paused_raw)

        audit.append(
            design_id=design_id,
            event_type=EventType.GATE_DECISION,
            payload={
                "stage": Stage.SIGNOFF.value,
                "verdict": "await_human",
                "current_stage": paused.current_stage.value,
            },
        )

        # F11.6: mirror every produced artifact under a named exports tree
        # while the store is still open. Failures here are best-effort —
        # the canonical truth is the content-addressed store.
        exports_dir = _export_artifacts(
            store=store, design=paused,
            exports_root=paths.exports_dir(design_id),
        )

    _print_run(design_id=design_id, paused=paused, spec_ref=spec_ref,
               run_dir=args.run_dir, exports_dir=exports_dir)
    return RunOutcome(
        design_id=design_id, spec_ref=spec_ref, plan_ref=plan_ref,
        paused_state=paused, exports_dir=exports_dir,
    )


def cmd_resume(args: RunArgs) -> ResumeOutcome:
    """Resume a paused run: walk past the human gate, mint a GDS, emit manifest."""
    paths = _RunPaths(args.run_dir)
    if not paths.checkpoint.exists():
        raise FileNotFoundError(
            f"no checkpoint at {paths.checkpoint}; was the run started in {args.run_dir}?",
        )
    if args.design_id is None:
        raise CLIError("cmd_resume requires design_id")
    design_id = args.design_id
    tracer = args.tracer or NoopTracer()
    settings = _load_settings(args)
    # F14.4: unless the operator pins --sandbox on resume, reuse the backend
    # the original run recorded so a docker run streams out a real GDS rather
    # than the stub-sandbox placeholder.
    if args.sandbox_kind is None:
        args = replace(args, sandbox_kind=_recorded_sandbox_kind(paths))
    sandbox = _resolve_sandbox(args, settings=settings)
    router = _resolve_router(args, settings=settings)

    with ExitStack() as stack:
        store = stack.enter_context(_open_store(paths))
        audit = stack.enter_context(_open_audit(paths, hmac_key=args.hmac_key))
        _attach_audit_to_router(router, audit, design_id)
        stack.enter_context(tracer.run(design_id, name="cmd_resume"))

        # F19.11: open the checkpointer FIRST so we can peek at the
        # persisted ``DesignState.use_test_first_workflow`` before we
        # build the StageContext. Reject mid-session flag flips loudly
        # — the M19 graph topology vs the pre-M19 fallback path can't
        # be mixed on a single checkpoint.
        saver = stack.enter_context(open_sqlite_checkpointer(paths.checkpoint))
        use_m19 = _check_workflow_flag_for_resume(
            saver=saver, design_id=design_id, settings=settings,
        )

        audit.append(
            design_id=design_id,
            event_type=EventType.HUMAN_DECISION,
            payload={"decision": "approve"},
        )

        # The resume path walks the same graph compile signature: a
        # StageContext supplies the GDSII driver so the node mints a
        # real artifact rather than the F5.3 placeholder.
        stage_context = _resume_stage_context(
            store=store, audit_log=audit, tracer=tracer, design_id=design_id,
            sandbox=sandbox, router=router,
            use_test_first_workflow=use_m19,
            m19_trivial_max_ports=settings.routing.m19_trivial_max_ports,
            sta_corners=(
                tuple(settings.signoff.sta_corners)
                if settings.signoff.sta_corners else None
            ),
            sta_report_power=settings.signoff.sta_report_power,
        )

        graph = build_design_graph(
            checkpointer=saver, stage_context=stage_context,
        )
        thread = _thread_config(design_id)
        # F23.5 Option B — inject operator guidance into a parked
        # interactive-repair request before resuming, so the graph re-enters
        # RTL with the hint rather than dead-ending. No-op for an ordinary
        # (pre-GDSII gate) resume or when --hint is absent.
        if args.hint:
            current = _as_state(graph.get_state(thread).values)
            update = _hint_update_for_state(current, args.hint)
            if update is not None:
                graph.update_state(thread, update)
        # Resume from the interrupt (pre-GDSII gate or interactive repair).
        final_raw = graph.invoke(None, thread)
        final = _as_state(final_raw)

        # F23.5: an interactive-repair resume can end WITHOUT a GDS — the
        # hinted retry re-paused (needs more guidance) or ended blocked.
        # Branch instead of crashing on a missing GDSII head.
        gds_ref = _gds_head_ref_or_none(final)
        manifest: RunManifest | None = None
        manifest_path: Path | None = None
        if gds_ref is not None:
            store.set_status(gds_ref, ArtifactStatus.ACCEPTED)
            manifest = manifest_from_run(
                store, design_id=design_id, root_ref=gds_ref,
            )
            manifest_path = paths.manifest_path(design_id)
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(manifest.model_dump_json(indent=2))

        # F11.6: re-run the export pass so the final layout + signoff bodies
        # + the GDS (when produced) land next to whatever cmd_run exported.
        exports_dir = _export_artifacts(
            store=store, design=final,
            exports_root=paths.exports_dir(design_id),
        )

    _print_resume(design_id=design_id, final=final, gds_ref=gds_ref,
                  manifest_path=manifest_path, exports_dir=exports_dir)
    return ResumeOutcome(
        design_id=design_id, gds_ref=gds_ref, final_state=final,
        manifest=manifest, manifest_path=manifest_path,
        exports_dir=exports_dir,
    )


def cmd_chat(args: RunArgs) -> ChatOutcome:
    """Run the ``chip-agent chat`` REPL.

    Streams clarifying-question dialog through ``router.stream``
    (:class:`LiteLLMRouter.stream`, F11.2); persists the transcript to
    ``<run_dir>/chat.transcript.md``; on ``/run``, materialises a typed
    :class:`Spec` via :class:`SpecIntakeAgent` and stores it under
    ``<design_id>.spec`` so a subsequent ``chip-agent run --design-id <id>``
    picks it up without re-running intake.
    """
    if args.name is None:
        raise CLIError("cmd_chat requires --name")

    settings = _load_settings(args)
    router = _resolve_router(args, settings=settings)

    design_id = args.design_id or _mint_design_id(args.name)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    paths = _RunPaths(args.run_dir)

    exports_dir = paths.exports_dir(design_id)
    with ExitStack() as stack:
        store = stack.enter_context(_open_store(paths))
        session = ChatSession(
            router=router,
            store=store,
            design_id=design_id,
            name=args.name,
            transcript_path=paths.transcript_path,
            defaults=settings.constraints,
            stdin=args.chat_stdin if args.chat_stdin is not None else sys.stdin,
            stdout=args.chat_stdout if args.chat_stdout is not None else sys.stdout,
        )
        spec = session.run()

        # F11.6: mirror the chat-minted Spec under exports/<id>/spec.{md,json}
        # so the operator can hand-inspect the materialised body before
        # running ``chip-agent run``. The export helper takes a DesignState
        # so we build a minimal one carrying just the spec ref — every other
        # branch (plan, modules, stages) no-ops cleanly.
        if spec is not None:
            chat_state = DesignState(
                design_id=design_id, name=args.name,
                constraints=spec.constraints,
                spec=spec.ref(),
            )
            _export_artifacts(
                store=store, design=chat_state, exports_root=exports_dir,
            )

    spec_ref: ArtifactRef | None = None
    if spec is not None:
        spec_ref = ArtifactRef(
            artifact_id=spec.artifact_id, version=spec.version,
            kind=spec.kind, content_hash=spec.content_hash,
        )
    _print_chat(
        design_id=design_id, spec_ref=spec_ref, run_dir=args.run_dir,
        name=args.name,
        out=_stdout_writer(args.chat_stdout),
    )
    return ChatOutcome(
        design_id=design_id, spec_ref=spec_ref,
        transcript_path=paths.transcript_path,
        exports_dir=exports_dir,
    )


def cmd_tui(args: RunArgs) -> TuiOutcome:
    """Launch the Textual TUI.

    Builds the same router + store + paths ``cmd_chat`` uses, plus a
    ``run_args_factory`` so the in-app ``[R]`` / ``[A]`` keybinds can
    spawn the existing ``cmd_run`` / ``cmd_resume`` entry points
    without re-implementing arg construction. The app returns a typed
    :class:`TuiResult` carrying whichever of (spec, paused_state,
    final_state) the operator advanced to before quitting.
    """
    from chip_agent.tui.app import ChipAgentApp

    if args.name is None:
        raise CLIError("cmd_tui requires --name")
    name = args.name  # local narrow so the closures below can use it

    settings = _load_settings(args)
    # Fail fast on missing Ollama daemon / unpulled models BEFORE the
    # Textual app takes over the terminal — a stderr trace there gets
    # masked by the alt-screen redraw and the operator just sees a
    # silent freeze.
    preflight_local_models(settings)
    # Same rationale for the docker image: when ``--sandbox docker`` is
    # the active backend (the TUI default), we must know up front that
    # the pinned image is locally available — otherwise the operator
    # spends time chatting, hits [R], and only THEN sees that the image
    # was never pulled.
    if args.sandbox_kind == "docker":
        _preflight_docker_image(settings, config_path=args.config_path)
    router = _resolve_router(args, settings=settings)

    design_id = args.design_id or _mint_design_id(name)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    paths = _RunPaths(args.run_dir)

    def _transcript_for(did: str) -> Path:
        """Per-design transcript path. The TUI uses one file per design_id
        so Ctrl+N's reset doesn't mingle conversations across runs."""
        return args.run_dir / f"chat.transcript.{did}.md"

    def _exports_for(did: str) -> Path:
        return paths.exports_dir(did)

    def _reasoning_for(did: str) -> Path:
        return paths.reasoning_jsonl(did)

    def _mint_id() -> str:
        return _mint_design_id(name)

    exports_dir = _exports_for(design_id)
    transcript_path = _transcript_for(design_id)
    reasoning_jsonl_path = _reasoning_for(design_id)

    def _run_args_factory(cmd: str, current_design_id: str) -> RunArgs:
        """Build a RunArgs for the in-app keybind workers, parameterised
        on the design_id the app is currently driving."""
        return RunArgs(
            cmd=cmd, spec_path=None, name=args.name,
            run_dir=args.run_dir, design_id=current_design_id,
            hmac_key=args.hmac_key, config_path=args.config_path,
            sandbox_kind=args.sandbox_kind,
        )

    spec_ref: ArtifactRef | None = None
    paused_state: DesignState | None = None
    final_state: DesignState | None = None
    with ExitStack() as stack:
        store = stack.enter_context(_open_store(paths))
        app = ChipAgentApp(
            router=router,
            store=store,
            design_id=design_id,
            name=args.name,
            transcript_path=transcript_path,
            checkpoint_path=paths.checkpoint,
            audit_db_path=paths.audit_db,
            hmac_key=args.hmac_key,
            exports_dir=exports_dir,
            reasoning_jsonl_path=reasoning_jsonl_path,
            run_args_factory=_run_args_factory,
            defaults=settings.constraints,
            routing=settings.routing,
            mint_design_id=_mint_id,
            transcript_path_fn=_transcript_for,
            exports_dir_fn=_exports_for,
            reasoning_jsonl_path_fn=_reasoning_for,
        )
        result = app.run()

        if result is not None:
            spec_ref = result.spec.ref() if result.spec is not None else None
            paused_state = result.paused_state
            final_state = result.final_state

            # Ctrl+N can change the design_id mid-session. Whichever was
            # last driven dictates which exports dir + transcript path
            # we report. Result objects carry the design_id of the run
            # they came from; with nothing produced we fall back to the
            # boot-time id.
            if final_state is not None:
                design_id = final_state.design_id
            elif paused_state is not None:
                design_id = paused_state.design_id
            elif result.spec is not None:
                design_id = result.spec.design_id

            exports_dir = _exports_for(design_id)
            transcript_path = _transcript_for(design_id)

            # F11.6: the F14.1 path (spec minted but operator quit without
            # running the pipeline) still needs the spec-only export pass —
            # cmd_run / cmd_resume do their own export passes when they run,
            # so we only mirror the spec when the pipeline didn't run.
            if (
                result.spec is not None
                and paused_state is None
                and final_state is None
            ):
                chat_state = DesignState(
                    design_id=design_id, name=args.name,
                    constraints=result.spec.constraints,
                    spec=result.spec.ref(),
                )
                _export_artifacts(
                    store=store, design=chat_state, exports_root=exports_dir,
                )

    _print_tui(
        design_id=design_id, spec_ref=spec_ref,
        paused_state=paused_state, final_state=final_state,
        run_dir=args.run_dir, exports_dir=exports_dir, name=args.name,
    )
    return TuiOutcome(
        design_id=design_id, spec_ref=spec_ref,
        transcript_path=transcript_path,
        exports_dir=exports_dir,
    )


# --------------------------------------------------------------------------- #
# Initial state seeding (post-agent)
# --------------------------------------------------------------------------- #
def _seed_initial_state(
    *,
    design_id: str,
    name: str,
    spec: Spec,
    plan: DesignPlan,
    spec_ref: ArtifactRef,
    plan_ref: ArtifactRef,
    use_test_first_workflow: bool = False,
) -> DesignState:
    """Build the :class:`DesignState` the graph will drive.

    Seeds a :class:`ModuleState` for every module declared in the plan
    (not just the top) so the RTL graph's per-module iteration
    (``state_graph._module_order(plan)``) can write blackboard mutations
    for any submodule the planner asked for. Seeding only the top broke
    every multi-module plan with ``BlackboardError("unknown module_id
    'X'")`` the moment the spine touched a submodule.

    F19.11: ``use_test_first_workflow`` snapshots the active routing
    flag onto the design state so ``cmd_resume`` can detect
    mid-session flag flips and reject the resume.
    """
    top = plan.top_module_id
    modules = {
        m.module_id: ModuleState(module_id=m.module_id, name=m.name)
        for m in plan.modules
    }
    # Tolerate degenerate plans that omit the top from the modules list:
    # the spine still expects ``modules[top]`` to exist.
    if top not in modules:
        modules[top] = ModuleState(module_id=top, name=top)
    return DesignState(
        design_id=design_id,
        name=name,
        constraints=spec.constraints,
        spec=spec_ref,
        plan=plan_ref,
        top_module_id=top,
        modules=modules,
        use_test_first_workflow=use_test_first_workflow,
    )


def _check_workflow_flag_for_resume(
    *,
    saver: Any,
    design_id: str,
    settings: Settings,
) -> bool:
    """F19.11: refuse a resume that flips ``use_test_first_workflow``.

    The checkpoint persists the run-creation snapshot of the flag on
    ``DesignState.use_test_first_workflow``. If the resume-time
    config disagrees, the M19 graph topology and the pre-M19 fallback
    can't be mixed on a single checkpoint without producing
    inconsistent stage state. Raise :class:`CLIError` with a clear
    message naming both values and the expected fix.

    Returns the persisted (run-creation) value so callers can build
    the StageContext that matches the original run, not the (now
    different) settings.
    """
    snapshot = saver.get(_thread_config(design_id))
    if snapshot is None:
        # No checkpoint snapshot yet — first invoke would have caught
        # this; nothing to validate. Fall back to the settings value.
        return settings.routing.use_test_first_workflow
    raw_values = snapshot.get("channel_values") or {}
    persisted = bool(raw_values.get("use_test_first_workflow", False))
    current = settings.routing.use_test_first_workflow
    if persisted != current:
        raise CLIError(
            f"routing.use_test_first_workflow was {persisted} when the "
            f"run was created (recorded on the checkpoint), but the "
            f"current config has {current}. Switching the workflow "
            f"mid-session is not supported — revert the flag in your "
            f"config to {persisted} or start a new run.",
        )
    return persisted


def _resume_stage_context(
    *,
    store: SqliteArtifactStore,
    audit_log: SqliteAuditLog,
    tracer: Tracer,
    design_id: str,
    sandbox: SandboxLike | None = None,
    router: ModelRouter,
    use_test_first_workflow: bool = True,
    m19_trivial_max_ports: int = 2,
    sta_corners: tuple[str, ...] | None = None,
    sta_report_power: bool = False,
) -> Any:
    """Rebuild the StageContext for ``cmd_resume`` — only the GDSII driver matters."""
    plan_art = _load_plan(store, design_id)
    spec_art = _load_spec(store, design_id)
    return build_demo_stage_context(
        store=store, audit_log=audit_log, tracer=tracer,
        design_id=design_id, top_module=_resolve_verilog_top_name(plan_art),
        constraints=spec_art.constraints, sandbox=sandbox, router=router,
        spec=spec_art, plan=plan_art,
        use_test_first_workflow=use_test_first_workflow,
        m19_trivial_max_ports=m19_trivial_max_ports,
        sta_corners=sta_corners,
        sta_report_power=sta_report_power,
    )


# --------------------------------------------------------------------------- #
# Settings + router/sandbox resolution
# --------------------------------------------------------------------------- #
def _load_settings(args: RunArgs) -> Settings:
    """Load :class:`Settings` from ``--config`` — required for every run/resume."""
    if args.config_path is None:
        raise CLIError(
            "--config <yaml> is required: provide a Settings YAML with a "
            "non-empty routing.registry + per-task bindings (see "
            "configs/demo-counter.yaml).",
        )
    return Settings.from_yaml(args.config_path)


def _write_run_meta(paths: _RunPaths, *, sandbox_kind: str | None) -> None:
    """Persist infra choices so ``resume`` can reuse the run's backend (F14.4).

    Best-effort: the run's canonical truth is the content-addressed store, so
    a failure to write the sidecar must not abort an otherwise-good run.
    """
    with suppress(OSError):
        paths.run_meta.write_text(
            json.dumps({"sandbox_kind": sandbox_kind or "stub"}, indent=2),
        )


def _recorded_sandbox_kind(paths: _RunPaths) -> str:
    """Read the sandbox backend recorded at run time; default ``stub`` (F14.4)."""
    try:
        meta = json.loads(paths.run_meta.read_text())
    except (OSError, ValueError):
        return "stub"
    kind = meta.get("sandbox_kind")
    return kind if kind in ("stub", "docker") else "stub"


def _resolve_sandbox(
    args: RunArgs, *, settings: Settings,
) -> SandboxLike | None:
    """Build the real ``DockerSandbox`` for ``--sandbox docker``, else ``None``.

    ``None`` keeps the stub-sandbox path active: the stage-context factory
    falls back to its in-process stub services. ``--sandbox docker`` requires
    a pinned image digest in ``settings.sandbox`` and verifies it via
    :func:`verify_pinned_image` so a drifted tag aborts the run rather than
    silently producing a different binary.
    """
    if args.sandbox_kind in (None, "stub"):
        return None
    if args.sandbox_kind != "docker":
        raise CLIError(
            f"unknown sandbox kind {args.sandbox_kind!r}; expected 'stub' or 'docker'",
        )
    if not settings.sandbox.is_pinned:
        raise ImageProvisioningError(
            f"sandbox in {args.config_path} is not digest-pinned; "
            f"run `python -m chip_agent.tools.image pin --config {args.config_path}` first",
        )
    verify_pinned_image(settings.sandbox)
    return DockerSandbox(settings.sandbox)


def _attach_audit_to_router(
    router: ModelRouter, audit: SqliteAuditLog, design_id: str,
) -> None:
    """F11.5: thread ``audit`` + ``design_id`` into ``router`` so any
    transparent backend fallback during this run lands in the audit trail.

    The plumbing is a duck-typed attribute set rather than a constructor
    arg so existing :class:`ModelRouter` callers don't need to change.
    Routers that don't carry these attributes (i.e. test stubs) just
    silently no-op.
    """
    try:
        router.audit_log = audit  # type: ignore[attr-defined]
        router.design_id = design_id  # type: ignore[attr-defined]
    except AttributeError:
        # Test stubs that disallow attribute mutation just lose the
        # fallback audit feed — non-fatal.
        pass


_OLLAMA_DEFAULT_ENDPOINT = "http://localhost:11434"
_OLLAMA_PREFLIGHT_TIMEOUT_S = 3.0


def preflight_local_models(
    settings: Settings,
    *,
    opener: Callable[[urllib.request.Request, float], Any] | None = None,
) -> None:
    """Verify every ``ollama`` entry in ``settings.routing.registry`` is reachable.

    For each entry the daemon is hit at ``<endpoint>/api/tags`` (3 s
    timeout) and the configured model name is matched against the returned
    list (with a ``:latest`` fallback for tagless model strings). Any
    failure raises :class:`PreflightError` with an actionable hint —
    "start the daemon" or "pull the model" — rather than letting the user
    discover the problem only when ``/run`` later errors out mid-stream.

    ``opener`` exists for tests: a callable matching
    ``urllib.request.urlopen``'s ``(request, timeout)`` shape so the
    HTTP call can be stubbed.
    """
    _open = opener if opener is not None else _default_urlopen
    for handle, entry in settings.routing.registry.items():
        if entry.provider != "ollama":
            continue
        endpoint = (entry.endpoint or _OLLAMA_DEFAULT_ENDPOINT).rstrip("/")
        url = f"{endpoint}/api/tags"
        try:
            req = urllib.request.Request(url, method="GET")
            with _open(req, _OLLAMA_PREFLIGHT_TIMEOUT_S) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise PreflightError(
                f"Ollama daemon at {endpoint} is unreachable "
                f"(registry handle {handle!r}, model {entry.model!r}): {e}. "
                f"Start it with `ollama serve` and pull the model with "
                f"`ollama pull {entry.model}`.",
            ) from e
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise PreflightError(
                f"Ollama daemon at {endpoint} returned a non-JSON "
                f"response when listing models: {e}",
            ) from e
        models = payload.get("models") if isinstance(payload, dict) else None
        available = {
            str(m.get("name", "")) for m in (models or []) if isinstance(m, dict)
        }
        wants = {entry.model, f"{entry.model}:latest"}
        if not (wants & available):
            avail_str = ", ".join(sorted(n for n in available if n)) or "none"
            raise PreflightError(
                f"Ollama model {entry.model!r} is not pulled at {endpoint} "
                f"(registry handle {handle!r}). Available: {avail_str}. "
                f"Pull it with `ollama pull {entry.model}`.",
            )


def _default_urlopen(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)


def _preflight_docker_image(
    settings: Settings,
    *,
    config_path: Path | None,
    verify: Callable[[Any], Any] | None = None,
) -> None:
    """Verify the pinned IIC-OSIC-TOOLS image is locally available.

    Wraps :func:`verify_pinned_image` so a missing pin, an unpulled image,
    or a docker daemon issue surfaces as :class:`PreflightError` with the
    exact pull command the operator should run. ``verify`` is injected for
    tests; in production it is :func:`verify_pinned_image`.
    """
    _verify = verify if verify is not None else verify_pinned_image
    if not settings.sandbox.is_pinned:
        cfg = config_path or "<unspecified>"
        raise PreflightError(
            f"sandbox in {cfg} is not digest-pinned; run "
            f"`python -m chip_agent.tools.image pin --config {cfg}` first, "
            f"or pass `--sandbox stub` for a UI smoke test.",
        )
    try:
        _verify(settings.sandbox)
    except ImageProvisioningError as e:
        ref = settings.sandbox.image_ref
        raise PreflightError(
            f"IIC-OSIC-TOOLS image {ref} is not available locally: {e}. "
            f"Pull it with `docker pull {ref}`, or pass `--sandbox stub` "
            f"for a UI smoke test (GDS + signoff will be placeholders).",
        ) from e


def _resolve_router(args: RunArgs, *, settings: Settings) -> ModelRouter:
    """Build a live :class:`LiteLLMRouter` from ``settings.routing``.

    F10.1 made the router non-optional: ``settings.routing.registry`` must
    have at least one entry, and at minimum the ``spec_intake``, ``plan``,
    ``rtl_gen``, and ``rtl_repair`` tasks need bindings (the loop-default
    fallback in :func:`chip_agent.routing.policy.resolve_model` covers any
    missing per-task binding).
    """
    if not settings.routing.registry:
        raise CLIError(
            "routing.registry is empty in --config "
            f"{args.config_path}: add at least one ModelEntry + per-task "
            "bindings for spec_intake/plan/rtl_gen/rtl_repair.",
        )
    # F22.2: thread the run's tracer into the gateway so every LLM call
    # appends a MODEL span to the JSONL reasoning log. Default tracer is
    # NoopTracer, so non-TUI runs that don't pass --tracer stay silent.
    return LiteLLMRouter(
        routing=settings.routing,
        gateway=LiteLLMGateway(tracer=args.tracer),
    )


def _load_spec(store: SqliteArtifactStore, design_id: str) -> Spec:
    art = store.get_by_id(f"{design_id}.spec")
    assert isinstance(art, Spec)
    return art


def _spec_for_run(
    *,
    store: SqliteArtifactStore,
    router: ModelRouter,
    settings: Settings,
    design_id: str,
    raw_text: str | None,
) -> tuple[Spec, ArtifactRef]:
    """Resolve the Spec the run will start from.

    If ``raw_text`` is set, the SpecIntakeAgent mints a fresh Spec from it.
    Otherwise we expect ``<design_id>.spec`` to already exist in the store
    (the chat-handoff path) and reuse it.
    """
    if raw_text is not None:
        # cmd_run is non-interactive — set clarifying_budget=0 so the
        # F11.4 multi-turn intake forces a Spec materialisation on the
        # first call even if the model reaches for a clarifying question.
        # The chat path (``cmd_chat``) keeps the default budget for its
        # interactive loop.
        agent = SpecIntakeAgent(
            router=router, design_id=design_id, defaults=settings.constraints,
            clarifying_budget=0,
        )
        outcome = agent.intake(raw_text)
        assert isinstance(outcome, Spec), (
            "intake with clarifying_budget=0 must return a Spec"
        )
        spec_ref = store.put(outcome)
        return outcome, spec_ref

    try:
        spec = _load_spec(store, design_id)
    except StoreError as e:
        raise CLIError(
            f"no Spec found for design_id={design_id!r} in --run-dir; "
            "pass --spec <file> for a fresh run, or chat first.",
        ) from e
    spec_ref = ArtifactRef(
        artifact_id=spec.artifact_id, version=spec.version,
        kind=spec.kind, content_hash=spec.content_hash,
    )
    return spec, spec_ref


def _load_plan(store: SqliteArtifactStore, design_id: str) -> DesignPlan:
    art = store.get_by_id(f"{design_id}.plan")
    assert isinstance(art, DesignPlan)
    return art


def _resolve_verilog_top_name(plan: DesignPlan) -> str:
    """Return the Verilog ``module <name>`` token for the plan's top module.

    The plan tracks two names per module: ``module_id`` is the planner's
    logical handle (typically prefixed with ``mod_``) used throughout the
    state graph for stage bookkeeping; ``name`` is the actual identifier
    written by the RTL specialist into the ``module`` declaration. Tools
    that consume RTL — Verilator, Yosys, LibreLane — must see the
    Verilog name; tools that track stage state see the handle. Passing
    the handle to LibreLane caused the live "counter" run to die at
    Verilator stage 2/80 with ``--top-module 'mod_up_counter_8bit' was
    not found in design`` (the source declared ``module up_counter_8bit``).
    """
    for module in plan.modules:
        if module.module_id == plan.top_module_id:
            return module.name
    raise CLIError(
        f"plan.top_module_id={plan.top_module_id!r} is not in plan.modules "
        f"(known module_ids: {[m.module_id for m in plan.modules]!r}); "
        f"the planner produced an inconsistent DesignPlan."
    )


def _gds_head_ref(final: DesignState) -> ArtifactRef:
    """Return the GDS head ref produced by the resume's GDSII node."""
    ss: StageState | None = final.stages.get(Stage.GDSII)
    if ss is None or ss.head is None:
        raise RuntimeError(
            f"resumed design {final.design_id!r} has no GDSII head — "
            f"did the GDSII node run?",
        )
    return ss.head


def _gds_head_ref_or_none(final: DesignState) -> ArtifactRef | None:
    """GDS head ref if the GDSII node ran, else ``None`` (F23.5).

    An interactive-repair resume can end blocked / re-paused before GDSII;
    callers use this to branch instead of crashing on a missing head.
    """
    ss: StageState | None = final.stages.get(Stage.GDSII)
    return ss.head if ss is not None else None


# --------------------------------------------------------------------------- #
# Run-dir wiring
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _RunPaths:
    """Conventional layout under ``--run-dir``."""

    root: Path

    @property
    def store_db(self) -> Path:
        return self.root / "store.sqlite"

    @property
    def content_dir(self) -> Path:
        return self.root / "content"

    @property
    def audit_db(self) -> Path:
        return self.root / "audit.sqlite"

    @property
    def checkpoint(self) -> Path:
        return self.root / "checkpoint.sqlite"

    @property
    def transcript_path(self) -> Path:
        # F11.3: single transcript per run-dir (one chat per design).
        return self.root / "chat.transcript.md"

    @property
    def run_meta(self) -> Path:
        # F14.4: infra metadata for the run (which sandbox backend it used),
        # read back on ``resume`` so the GDSII stream-out reuses that backend
        # rather than silently falling back to the stub sandbox.
        return self.root / "run_meta.json"

    def manifest_path(self, design_id: str) -> Path:
        return self.root / "manifests" / f"{design_id}.json"

    def exports_dir(self, design_id: str) -> Path:
        # F11.6: human-named mirrors of the content-addressed store, so an
        # operator can ``ls`` / open RTL / netlists / GDS by module name
        # without going through the SQLite index.
        return self.root / "exports" / design_id

    def reasoning_jsonl(self, design_id: str) -> Path:
        # F22.2: per-run reasoning trace (one SpanRecord per line) — written
        # by ``JsonlTracer`` when the run is invoked from the TUI so the
        # ReasoningTape pane can tail it at 500 ms.
        return self.root / "reasoning" / f"{design_id}.jsonl"


@contextmanager
def _open_store(paths: _RunPaths) -> Any:
    store = SqliteArtifactStore(
        db_path=paths.store_db, content_dir=paths.content_dir,
    )
    try:
        yield store
    finally:
        store.close()


@contextmanager
def _open_audit(paths: _RunPaths, *, hmac_key: bytes) -> Any:
    audit = SqliteAuditLog(db_path=paths.audit_db, hmac_key=hmac_key)
    try:
        yield audit
    finally:
        audit.close()


# --------------------------------------------------------------------------- #
# F11.6 — named artifact exports
# --------------------------------------------------------------------------- #
# Maps each `ArtifactKind` to the exports/ subdirectory it belongs in.
# Verification reports (LINT / SIM / STA / DRC / LVS / SECURITY / SYNTH_REPORT)
# are written as JSON bodies under the stage they gate.
_EXPORT_DIR_BY_KIND: dict[ArtifactKind, str] = {
    ArtifactKind.RTL: "rtl",
    ArtifactKind.TESTBENCH: "tb",
    ArtifactKind.LINT: "rtl",
    ArtifactKind.SIM: "sim",
    ArtifactKind.NETLIST: "synth",
    ArtifactKind.SYNTH_REPORT: "synth",
    ArtifactKind.LAYOUT: "physical",
    ArtifactKind.STA: "signoff",
    ArtifactKind.DRC: "signoff",
    ArtifactKind.LVS: "signoff",
    ArtifactKind.SECURITY: "signoff",
    ArtifactKind.GDSII: "gds",
}


def _export_artifacts(
    *,
    store: SqliteArtifactStore,
    design: DesignState,
    exports_root: Path,
) -> Path:
    """Mirror every head + result artifact of ``design`` as a named file.

    Writes copies (not symlinks) so the exports survive store relocation.
    Idempotent: identical content_hash means identical bytes, so overwriting
    is a no-op. Tolerates partial state — missing stages/blobs are skipped
    silently. The canonical truth is still the content-addressed store; this
    pass is a developer affordance for hand inspection.
    """
    exports_root.mkdir(parents=True, exist_ok=True)

    top = design.top_module_id or "design"

    _export_spec(store=store, ref=design.spec, root=exports_root)
    _export_plan(store=store, ref=design.plan, root=exports_root)

    for module_id, module in design.modules.items():
        rtl_state = module.stages.get(Stage.RTL)
        if rtl_state is not None:
            _export_module_artifacts(
                store=store, module_id=module_id, state=rtl_state,
                root=exports_root,
            )
        # The testbench isn't on the RTL StageState — it's a separate logical
        # artifact under id ``<design>.<module>.tb``. Look it up directly so
        # the exports include the cocotb harness alongside the RTL.
        _export_testbench(
            store=store, design_id=design.design_id,
            module_id=module_id, root=exports_root,
        )

    for stage in (Stage.SYNTH, Stage.PHYSICAL, Stage.SIGNOFF, Stage.GDSII):
        state = design.stages.get(stage)
        if state is None:
            continue
        _export_design_stage(
            store=store, state=state, top=top, root=exports_root,
        )

    return exports_root


def _export_spec(
    *, store: SqliteArtifactStore, ref: ArtifactRef | None, root: Path,
) -> None:
    if ref is None:
        return
    art = _try_get(store, ref)
    if not isinstance(art, Spec):
        return
    _atomic_text(root / "spec.md", art.raw_text)
    _atomic_text(root / "spec.json", art.model_dump_json(indent=2))


def _export_plan(
    *, store: SqliteArtifactStore, ref: ArtifactRef | None, root: Path,
) -> None:
    if ref is None:
        return
    art = _try_get(store, ref)
    if not isinstance(art, DesignPlan):
        return
    _atomic_text(root / "plan.json", art.model_dump_json(indent=2))


def _export_module_artifacts(
    *,
    store: SqliteArtifactStore,
    module_id: str,
    state: StageState,
    root: Path,
) -> None:
    """Write the per-module RTL head + verification bodies."""
    if state.head is not None:
        rtl = _try_get(store, state.head)
        if isinstance(rtl, RTLArtifact):
            blob = _try_get_blob(store, rtl.source)
            ext = "sv" if rtl.language == "systemverilog" else "v"
            stem = rtl.top_module or module_id
            if blob is not None:
                _atomic_bytes(root / "rtl" / f"{stem}.{ext}", blob)
            _atomic_text(
                root / "rtl" / f"{stem}.rtl.json",
                rtl.model_dump_json(indent=2),
            )
    for ref in state.results:
        _export_verification_ref(store=store, ref=ref, stem=module_id, root=root)


def _export_testbench(
    *,
    store: SqliteArtifactStore,
    design_id: str,
    module_id: str,
    root: Path,
) -> None:
    artifact_id = f"{design_id}.{module_id}.tb"
    try:
        art = store.get_by_id(artifact_id)
    except StoreError:
        return
    if not isinstance(art, TestbenchArtifact):
        return
    blob = _try_get_blob(store, art.source)
    if blob is not None:
        _atomic_bytes(root / "tb" / f"{module_id}_tb.py", blob)
    _atomic_text(
        root / "tb" / f"{module_id}_tb.json",
        art.model_dump_json(indent=2),
    )


def _export_design_stage(
    *,
    store: SqliteArtifactStore,
    state: StageState,
    top: str,
    root: Path,
) -> None:
    """Write SYNTH / PHYSICAL / SIGNOFF / GDSII heads + results."""
    if state.head is not None:
        art = _try_get(store, state.head)
        if isinstance(art, NetlistArtifact):
            blob = _try_get_blob(store, art.netlist)
            if blob is not None:
                _atomic_bytes(root / "synth" / f"{top}.netlist.v", blob)
            _atomic_text(
                root / "synth" / f"{top}.netlist.json",
                art.model_dump_json(indent=2),
            )
        elif isinstance(art, LayoutArtifact):
            blob = _try_get_blob(store, art.def_file)
            if blob is not None:
                _atomic_bytes(root / "physical" / f"{top}.def", blob)
            _atomic_text(
                root / "physical" / f"{top}.layout.json",
                art.model_dump_json(indent=2),
            )
            # F24: mirror the LibreLane report/log bundle so the native
            # OpenSTA/Magic/OpenROAD reports are hand-inspectable next to
            # the DEF instead of only living as a blob in the store.
            if art.librelane_reports is not None:
                reports_blob = _try_get_blob(store, art.librelane_reports)
                if reports_blob is not None:
                    _atomic_bytes(
                        root / "physical" / f"{top}.librelane_reports.tar.gz",
                        reports_blob,
                    )
            # Mirror the harvested LibreLane netlists next to the DEF: the
            # sky130-mapped netlist (SIGNOFF's STA input) and the powered
            # PNL with VPWR/VGND pins (SIGNOFF's LVS input). Both otherwise
            # live only as content-hash blobs in the store.
            for netlist_ref, suffix in (
                (art.librelane_mapped_netlist, "mapped.nl.v"),
                (art.librelane_powered_netlist, "pnl.v"),
            ):
                if netlist_ref is None:
                    continue
                nl_blob = _try_get_blob(store, netlist_ref)
                if nl_blob is not None:
                    _atomic_bytes(root / "physical" / f"{top}.{suffix}", nl_blob)
        elif isinstance(art, GDSIIArtifact):
            blob = _try_get_blob(store, art.gds)
            if blob is not None:
                _atomic_bytes(root / "gds" / f"{top}.gds", blob)
            _atomic_text(
                root / "gds" / f"{top}.gdsii.json",
                art.model_dump_json(indent=2),
            )
    for ref in state.results:
        _export_verification_ref(store=store, ref=ref, stem=top, root=root)


def _export_verification_ref(
    *,
    store: SqliteArtifactStore,
    ref: ArtifactRef,
    stem: str,
    root: Path,
) -> None:
    """Mirror a single verification artifact body as JSON under its stage dir."""
    subdir = _EXPORT_DIR_BY_KIND.get(ref.kind)
    if subdir is None:
        return
    art = _try_get(store, ref)
    if art is None:
        return
    filename = f"{stem}.{ref.kind.value}.json"
    _atomic_text(root / subdir / filename, art.model_dump_json(indent=2))


def _try_get(
    store: SqliteArtifactStore, ref: ArtifactRef,
) -> Artifact | None:
    try:
        return store.get(ref)
    except StoreError:
        return None


def _try_get_blob(store: SqliteArtifactStore, ref: Any) -> bytes | None:
    try:
        return store.get_blob(ref)
    except StoreError:
        return None


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve_args(ns: argparse.Namespace) -> RunArgs:
    return RunArgs(
        cmd=ns.cmd,
        spec_path=getattr(ns, "spec", None),
        name=getattr(ns, "name", None),
        run_dir=ns.run_dir,
        design_id=getattr(ns, "design_id", None),
        hmac_key=_hmac_key_from_env(),
        config_path=getattr(ns, "config", None),
        sandbox_kind=getattr(ns, "sandbox", "stub"),
        interactive_repair=getattr(ns, "interactive_repair", False),
        hint=getattr(ns, "hint", None),
    )


def _hmac_key_from_env() -> bytes:
    raw = os.environ.get("CHIP_AGENT_HMAC_KEY")
    if raw:
        return raw.encode("utf-8")
    return _DEFAULT_HMAC_KEY


def _mint_design_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    if not slug:
        slug = "design"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{slug}-{stamp}-{suffix}"


def _thread_config(design_id: str) -> RunnableConfig:
    return RunnableConfig(configurable={"thread_id": design_id})


def _as_state(raw: Any) -> DesignState:
    if isinstance(raw, DesignState):
        return raw
    if isinstance(raw, dict):
        return DesignState.model_validate(raw)
    raise TypeError(f"unexpected graph output type: {type(raw).__name__}")


def _hint_update_for_state(
    state: DesignState, hint: str,
) -> dict[str, Any] | None:
    """F23.5 Option B — state-update dict that fills a parked repair request.

    Returns ``{"pending_human_repair": <copy with transcript=hint>}`` when a
    request is parked (so ``graph.update_state`` re-enters the apply node on
    resume), or ``None`` when there is nothing to inject (ordinary resume /
    no pause) so the caller leaves the checkpoint untouched.
    """
    pending = state.pending_human_repair
    if pending is None:
        return None
    return {"pending_human_repair": pending.model_copy(update={"transcript": hint})}


def _ref_payload(ref: ArtifactRef) -> dict[str, Any]:
    return {
        "artifact_id": ref.artifact_id,
        "version": ref.version,
        "kind": ref.kind.value,
        "content_hash": ref.content_hash,
    }


def _print_run(
    *,
    design_id: str,
    paused: DesignState,
    spec_ref: ArtifactRef,
    run_dir: Path,
    exports_dir: Path | None = None,
    out: Callable[[str], None] = print,
) -> None:
    out(f"design_id:     {design_id}")
    out(f"status:        {paused.status.value}")
    out(f"current_stage: {paused.current_stage.value}")
    out(f"spec_ref:      {spec_ref.artifact_id}@v{spec_ref.version}")
    if exports_dir is not None:
        out(f"exports:       {exports_dir}")
    if paused.status is DesignStatus.AWAITING_HUMAN:
        out("awaiting human approval; resume with:")
        out(f"  chip-agent resume --design-id {design_id} --run-dir {run_dir}")


def _print_resume(
    *,
    design_id: str,
    final: DesignState,
    gds_ref: ArtifactRef | None,
    manifest_path: Path | None,
    exports_dir: Path | None = None,
    out: Callable[[str], None] = print,
) -> None:
    out(f"design_id:     {design_id}")
    out(f"status:        {final.status.value}")
    out(f"current_stage: {final.current_stage.value}")
    if gds_ref is not None:
        out(f"gds_ref:       {gds_ref.artifact_id}@v{gds_ref.version}")
        if manifest_path is not None:
            out(f"manifest:      {manifest_path}")
    elif final.pending_human_repair is not None:
        # F23.5: interactive-repair resume re-paused for more guidance.
        out(
            "gds_ref:       <none> — repair re-paused on module "
            f"{final.pending_human_repair.module_id!r}; resume again with "
            "--hint <guidance>.",
        )
    else:
        out(
            "gds_ref:       <none> — run is blocked (no GDS produced). "
            "Revisit the spec / RTL and re-run.",
        )
    if exports_dir is not None:
        out(f"exports:       {exports_dir}")


def _print_chat(
    *,
    design_id: str,
    spec_ref: ArtifactRef | None,
    run_dir: Path,
    name: str,
    out: Callable[[str], None] = print,
) -> None:
    out(f"design_id:     {design_id}")
    if spec_ref is None:
        out("status:        no spec materialised (chat ended without /run)")
        return
    out(f"spec_ref:      {spec_ref.artifact_id}@v{spec_ref.version}")
    out("hand off with:")
    out(
        f"  chip-agent run --design-id {design_id} --name {name} "
        f"--run-dir {run_dir}",
    )


def _print_tui(
    *,
    design_id: str,
    spec_ref: ArtifactRef | None,
    paused_state: DesignState | None = None,
    final_state: DesignState | None = None,
    run_dir: Path,
    exports_dir: Path,
    name: str,
    out: Callable[[str], None] = print,
) -> None:
    """Operator handoff hint after the TUI exits.

    Four end-states, ordered by progress:

    * no spec → operator quit before /run
    * spec only → /run minted, operator quit before pressing [R]
    * paused → [R] drove the spine to AWAITING_HUMAN; operator quit
      before pressing [A]
    * completed → [A] drove past the human gate to GDSII

    Mirrors ``_print_chat``'s shape so the CLI/TUI handoff lines feel
    identical, plus surfaces the exports path so the operator can
    inspect artifacts immediately.
    """
    out(f"design_id:     {design_id}")
    if spec_ref is None:
        out("status:        no spec materialised (TUI exited without /run)")
        return
    out(f"spec_ref:      {spec_ref.artifact_id}@v{spec_ref.version}")
    out(f"exports:       {exports_dir}")

    if final_state is not None:
        out(f"status:        {final_state.status.value} (GDSII emitted)")
        out("inspect:")
        out(f"  ls {exports_dir}/gds/")
        return

    if paused_state is not None:
        out(f"status:        {paused_state.status.value} "
            f"at {paused_state.current_stage.value}")
        out("approve + resume with:")
        out(
            f"  chip-agent resume --design-id {design_id} "
            f"--run-dir {run_dir}",
        )
        return

    out("hand off with:")
    out(
        f"  chip-agent run --design-id {design_id} --name {name} "
        f"--run-dir {run_dir}",
    )


def _stdout_writer(stream: TextIO | None) -> Callable[[str], None]:
    """Return a ``print``-shaped callable backed by ``stream`` (or stdout)."""
    if stream is None:
        return print

    def write(s: str) -> None:
        stream.write(s)
        stream.write("\n")
        stream.flush()

    return write


# Allow `python -m chip_agent.cli ...` in addition to the entry-point script.
if __name__ == "__main__":  # pragma: no cover — exercised via shell, not pytest
    sys.exit(main())
