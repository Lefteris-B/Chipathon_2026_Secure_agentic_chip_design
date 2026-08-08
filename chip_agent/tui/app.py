"""Main Textual App for chip-agent (M14).

Single-window app mounting the chat pane (F14.1) + pipeline pane
(F14.2). F14.3 adds drive controls: ``[R]`` to start ``cmd_run``, ``[A]``
to ``cmd_resume`` past the human gate, ``[Q]`` to quit. Workers run on
threads (see :mod:`chip_agent.tui.workers.run_worker`); the app posts
notifications + records the produced :class:`DesignState` snapshots.

The app's :meth:`run` returns a :class:`TuiResult` carrying ``spec``,
``paused_state``, ``final_state`` — whichever the operator reached
before quitting. ``cmd_tui`` in ``chip_agent.cli`` maps the result onto
the existing ``TuiOutcome`` shape.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical

from chip_agent.design_state import (
    DesignState,
    DesignStatus,
    FailureDiagnosis,
    ModelRouter,
    Spec,
    Stage,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.settings import ConstraintDefaults, RoutingSettings
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tui.messages import (
    PipelineCompleted,
    PipelinePaused,
    RunFailed,
    SpecMaterialised,
    StageAdvanced,
)
from chip_agent.tui.panes.audit import AuditPane
from chip_agent.tui.panes.chat import ChatPane
from chip_agent.tui.panes.exports import ExportsPane
from chip_agent.tui.panes.help import HelpScreen
from chip_agent.tui.panes.human_repair import HumanRepairScreen, format_diagnosis
from chip_agent.tui.panes.model_picker import ModelPickerScreen
from chip_agent.tui.panes.new_run import NewRunConfirmScreen
from chip_agent.tui.panes.pipeline import PipelinePane
from chip_agent.tui.panes.reasoning_tape import ReasoningTape
from chip_agent.tui.panes.signoff import SignoffDashboardScreen
from chip_agent.tui.workers.run_worker import resume_pipeline, run_pipeline

if TYPE_CHECKING:
    from chip_agent.cli import RunArgs

__all__ = ["ChipAgentApp", "TuiResult"]


@dataclass(frozen=True)
class TuiResult:
    """Outcome of one TUI session.

    Populated incrementally as the operator advances through the chain:

    * ``spec`` — set when ``/run`` in the chat pane mints a Spec.
    * ``paused_state`` — set when ``cmd_run`` (via ``[R]``) returns.
    * ``final_state`` — set when ``cmd_resume`` (via ``[A]``) returns
      ``DesignStatus.COMPLETED``.

    ``cmd_tui`` reads these fields to decide which post-exit hint to
    print (still mid-chat / spec-only / paused / completed).
    """

    spec: Spec | None = None
    paused_state: DesignState | None = None
    final_state: DesignState | None = None


class ChipAgentApp(App[TuiResult]):
    """Single-window chip-agent TUI.

    :meth:`run` returns a :class:`TuiResult` summarising how far the
    operator drove the chain before quitting.
    """

    TITLE = "chip-agent"
    SUB_TITLE = "Textual TUI"

    # F14.5: two-column layout — chat + pipeline on the left, exports +
    # audit on the right. The pipeline strip is narrow vertically so
    # most of the chat is visible; on the right, the directory tree
    # takes ~40% of the column with the rest going to preview + audit.
    CSS = """
    Screen > #main-row {
        layout: horizontal;
        height: 1fr;
    }

    Screen > #main-row > #left-column {
        width: 1fr;
        layout: vertical;
    }

    Screen > #main-row > #right-column {
        width: 1fr;
        layout: vertical;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        # F14.3: drive keybinds use chord prefixes so they don't collide
        # with letters typed into the chat input. Use ``Ctrl+R`` for run,
        # ``Ctrl+A`` for approve, ``Ctrl+Q`` for quit. F14.6 can revisit
        # with a help screen + a more discoverable scheme if needed.
        Binding("ctrl+r", "run", "Run pipeline", show=True, priority=True),
        Binding("ctrl+a", "approve", "Approve + resume", show=True, priority=True),
        # F14.6: signoff dashboard + help screen. Ctrl+S opens the
        # 4-quadrant STA/DRC/LVS/Security summary; Ctrl+H opens the
        # keybinding reference. Both are modals — Esc closes.
        Binding("ctrl+s", "signoff", "Signoff dashboard", show=True, priority=True),
        Binding("ctrl+h", "help", "Help", show=True, priority=True),
        # Ctrl+N: mint a fresh design_id mid-session + remount the four
        # panes. Pushes a confirm modal first so accidental presses are
        # caught. Refused while a worker is in flight.
        Binding("ctrl+n", "new_run", "New run", show=True, priority=True),
        # F15.1: Ctrl+L (not Ctrl+M — terminal protocol Enter is ^M, so
        # it would collide with the chat input) pushes the model picker
        # modal. F15.1 ships read-only; F15.2 adds the editing path.
        Binding("ctrl+l", "models", "Models", show=True, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=True, priority=True),
    ]

    def __init__(
        self,
        *,
        router: ModelRouter,
        store: SqliteArtifactStore,
        design_id: str,
        name: str,
        transcript_path: Path,
        checkpoint_path: Path,
        audit_db_path: Path,
        hmac_key: bytes,
        exports_dir: Path,
        # F22.2-D: Optional so test harnesses that don't care about the
        # reasoning surface stay terse. When None we fall back to
        # ``<run-dir>/reasoning/<design_id>.jsonl`` derived from the
        # other args, mirroring the cli.py wiring.
        reasoning_jsonl_path: Path | None = None,
        run_args_factory: _RunArgsFactory,
        defaults: ConstraintDefaults | None = None,
        routing: RoutingSettings | None = None,
        mint_design_id: Callable[[], str] | None = None,
        transcript_path_fn: Callable[[str], Path] | None = None,
        exports_dir_fn: Callable[[str], Path] | None = None,
        reasoning_jsonl_path_fn: Callable[[str], Path] | None = None,
    ) -> None:
        super().__init__()
        self._router = router
        self._store = store
        self._design_id = design_id
        self._design_name = name
        self._transcript_path = transcript_path
        self._checkpoint_path = checkpoint_path
        self._audit_db_path = audit_db_path
        self._hmac_key = hmac_key
        self._exports_dir = exports_dir
        # F22.2-D: per-run reasoning JSONL file. The ReasoningTape pane
        # tails this path at 500 ms; the JsonlTracer (wired in the run
        # worker) appends to it for every LLM call + tool invocation.
        # When the caller didn't supply one (test harnesses), default
        # to a path next to the audit DB so the pane still has a stable
        # location to watch.
        self._reasoning_jsonl_path = (
            reasoning_jsonl_path
            or audit_db_path.parent / "reasoning" / f"{design_id}.jsonl"
        )
        self._defaults = defaults
        # F15.1: live routing settings — the router was constructed over
        # this same RoutingSettings instance, so mutating it (F15.2) takes
        # effect on the next router.generate call. Optional so test
        # harnesses that don't care about the picker can skip wiring it;
        # action_models notifies + no-ops when None.
        self._routing = routing
        # F14.3: a callable that yields RunArgs for "run" / "resume". The
        # callable lives outside the app so cli.cmd_tui can re-use the
        # exact same args-build flow as cmd_run / cmd_resume use directly.
        # Now also takes the active design_id so Ctrl+N's reset can drive
        # a freshly-minted id through the same factory.
        self._run_args_factory = run_args_factory
        # Ctrl+N support: when these three are wired, the operator can
        # mint a new design_id mid-session. When ``mint_design_id`` is
        # ``None`` the keybind is a friendly no-op — the app stays
        # usable in test harnesses that don't care about new-run.
        self._mint_design_id = mint_design_id
        self._transcript_path_fn = transcript_path_fn
        self._exports_dir_fn = exports_dir_fn
        self._reasoning_jsonl_path_fn = reasoning_jsonl_path_fn
        # State tracked across actions + messages.
        self._spec: Spec | None = None
        self._paused_state: DesignState | None = None
        self._final_state: DesignState | None = None
        self._driving = False  # True while a run/resume worker is in flight

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-row"):
            with Vertical(id="left-column"):
                yield ChatPane(
                    router=self._router,
                    store=self._store,
                    design_id=self._design_id,
                    name=self._design_name,
                    transcript_path=self._transcript_path,
                    defaults=self._defaults,
                )
                yield PipelinePane(
                    design_id=self._design_id,
                    checkpoint_path=self._checkpoint_path,
                )
            with Vertical(id="right-column"):
                yield ExportsPane(exports_root=self._exports_dir)
                yield ReasoningTape(jsonl_path=self._reasoning_jsonl_path)
                yield AuditPane(
                    design_id=self._design_id,
                    audit_db_path=self._audit_db_path,
                    hmac_key=self._hmac_key,
                )

    # --------------------------------------------------------------- actions
    def action_run(self) -> None:
        """[R] — spawn ``cmd_run`` against the chat-minted Spec."""
        if self._spec is None:
            self.notify(
                "No Spec yet — describe the module and /run first.",
                severity="warning",
            )
            return
        if self._driving:
            self.notify("Pipeline already running.", severity="warning")
            return
        if self._paused_state is not None:
            self.notify(
                "Run already completed cmd_run — press [A] to approve.",
                severity="warning",
            )
            return
        self._driving = True
        self.notify("Running pipeline (cmd_run)…")
        args = self._run_args_factory("run", self._design_id)
        self.run_worker(
            lambda: run_pipeline(app=self, pane=self, args=args),
            thread=True, exclusive=False, group="pipeline-run",
        )

    def action_approve(self) -> None:
        """[A] — spawn ``cmd_resume`` from the paused state."""
        if self._paused_state is None:
            self.notify(
                "Pipeline is not at the human gate yet.",
                severity="warning",
            )
            return
        # F23.5 Option B: an interactive-repair pause is addressed by giving
        # guidance, not by approving a tapeout — (re-)open the hint modal.
        if self._paused_state.pending_human_repair is not None:
            self._prompt_human_repair(self._paused_state)
            return
        # Only the normal end-of-flow gate (paused at SIGNOFF with status
        # AWAITING_HUMAN) can advance through to GDSII. If the spine
        # paused at an EARLIER stage because a repair budget exhausted
        # and the F12.5 frontier-fallback also failed, cmd_resume would
        # drive into ``gdsii_emit`` with no PHYSICAL head and crash with
        # MissingHeadError. Refuse loudly instead.
        if self._paused_state.current_stage is not Stage.SIGNOFF:
            self.notify(
                f"Spine paused at {self._paused_state.current_stage.value!r}, "
                "not at the SIGNOFF gate. The chain is stuck — "
                "cmd_resume can't recover an earlier-stage failure. "
                "Restart the run after revisiting the spec.",
                severity="error",
                timeout=15.0,
            )
            return
        if self._driving:
            self.notify("Resume already running.", severity="warning")
            return
        self._driving = True
        self.notify("Approving + resuming (cmd_resume)…")
        args = self._run_args_factory("resume", self._design_id)
        self.run_worker(
            lambda: resume_pipeline(app=self, pane=self, args=args),
            thread=True, exclusive=False, group="pipeline-resume",
        )

    # -------------------------------------------------------- message handlers
    def on_spec_materialised(self, message: SpecMaterialised) -> None:
        """Chat pane minted a Spec — record it for the [R] action."""
        self._spec = message.spec

    def on_stage_advanced(self, message: StageAdvanced) -> None:
        """F22.1-D: forward the freshly-polled DesignState to the audit
        pane's activity banner. The pipeline pane already handles its
        own ``StageAdvanced`` to update its cells; this app-level
        handler runs in parallel and pushes the same state into the
        banner's "Now" line. Without it the banner stays blank because
        ``StageAdvanced`` bubbles up the pipeline pane's branch of the
        DOM, not through the audit pane (a sibling)."""
        try:
            audit = self.query_one(AuditPane)
        except Exception:
            # Pane not mounted (e.g. mid-remount during Ctrl+N) — drop
            # silently. The next StageAdvanced tick will catch up.
            return
        audit.apply_design_state(message.design)

    def on_pipeline_paused(self, message: PipelinePaused) -> None:
        """``cmd_run`` returned — store the paused state, enable [A].

        F23.5 Option B: when the pause carries a ``pending_human_repair``
        request (an RTL escalation, not the pre-GDSII success gate), open
        the interactive-repair modal to capture guidance instead.
        """
        self._paused_state = message.state
        self._driving = False
        if message.state.pending_human_repair is not None:
            self._prompt_human_repair(message.state)
            return
        if message.state.status is DesignStatus.AWAITING_HUMAN:
            self.notify("Pipeline paused at human gate. Press [A] to approve.")
        else:
            self.notify(
                f"cmd_run returned with status={message.state.status.value!r}.",
                severity="warning",
            )

    def _prompt_human_repair(self, state: DesignState) -> None:
        """Push the interactive-repair modal for a parked request."""
        pending = state.pending_human_repair
        if pending is None:
            return
        diagnosis = None
        if pending.diagnosis_ref is not None:
            try:
                loaded = self._store.get(pending.diagnosis_ref)
                if isinstance(loaded, FailureDiagnosis):
                    diagnosis = loaded
            except Exception:
                diagnosis = None
        self.notify(
            f"RTL repair stuck on {pending.module_id!r} — enter guidance.",
            severity="warning",
        )
        self.push_screen(
            HumanRepairScreen(
                module_id=pending.module_id,
                body=format_diagnosis(diagnosis),
            ),
            self._on_repair_hint,
        )

    def _on_repair_hint(self, hint: str | None) -> None:
        """Modal returned — resume with the hint, or leave the run blocked."""
        if not hint:
            self.notify(
                "No guidance given — the run stays blocked. "
                "Re-open with the pause, or Ctrl+Q to quit.",
                severity="warning",
            )
            return
        if self._driving:
            self.notify("Resume already running.", severity="warning")
            return
        self._driving = True
        self.notify("Resuming with your guidance (cmd_resume --hint)…")
        args = replace(
            self._run_args_factory("resume", self._design_id), hint=hint,
        )
        self.run_worker(
            lambda: resume_pipeline(app=self, pane=self, args=args),
            thread=True, exclusive=False, group="pipeline-resume",
        )

    def on_pipeline_completed(self, message: PipelineCompleted) -> None:
        """``cmd_resume`` returned — store the final state and stay open.

        We deliberately do NOT auto-exit on completion: when the GDS lands
        the user wants a beat to inspect the signoff dashboard, the export
        manifest, and the chat scrollback before the terminal closes.
        Pressing ``Ctrl+Q`` (or ``Ctrl+C``) runs ``action_quit`` which
        snapshots the same :class:`TuiResult` the auto-exit used to emit,
        so ``cmd_tui`` post-exit hints still fire correctly.
        """
        self._final_state = message.state
        self._driving = False
        self.notify(
            "Pipeline COMPLETED — GDS ready. Press Ctrl+Q to quit "
            "when you're done inspecting.",
            timeout=15.0,
        )

    def on_run_failed(self, message: RunFailed) -> None:
        """A drive worker raised — surface + restore the keybinds."""
        self._driving = False
        self.notify(
            f"{message.stage} failed: {message.message}",
            severity="error",
            timeout=10.0,
        )

    # ------------------------------------------------------------- modals
    def action_signoff(self) -> None:
        """[Ctrl+S] — push the 4-quadrant signoff dashboard modal.

        Reads the F11.6-mirrored signoff JSONs from ``exports_dir/signoff``
        on mount, auto-detecting the top-module filename prefix.
        Works even mid-flow — missing legs render as "awaiting",
        which is preferable to refusing to open until the spine
        reaches SIGNOFF.
        """
        self.push_screen(
            SignoffDashboardScreen(exports_dir=self._exports_dir),
        )

    def action_help(self) -> None:
        """[Ctrl+H] — push the help / keybinding reference modal."""
        self.push_screen(HelpScreen())

    def action_models(self) -> None:
        """[Ctrl+L] — push the model picker.

        Refused while a worker is in flight — parity with Ctrl+N.
        F15.2: the modal dismisses with a ``{task: model_key}`` diff
        dict (the operator's Apply); ``None`` cancels; ``{}`` means
        Apply was clicked with no changes.
        """
        if self._driving:
            self.notify(
                "Wait for the current run to finish before opening the "
                "model picker.",
                severity="warning",
            )
            return
        if self._routing is None:
            self.notify(
                "Model picker isn't wired in this session.",
                severity="warning",
            )
            return
        self.push_screen(
            ModelPickerScreen(routing=self._routing),
            self._on_models_applied,
        )

    def _on_models_applied(
        self, diff: dict[str, dict[str, dict[str, Any]]] | None,
    ) -> None:
        """Modal callback.

        ``diff`` shape (F15.5): nested per-binding partials::

            {
                "tasks": {name: {"model"?, "temperature"?, "n"?}, ...},
                "loops": {name: {...}, ...},
            }

        Each PartialBinding inner dict carries only the fields the
        operator changed. ``None`` cancels; all subdicts empty means
        Apply with no changes (no-op).
        """
        if diff is None:
            return
        task_changes = diff.get("tasks") or {}
        loop_changes = diff.get("loops") or {}
        if not task_changes and not loop_changes:
            return
        try:
            self.apply_routing_change(tasks=task_changes, loops=loop_changes)
        except ValueError as e:
            self.notify(f"Routing change rejected: {e}", severity="error")
            return
        parts: list[str] = []
        if task_changes:
            parts.append(f"{len(task_changes)} task(s)")
        if loop_changes:
            parts.append(f"{len(loop_changes)} loop(s)")
        self.notify(
            f"Routing updated for {' + '.join(parts)}; "
            "next run will use the new bindings.",
        )

    def apply_routing_change(
        self,
        *,
        tasks: dict[str, dict[str, Any]] | None = None,
        loops: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Mutate any field on any binding in the live routing.

        ``tasks`` / ``loops``: ``{name: {"model"?, "temperature"?,
        "n"?}}`` per kind. Each inner dict is a partial binding spec —
        only the supplied fields are changed; others stay. Validation
        runs across all supplied fields before any mutation — raises
        :class:`ValueError` with a specific message on the first failed
        check, leaving routing unchanged (atomic across kinds + fields).

        Validation rules (mirror the Pydantic field constraints on
        :class:`TaskBinding` / :class:`LoopBinding`):

        * ``model`` must exist in ``self._routing.registry``.
        * ``temperature`` must lie in ``[0.0, 2.0]``.
        * ``n`` must be an int ``>= 1``.

        The mutation propagates to the live router because both the
        router and this app share the same :class:`RoutingSettings`
        instance — every ``LiteLLMRouter.generate`` call re-reads
        ``self.routing.tasks[task]`` / ``loops[slot]`` so the next
        agent invocation picks up the new model / temperature / n.

        F15.3-F15.5: a successful call appends one
        :class:`EventType.ROUTING_CHANGED` entry to the audit log
        carrying the combined ``{tasks, loops}`` partial-binding diff
        (one event per Apply, regardless of how many bindings + fields
        changed).
        """
        if self._routing is None:
            raise ValueError("routing is not wired on this app")
        tasks = tasks or {}
        loops = loops or {}
        if not tasks and not loops:
            return
        # Validate every supplied field across both blocks. Atomic.
        self._validate_partial_block(
            block="tasks", changes=tasks, owned=self._routing.tasks,
        )
        self._validate_partial_block(
            block="loops", changes=loops, owned=self._routing.loops,
        )
        # Capture old field values for the changed bindings BEFORE
        # mutating, so the audit event records what the operator
        # swapped from. Only changed fields appear in the snapshot.
        old_tasks = self._snapshot_old(
            tasks, store=self._routing.tasks,
        )
        old_loops = self._snapshot_old(
            loops, store=self._routing.loops,
        )
        # All validations passed — mutate.
        self._mutate_block(tasks, store=self._routing.tasks)
        self._mutate_block(loops, store=self._routing.loops)
        # F15.3-F15.5: one audit event for the combined diff.
        self._append_routing_changed_event(
            old={"tasks": old_tasks, "loops": old_loops},
            new={"tasks": dict(tasks), "loops": dict(loops)},
        )

    def _validate_partial_block(
        self,
        *,
        block: str,  # "tasks" / "loops" — only used in error messages
        changes: dict[str, dict[str, Any]],
        owned: dict[str, Any],
    ) -> None:
        """Validate every partial binding change in ``changes``.

        Raises :class:`ValueError` with a specific message on the
        first failure. No mutation lands here — the caller mutates
        after every block validates clean.
        """
        assert self._routing is not None
        kind_singular = block.rstrip("s")  # "task" / "loop"
        for name, partial in changes.items():
            if name not in owned:
                raise ValueError(
                    f"unknown {kind_singular} {name!r}; "
                    f"available: {sorted(owned)}",
                )
            for field, value in partial.items():
                if field == "model":
                    if value not in self._routing.registry:
                        raise ValueError(
                            f"model {value!r} is not in registry; "
                            f"available: {sorted(self._routing.registry)}",
                        )
                elif field == "temperature":
                    if not isinstance(value, int | float) or not (
                        0.0 <= float(value) <= 2.0
                    ):
                        raise ValueError(
                            f"{name}: temperature {value!r} outside [0.0, 2.0]",
                        )
                elif field == "n":
                    if not isinstance(value, int) or value < 1:
                        raise ValueError(
                            f"{name}: n {value!r} must be an int >= 1",
                        )
                else:
                    raise ValueError(
                        f"{name}: unknown field {field!r}; "
                        "allowed: model, temperature, n",
                    )

    def _snapshot_old(
        self,
        changes: dict[str, dict[str, Any]],
        *,
        store: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Return ``{name: {field: old_value, ...}}`` mirroring the
        shape of ``changes`` but pulling the BEFORE values from
        ``store``. Each old field corresponds to a field the operator
        is changing."""
        snapshot: dict[str, dict[str, Any]] = {}
        for name, partial in changes.items():
            binding = store[name]
            snapshot[name] = {
                field: getattr(binding, field) for field in partial
            }
        return snapshot

    def _mutate_block(
        self,
        changes: dict[str, dict[str, Any]],
        *,
        store: dict[str, Any],
    ) -> None:
        """Set every supplied field on every named binding."""
        for name, partial in changes.items():
            binding = store[name]
            for field, value in partial.items():
                setattr(binding, field, value)

    def _append_routing_changed_event(
        self,
        *,
        old: dict[str, dict[str, dict[str, Any]]],
        new: dict[str, dict[str, dict[str, Any]]],
    ) -> None:
        """Open the audit log for one write + append a ROUTING_CHANGED row.

        The audit DB file is created on first append if it doesn't yet
        exist — Ctrl+L can fire before the operator has ever pressed
        Ctrl+R, and the chain should still capture the change. The
        connection is opened + closed per event because SqliteAuditLog
        is single-writer single-connection; the AuditPane poller has
        its own read-only WAL connection.
        """
        log = SqliteAuditLog(
            db_path=self._audit_db_path, hmac_key=self._hmac_key,
        )
        try:
            log.append(
                design_id=self._design_id,
                event_type=EventType.ROUTING_CHANGED,
                payload={"kind": "routing", "old": old, "new": new},
            )
        finally:
            log.close()

    def action_new_run(self) -> None:
        """[Ctrl+N] — push the confirm modal to start a fresh design_id.

        Refused while a worker is in flight (resetting state mid-run
        leaves the worker zombie-pointing at an orphaned design_id).
        No-op when ``mint_design_id`` wasn't supplied at construction —
        test harnesses don't need it.
        """
        if self._driving:
            self.notify(
                "Wait for the current run to finish before starting a new one.",
                severity="warning",
            )
            return
        if self._mint_design_id is None:
            self.notify(
                "New-run isn't wired in this session.",
                severity="warning",
            )
            return
        self.push_screen(NewRunConfirmScreen(), self._on_new_run_confirmed)

    def _on_new_run_confirmed(self, confirmed: bool | None) -> None:
        """Confirm-modal callback — when ``True``, swap to a fresh design.

        Modal dismisses with ``True`` (confirm) / ``False`` (cancel);
        ``None`` is treated as cancel for safety.
        """
        if not confirmed:
            return
        self._reset_for_new_run()

    def _reset_for_new_run(self) -> None:
        """Mint a fresh design_id, reset in-memory state, remount panes."""
        assert self._mint_design_id is not None  # gated by action_new_run
        new_id = self._mint_design_id()
        self._design_id = new_id
        self._spec = None
        self._paused_state = None
        self._final_state = None
        # Per-design paths refresh when the callables are wired; otherwise
        # the panes will reuse the existing transcript + exports paths,
        # which is fine for the (test) case where those callables aren't
        # provided.
        if self._transcript_path_fn is not None:
            self._transcript_path = self._transcript_path_fn(new_id)
        if self._exports_dir_fn is not None:
            self._exports_dir = self._exports_dir_fn(new_id)
        if self._reasoning_jsonl_path_fn is not None:
            self._reasoning_jsonl_path = self._reasoning_jsonl_path_fn(new_id)
        self._remount_panes()
        self.notify(f"New run started — design_id = {new_id}")

    def _remount_panes(self) -> None:
        """Drop the four pane widgets + mount fresh instances.

        Keeps the Horizontal / Vertical containers in place — we only
        swap the leaf panes so the columns + main-row IDs stay
        consistent across new-runs (later code paths can rely on them).
        """
        left = self.query_one("#left-column", Vertical)
        right = self.query_one("#right-column", Vertical)
        left.remove_children()
        right.remove_children()
        left.mount(
            ChatPane(
                router=self._router,
                store=self._store,
                design_id=self._design_id,
                name=self._design_name,
                transcript_path=self._transcript_path,
                defaults=self._defaults,
            ),
            PipelinePane(
                design_id=self._design_id,
                checkpoint_path=self._checkpoint_path,
            ),
        )
        right.mount(
            ExportsPane(exports_root=self._exports_dir),
            ReasoningTape(jsonl_path=self._reasoning_jsonl_path),
            AuditPane(
                design_id=self._design_id,
                audit_db_path=self._audit_db_path,
                hmac_key=self._hmac_key,
            ),
        )

    # ---------------------------------------------------------- lifecycle
    async def action_quit(self) -> None:
        """[Q] / Ctrl+C — exit with the current snapshot.

        Signature matches Textual's base ``App.action_quit`` (async).
        The actual exit work is sync; the ``async`` is just so the
        override type-checks.
        """
        self.exit(result=self._snapshot())

    def _snapshot(self) -> TuiResult:
        return TuiResult(
            spec=self._spec,
            paused_state=self._paused_state,
            final_state=self._final_state,
        )


# Type alias kept in quotes to dodge a circular import at module load —
# ``chip_agent.cli`` imports from this module to build the app.
if TYPE_CHECKING:
    _RunArgsFactory = Callable[[str, str], "RunArgs"]
else:
    _RunArgsFactory = Callable[[str, str], object]
