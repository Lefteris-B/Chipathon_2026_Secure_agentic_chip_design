"""LibreLane physical implementation as a typed tool service (F6.2).

Drives a LibreLane flow (floorplan → place → cts → route) inside the sandbox
and harvests two outputs:

* the routed ``.def`` file at the configured tag in ``runs/.../final/def/``;
* the consolidated ``metrics.json`` LibreLane writes alongside it.

The parser turns those into a :class:`PhysicalRun` — a transient dataclass,
not an :class:`Artifact` — that the driver consumes to decide whether to
retune and retry (F6.2 inner-loop AC) or hand off to signoff. The
:class:`LayoutArtifact` IS persisted: it's the SYNTH/PHYSICAL stage head
that the signoff stage's STA/DRC/LVS gates read in M6.

Parser and runner are split so the parser is unit-testable against canned
metrics dicts + DEF bytes, and so an integration test can swap in a real
sandbox without touching this module.
"""

from __future__ import annotations

import json
import re
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from chip_agent.design_state import (
    BlobRef,
    DesignConstraints,
    LayoutArtifact,
    NetlistArtifact,
    PhysicalRepairRoute,
    PhysicalRepairRouteKind,
    Provenance,
    Stage,
    ToolRun,
    ToolVersion,
    Violation,
)
from chip_agent.obs.tracing import NoopTracer, SpanKind, Tracer
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools._protocols import SandboxLike

__all__ = [
    "DEFAULT_FLOW_RUN_TAG",
    "LIBRELANE_BIN",
    "LibreLaneFlowError",
    "LibreLanePhysicalService",
    "PhysicalConfig",
    "PhysicalRun",
    "apply_repair_delta",
    "build_librelane_config",
    "from_constraints",
    "parse_librelane_outputs",
]


LIBRELANE_BIN = "librelane"
DEFAULT_FLOW_RUN_TAG = "run_latest"

# Congestion fraction above which a route is considered failed. Real LibreLane
# emits route__congestion__overflow as cells/edges-over-capacity; we treat any
# positive value as a soft failure but only ``> 0.05`` (5%) as a hard one.
_HARD_CONGESTION_THRESHOLD = 0.05


@dataclass(frozen=True)
class PhysicalConfig:
    """The subset of LibreLane variables the F6.2 driver tunes.

    Pinned and small on purpose: the inner-loop retune is deterministic, so
    only fields the driver actually adjusts (``target_utilization``,
    ``target_die_area_um2``) live here. Everything else stays in the
    sandbox-side template config.
    """

    design_name: str
    top_module: str
    clock_period_ns: float | None = None
    target_utilization: float = 0.5
    target_die_area_um2: float | None = None
    std_cell_lib: str = "gf180mcu_fd_sc_mcu7t5v0"
    pdk: str = "gf180mcuD"
    # F21.2: per-corner STA opt-in. Tuple for frozen-dataclass hashability.
    # ``None`` (default) keeps today's typical-only path byte-identical.
    sta_corners: tuple[str, ...] | None = None
    sta_report_power: bool = False
    # F21.3: timing-closure recovery knobs. Both default ``None`` so today's
    # path emits neither ``PL_TARGET_DENSITY`` nor ``SYNTH_STRATEGY`` —
    # LibreLane uses its defaults. The F21.3 dispatcher accumulates routes
    # on ``DesignState.physical_repair_routes`` and the PHYSICAL node walks
    # them through ``apply_repair_delta()`` to produce the active config.
    pl_target_density: float | None = None
    synth_strategy: str | None = None

    def retuned(
        self,
        *,
        utilization_delta: float = 0.05,
        floor_utilization: float = 0.30,
    ) -> PhysicalConfig:
        """Return a copy with ``target_utilization`` lowered by ``delta``.

        Clamped at ``floor_utilization`` so a runaway loop can't drive
        utilization below a sensible floor and silently mask exhaustion.
        Pure — does not mutate ``self``.
        """
        next_u = max(floor_utilization, round(self.target_utilization - utilization_delta, 4))
        return PhysicalConfig(
            design_name=self.design_name,
            top_module=self.top_module,
            clock_period_ns=self.clock_period_ns,
            target_utilization=next_u,
            target_die_area_um2=self.target_die_area_um2,
            std_cell_lib=self.std_cell_lib,
            pdk=self.pdk,
            sta_corners=self.sta_corners,
            sta_report_power=self.sta_report_power,
            pl_target_density=self.pl_target_density,
            synth_strategy=self.synth_strategy,
        )


@dataclass(frozen=True)
class PhysicalRun:
    """Transient result of one LibreLane flow attempt.

    The driver reads ``passed`` + ``congested`` to decide retune-or-give-up;
    ``def_text`` is the bytes of the routed DEF the service then blobs into
    the store as part of the :class:`LayoutArtifact`.
    """

    passed: bool
    stage_reached: str
    congested: bool
    congestion_pct: float
    utilization_pct: float
    die_area_um2: float | None
    violations: list[Violation]
    metrics: dict[str, float]
    def_bytes: bytes = b""
    cell_count: int = 0  # F12.4: total instance count from metrics.json


class LibreLaneFlowError(RuntimeError):
    """The flow could not be invoked at all (e.g. config schema rejected).

    Soft failures — congestion, DRC, flow killed mid-route — are surfaced
    as :class:`Violation`s on the returned :class:`PhysicalRun`; only a
    setup-level wiring bug warrants this exception.
    """


def from_constraints(
    constraints: DesignConstraints,
    *,
    design_name: str,
    top_module: str,
    target_utilization: float = 0.5,
    sta_corners: tuple[str, ...] | None = None,
    sta_report_power: bool = False,
    pl_target_density: float | None = None,
    synth_strategy: str | None = None,
) -> PhysicalConfig:
    """Build a :class:`PhysicalConfig` from project-wide
    :class:`DesignConstraints` plus the per-design knobs.

    ``sta_corners`` / ``sta_report_power`` (F21.2) are operator-level
    settings; defaulting both keeps today's single-corner path
    byte-identical, matching the ``signoff:`` block being unset.
    """
    return PhysicalConfig(
        design_name=design_name,
        top_module=top_module,
        clock_period_ns=constraints.target_clock_ns,
        target_utilization=(
            constraints.target_utilization
            if constraints.target_utilization is not None
            else target_utilization
        ),
        target_die_area_um2=constraints.max_die_area_um2,
        std_cell_lib=constraints.std_cell_lib,
        pdk=constraints.pdk,
        sta_corners=sta_corners,
        sta_report_power=sta_report_power,
        pl_target_density=pl_target_density,
        synth_strategy=synth_strategy,
    )


# F21.3: floor for the LOWER_DENSITY route. Sky130 LibreLane runs typically
# go unplaceable below ~0.30 density; clamping here keeps the dispatcher's
# repair attempts inside the routable band without an extra escape hatch.
_PL_TARGET_DENSITY_FLOOR = 0.30
_PL_TARGET_DENSITY_DELTA = 0.05
# F21.3: the multiplicative factor for the RELAX_CLOCK_PERIOD route. 1.10
# is the standard "first relaxation" the OpenROAD-Flow-Scripts AutoTuner
# docs recommend before giving up on closure at the target frequency.
_RELAX_CLOCK_PERIOD_FACTOR = 1.10
# F21.3: synthesis strategy the INCREASE_DELAY_OPTIMIZATION route picks.
# Yosys / LibreLane recognise ``DELAY 0..4`` (more passes optimising the
# critical path at the cost of area); 3 is the high-effort tier that still
# completes within the budget.
_INCREASE_DELAY_STRATEGY = "DELAY 3"


def apply_repair_delta(
    config: PhysicalConfig, route: PhysicalRepairRoute,
) -> PhysicalConfig:
    """F21.3 — apply a single route's knob delta to a PhysicalConfig.

    Pure function: returns a new immutable :class:`PhysicalConfig` with
    the route's knob applied; ``config`` is unchanged. ``ESCALATE_HUMAN``
    is the identity transform — the dispatcher routes elsewhere before
    calling this function, but treating it as a no-op here keeps the
    PHYSICAL node's reapply loop free of branching.

    Knob effects (see :class:`PhysicalRepairRouteKind` docstrings for
    the rationale):

    * ``LOWER_DENSITY``: ``pl_target_density`` = max(0.30, current - 0.05).
      When ``pl_target_density`` is currently None, the starting point is
      ``target_utilization`` (matches LibreLane's default behaviour where
      ``PL_TARGET_DENSITY`` defaults to ``FP_CORE_UTIL`` when unset).
    * ``INCREASE_DELAY_OPTIMIZATION``: ``synth_strategy`` = ``"DELAY 3"``.
    * ``RELAX_CLOCK_PERIOD``: ``clock_period_ns`` *= 1.10. When the clock
      period is currently None (no clock declared), this is a no-op so
      combinational designs don't crash the dispatcher.
    * ``ESCALATE_HUMAN``: identity.
    """
    if route.kind is PhysicalRepairRouteKind.LOWER_DENSITY:
        current = config.pl_target_density
        if current is None:
            current = config.target_utilization
        new_density = max(
            _PL_TARGET_DENSITY_FLOOR,
            round(current - _PL_TARGET_DENSITY_DELTA, 4),
        )
        return _replace_config(config, pl_target_density=new_density)
    if route.kind is PhysicalRepairRouteKind.INCREASE_DELAY_OPTIMIZATION:
        return _replace_config(config, synth_strategy=_INCREASE_DELAY_STRATEGY)
    if route.kind is PhysicalRepairRouteKind.RELAX_CLOCK_PERIOD:
        if config.clock_period_ns is None:
            return config
        return _replace_config(
            config,
            clock_period_ns=round(
                config.clock_period_ns * _RELAX_CLOCK_PERIOD_FACTOR, 4,
            ),
        )
    # ESCALATE_HUMAN — the dispatcher takes a different path before we
    # get here; identity transform is the defensive shape.
    return config


def _replace_config(config: PhysicalConfig, **overrides: object) -> PhysicalConfig:
    """Return a new frozen PhysicalConfig with the supplied fields replaced.

    Local helper instead of ``dataclasses.replace`` so the type-checker
    catches typo'd field names against the explicit kwarg surface.
    """
    return PhysicalConfig(
        design_name=overrides.get("design_name", config.design_name),  # type: ignore[arg-type]
        top_module=overrides.get("top_module", config.top_module),  # type: ignore[arg-type]
        clock_period_ns=overrides.get(  # type: ignore[arg-type]
            "clock_period_ns", config.clock_period_ns,
        ),
        target_utilization=overrides.get(  # type: ignore[arg-type]
            "target_utilization", config.target_utilization,
        ),
        target_die_area_um2=overrides.get(  # type: ignore[arg-type]
            "target_die_area_um2", config.target_die_area_um2,
        ),
        std_cell_lib=overrides.get(  # type: ignore[arg-type]
            "std_cell_lib", config.std_cell_lib,
        ),
        pdk=overrides.get("pdk", config.pdk),  # type: ignore[arg-type]
        sta_corners=overrides.get("sta_corners", config.sta_corners),  # type: ignore[arg-type]
        sta_report_power=overrides.get(  # type: ignore[arg-type]
            "sta_report_power", config.sta_report_power,
        ),
        pl_target_density=overrides.get(  # type: ignore[arg-type]
            "pl_target_density", config.pl_target_density,
        ),
        synth_strategy=overrides.get(  # type: ignore[arg-type]
            "synth_strategy", config.synth_strategy,
        ),
    )


def build_librelane_config(
    config: PhysicalConfig,
    *,
    verilog_files: list[str],
) -> dict[str, object]:
    """Serialise a :class:`PhysicalConfig` to a LibreLane JSON config dict.

    Only the keys LibreLane recognises end up in the dict; the driver's
    retune surface (``FP_CORE_UTIL``, etc.) is the only thing that varies
    between attempts.
    """
    cfg: dict[str, object] = {
        "DESIGN_NAME": config.design_name,
        "DESIGN_DIR": ".",
        "VERILOG_FILES": verilog_files,
        "PDK": config.pdk,
        "STD_CELL_LIBRARY": config.std_cell_lib,
        # LibreLane uses percent (50.0), not fraction (0.5) — convert at the seam.
        "FP_CORE_UTIL": round(config.target_utilization * 100.0, 2),
    }
    if config.clock_period_ns is not None:
        cfg["CLOCK_PERIOD"] = config.clock_period_ns
        cfg["CLOCK_PORT"] = "clk"
    if config.target_die_area_um2 is not None:
        cfg["DIE_AREA"] = config.target_die_area_um2
    # F21.2: opt-in per-corner STA + power. LibreLane recognises
    # ``STA_CORNERS`` as a list of corner tags (``tt``/``ss``/``ff``);
    # the per-corner liberty files are auto-resolved from the PDK's
    # ``libs.ref/.../{tt,ss,ff}/*.lib`` layout — no additional knob
    # needed. ``STA_REPORT_POWER`` toggles ``report_power`` output
    # alongside the slack reports.
    if config.sta_corners:
        cfg["STA_CORNERS"] = list(config.sta_corners)
    if config.sta_report_power:
        cfg["STA_REPORT_POWER"] = 1
    # F21.3: timing-closure recovery knobs — emit only when set so today's
    # path is byte-identical to pre-F21.3.
    if config.pl_target_density is not None:
        cfg["PL_TARGET_DENSITY"] = config.pl_target_density
    if config.synth_strategy is not None:
        cfg["SYNTH_STRATEGY"] = config.synth_strategy
    return cfg


# Metric keys the parser pulls out of LibreLane's metrics.json. LibreLane
# emits many more — we only care about the F6.2-AC + F12.4-AC-relevant ones.
#
# F13.2: widened to cover the IIC-OSIC-TOOLS chipathon26 image's emit shape.
# The F11.7 live run revealed that the M12 pins (single underscore between
# domain + field) miss the chipathon26 build (double-underscore convention).
# Both variants are now accepted; pinned candidates in priority order, with
# the most common variant per spec first. Future IIC image bumps that drop a
# pinned key now emit a one-line warning via ``_pick_first_with_warn`` so the
# drift surfaces visibly rather than silently leaving ``die_area_um2=None``
# on the layout body.
_CONGESTION_KEYS = (
    "route__congestion__overflow",
    "globalroute__congestion__overflow",  # IIC chipathon26 variant
    "gr__congestion__overflow",           # short variant
    "route__congestion__avg",
    "global_route__congestion__overflow",  # legacy single-underscore form
)
_UTILIZATION_KEYS = (
    "design__core__util",                  # IIC chipathon26 (double underscore)
    "design__instance__utilization",       # variant emitted by some flows
    "floorplan__core_util",                # legacy single-underscore form
    "design__core_util",                   # legacy single-underscore form
    "design__utilization",                 # legacy single-underscore form
    "design__util",                        # short variant
)
_DIE_AREA_KEYS = (
    "design__die__area",                   # IIC chipathon26 (double underscore)
    "design__die__area__um2",              # explicit units variant
    "floorplan__die_area",                 # alternate domain
    "design__die_area",                    # legacy single-underscore form
    "die__area",                           # legacy short form
)
_DRC_KEYS = (
    "magic__drc__errors",                  # canonical IIC chipathon26 form
    "route__drc__errors",                  # variant
    "route__drc_errors",                   # legacy single-underscore form
    "route__drc__violations",              # variant
)
# F12.4 + F13.2: total instance count from LibreLane's metrics.json — the
# canonical source for ``LayoutArtifact.cell_count`` + ``GDSIIArtifact.cell_count``.
_CELL_COUNT_KEYS = (
    "design__instance__count",             # IIC chipathon26 (works post-F12.4)
    "design__instance__count__total",      # variant
    "design__instance_count",              # legacy single-underscore form
    "design__inst_count_total",            # legacy short form
)
_STAGE_REACHED_KEYS = (
    "last__step",
    "flow__last_step",
)


def _pick_first(metrics: dict[str, float], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = metrics.get(k)
        if v is not None:
            return float(v)
    return None


def _pick_first_with_warn(
    metrics: dict[str, float], keys: tuple[str, ...], *, label: str,
) -> float | None:
    """Variant of :func:`_pick_first` that warns when no pinned key matches.

    F13.2: when LibreLane's ``metrics.json`` doesn't carry any of the pinned
    keys for an "always-present" metric (die area, utilization, congestion,
    cell count), emit a one-line warning naming the label so the operator
    knows a future IIC image bump introduced a new key shape. The function
    still returns ``None`` — callers handle the absence the same way they did
    before; the warning is purely diagnostic.
    """
    v = _pick_first(metrics, keys)
    if v is None and metrics:
        warnings.warn(
            f"LibreLane metrics: no pinned key matched for {label!r}; "
            f"tried {list(keys)!r}. Update _LIBRELANE_METRIC_KEYS in "
            f"chip_agent/tools/librelane.py for the new IIC image tag.",
            UserWarning,
            stacklevel=2,
        )
    return v


def parse_librelane_outputs(
    run: ToolRun,
    *,
    metrics: dict[str, float] | None = None,
    def_bytes: bytes = b"",
    stage_reached_override: str | None = None,
) -> PhysicalRun:
    """Parse LibreLane sandbox outputs into a typed :class:`PhysicalRun`.

    Pure of I/O: callers stage the metrics dict + DEF bytes (from disk on
    the real runner, hand-built on the test stub) and pass them in. The
    returned ``passed`` flag reads ``run.returncode == 0``, the DEF was
    produced, AND congestion is below the hard threshold AND no error-
    severity DRC violations rode along.
    """
    metrics = dict(metrics or {})
    violations: list[Violation] = []
    # F13.2: use the warn-on-miss helper for the always-present metrics so
    # future IIC image bumps that introduce a new key shape surface visibly.
    # The DRC + stage-reached lookups stay silent because their absence is
    # itself meaningful (no DRC violations, unknown stage).
    congestion = _pick_first_with_warn(
        metrics, _CONGESTION_KEYS, label="congestion",
    ) or 0.0
    utilization = _pick_first_with_warn(
        metrics, _UTILIZATION_KEYS, label="utilization",
    ) or 0.0
    die_area = _pick_first_with_warn(
        metrics, _DIE_AREA_KEYS, label="die_area",
    )
    drc_errors = int(_pick_first(metrics, _DRC_KEYS) or 0)
    # F12.4: pull the instance count so layout + GDSII carry a non-zero
    # cell_count when LibreLane reports one.
    cell_count = int(_pick_first_with_warn(
        metrics, _CELL_COUNT_KEYS, label="cell_count",
    ) or 0)

    # Stage reached: prefer an explicit override (set by the runner when it
    # detects the DEF file path), else the LibreLane-reported step name.
    if stage_reached_override is not None:
        stage_reached = stage_reached_override
    else:
        stage_reached_raw = None
        for k in _STAGE_REACHED_KEYS:
            v = metrics.get(k)
            if v is not None:
                stage_reached_raw = str(v)
                break
        stage_reached = stage_reached_raw or ("routed" if def_bytes else "unknown")

    is_routed = bool(def_bytes) and stage_reached in {"routed", "route", "detailed_route"}
    congested = congestion > _HARD_CONGESTION_THRESHOLD

    if congested:
        violations.append(Violation(
            code="PHYSICAL.CONGESTION",
            severity="error",
            message=(
                f"global-route congestion overflow {congestion:.3f} exceeds "
                f"hard threshold {_HARD_CONGESTION_THRESHOLD:.3f}"
            ),
            detail={"congestion_overflow": congestion},
        ))
    if drc_errors > 0:
        violations.append(Violation(
            code="PHYSICAL.DRC",
            severity="error",
            message=f"{drc_errors} routing-time DRC violation(s)",
            detail={"drc_errors": drc_errors},
        ))
    if not is_routed and run.returncode == 0:
        # Flow returned 0 but didn't reach `routed` — likely killed by an
        # earlier step. Surface that as a typed failure so the driver retunes.
        violations.append(Violation(
            code="PHYSICAL.FLOW",
            severity="error",
            message=(
                f"flow did not reach the routed stage (stage_reached="
                f"{stage_reached!r})"
            ),
        ))

    metrics_out: dict[str, float] = {
        **metrics,
        "congestion_overflow": congestion,
        "core_utilization": utilization,
        "drc_errors": float(drc_errors),
        "violations": float(len(violations)),
        "errors": float(sum(1 for v in violations if v.severity == "error")),
    }

    passed = (
        run.returncode == 0
        and is_routed
        and not congested
        and drc_errors == 0
    )

    return PhysicalRun(
        passed=passed,
        stage_reached=stage_reached,
        congested=congested,
        congestion_pct=congestion * 100.0,
        utilization_pct=utilization * 100.0,
        die_area_um2=die_area,
        cell_count=cell_count,
        violations=violations,
        metrics=metrics_out,
        def_bytes=def_bytes,
    )


class LibreLanePhysicalService:
    """Run LibreLane's classic flow on a synth netlist and return the routed
    layout + a transient metrics summary."""

    NAME = "librelane"

    def __init__(
        self,
        *,
        sandbox: SandboxLike,
        store: SqliteArtifactStore,
        version_str: str = "bundled",
        config_filename: str = "config.json",
        netlist_filename: str = "design.nl.v",
        flow_run_tag: str = DEFAULT_FLOW_RUN_TAG,
        tracer: Tracer | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.store = store
        self._version_str = version_str
        self._config_filename = config_filename
        self._netlist_filename = netlist_filename
        self._flow_run_tag = flow_run_tag
        self._tracer: Tracer = tracer or NoopTracer()

    def version(self) -> ToolVersion:
        digest = getattr(getattr(self.sandbox, "sandbox", None), "image_digest", None)
        return ToolVersion(
            name=self.NAME,
            version=self._version_str,
            container_digest=digest,
        )

    def run_flow(
        self,
        netlist: NetlistArtifact,
        *,
        config: PhysicalConfig,
        time_limit_s: int | None = None,
    ) -> tuple[LayoutArtifact, PhysicalRun]:
        """Stage ``netlist``, build the LibreLane JSON config, run the
        classic flow inside the sandbox, then assemble the typed pair.

        The :class:`LayoutArtifact` is persisted to the content-addressed
        store (its provenance points at the input netlist). The
        :class:`PhysicalRun` is transient — it never reaches the
        artifact store; the driver consumes it directly.
        """
        with self._tracer.span(
            name="tool:librelane_run_flow", kind=SpanKind.TOOL,
        ) as _span:
            _span.set_attribute("tool_name", self.NAME)
            _span.set_attribute("design_id", netlist.design_id)
            _span.set_attribute("module_id", netlist.module_id or config.top_module)
            _span.set_attribute("clock_period_ns", config.clock_period_ns)
            _span.set_attribute("design_name", config.design_name)
            layout, parse = self._run_flow_inner(
                netlist=netlist, config=config, time_limit_s=time_limit_s,
            )
            _span.set_attribute("passed", parse.passed)
            _span.set_attribute("stage_reached", parse.stage_reached)
            _span.set_attribute("cell_count", parse.cell_count)
            return layout, parse

    def _run_flow_inner(
        self,
        *,
        netlist: NetlistArtifact,
        config: PhysicalConfig,
        time_limit_s: int | None,
    ) -> tuple[LayoutArtifact, PhysicalRun]:
        with tempfile.TemporaryDirectory(prefix="chip-agent-physical-") as td:
            work_path = Path(td)
            netlist_bytes = self.store.get_blob(netlist.netlist)
            (work_path / self._netlist_filename).write_bytes(netlist_bytes)

            cfg_dict = build_librelane_config(
                config, verilog_files=[self._netlist_filename],
            )
            (work_path / self._config_filename).write_text(
                json.dumps(cfg_dict, indent=2, sort_keys=True)
            )

            run = self.sandbox.run(
                [LIBRELANE_BIN, "--run-tag", self._flow_run_tag,
                 self._config_filename],
                mount=work_path,
                time_limit_s=time_limit_s,
                read_only_mount=False,
            )

            harvested = _harvest_outputs(
                work_path, run_tag=self._flow_run_tag, design_name=config.design_name,
                sta_corners=config.sta_corners,
                sta_report_power=config.sta_report_power,
                stderr_text=run.stderr or "",
            )

        # F18: a startup-time LibreLane failure (config rejected, container
        # OOM, missing PDK, image entrypoint error) returns rc!=0 + no DEF
        # in seconds. Pre-fix this silently produced an empty LayoutArtifact
        # and the driver escalated to HUMAN with zero diagnostic signal.
        # Fail loud — the TUI surfaces the real error in the chat, and the
        # captured log blob (below) keeps the audit trail readable.
        if run.returncode != 0 and not harvested.def_bytes:
            body = (run.stderr or run.stdout or "").strip()
            tail = "\n".join(body.splitlines()[-40:]) if body else ""
            raise LibreLaneFlowError(
                f"librelane exited rc={run.returncode} without producing a DEF "
                f"for {config.design_name!r}. Last log lines:\n{tail}"
                if tail
                else f"librelane exited rc={run.returncode} without producing "
                     f"a DEF for {config.design_name!r} and emitted no output."
            )

        parse = parse_librelane_outputs(
            run,
            metrics=harvested.metrics,
            def_bytes=harvested.def_bytes,
            stage_reached_override=harvested.stage_reached,
        )
        # F18: persist the full LibreLane stdout+stderr as a sidecar blob so
        # post-mortem debugging doesn't depend on rerunning the flow. We do
        # this regardless of success so the audit can compare a healthy run
        # against a sick one. Empty output -> None (clear "no log captured").
        log_ref = self._maybe_put_log(run)
        layout = self._build_layout(
            netlist, parse, config=config, harvested=harvested, log_ref=log_ref,
        )
        return layout, parse

    def _maybe_put_log(self, run: ToolRun) -> BlobRef | None:
        body_parts: list[str] = []
        if run.stdout:
            body_parts.append("=== stdout ===\n" + run.stdout)
        if run.stderr:
            body_parts.append("=== stderr ===\n" + run.stderr)
        if not body_parts:
            return None
        return self.store.put_blob(
            "\n".join(body_parts).encode("utf-8", errors="replace"),
            media_type="text/plain",
        )

    def _build_layout(
        self,
        netlist: NetlistArtifact,
        parse: PhysicalRun,
        *,
        config: PhysicalConfig,
        harvested: HarvestedOutputs,
        log_ref: BlobRef | None = None,
    ) -> LayoutArtifact:
        module = netlist.module_id or config.top_module
        def_ref = self.store.put_blob(
            parse.def_bytes, media_type="text/x-def",
        )
        # F12.1: stash the CTS SDF so SIGNOFF's OpenSTA leg can read_sdf it.
        sdf_ref = (
            self.store.put_blob(harvested.sdf_bytes, media_type="text/x-sdf")
            if harvested.sdf_bytes else None
        )
        # F12.2: stash LibreLane's own Magic DRC report so SIGNOFF can
        # cross-check our re-run against it and flag disagreements.
        drc_ref = (
            self.store.put_blob(
                harvested.drc_report_bytes, media_type="text/plain",
            )
            if harvested.drc_report_bytes else None
        )
        # F12.3: stash the extracted SPICE so SIGNOFF's LVS leg can compare
        # the synth netlist against the real layout netlist.
        spice_ref = (
            self.store.put_blob(
                harvested.layout_spice_bytes, media_type="text/x-spice",
            )
            if harvested.layout_spice_bytes else None
        )
        # F13.1: stash the mapped Verilog netlist so SIGNOFF's STA leg
        # uses sky130-mapped cells instead of Yosys's intermediate generic
        # gates (OpenSTA chokes on the latter).
        mapped_ref = (
            self.store.put_blob(
                harvested.mapped_netlist_bytes, media_type="text/x-verilog",
            )
            if harvested.mapped_netlist_bytes else None
        )
        # F13.4-B: stash the powered netlist so SIGNOFF's LVS leg sees
        # port lists that match the extracted SPICE (VPWR/VGND + bit-
        # blasted busses) and Netgen produces a real verdict instead of
        # LVS.UNKNOWN.
        powered_ref = (
            self.store.put_blob(
                harvested.powered_netlist_bytes, media_type="text/x-verilog",
            )
            if harvested.powered_netlist_bytes else None
        )
        # F21.2: stash the per-corner timing + power blobs so SIGNOFF's
        # dispatch can build a MultiCornerSTAReport instead of today's
        # single-corner TimingReport. Empty dicts when the opt-in wasn't
        # requested OR when the sky130A multi-corner segfault was
        # detected; SIGNOFF treats both as "fall back to typical-only".
        per_corner_timing_refs: dict[str, BlobRef] | None = None
        if harvested.per_corner_timing:
            per_corner_timing_refs = {
                corner: self.store.put_blob(b, media_type="text/plain")
                for corner, b in harvested.per_corner_timing.items()
            }
        per_corner_power_refs: dict[str, BlobRef] | None = None
        if harvested.per_corner_power:
            per_corner_power_refs = {
                corner: self.store.put_blob(b, media_type="text/plain")
                for corner, b in harvested.per_corner_power.items()
            }
        tool = self.version()
        return LayoutArtifact(
            artifact_id=f"{netlist.design_id}.{module}.layout",
            design_id=netlist.design_id,
            module_id=netlist.module_id,
            def_file=def_ref,
            stage_reached=parse.stage_reached,
            die_area_um2=parse.die_area_um2,
            utilization_pct=parse.utilization_pct,
            cell_count=parse.cell_count,
            congestion_pct=parse.congestion_pct,
            librelane_sdf=sdf_ref,
            librelane_drc_report=drc_ref,
            librelane_layout_spice=spice_ref,
            librelane_mapped_netlist=mapped_ref,
            librelane_powered_netlist=powered_ref,
            librelane_per_corner_timing=per_corner_timing_refs,
            librelane_per_corner_power=per_corner_power_refs,
            librelane_log=log_ref,
            provenance=Provenance(
                produced_by=Stage.PHYSICAL,
                agent="physical_specialist",
                tool=tool,
                inputs=[netlist.ref()],
                config={
                    "target_utilization": config.target_utilization,
                    "clock_period_ns": config.clock_period_ns,
                    "pdk": config.pdk,
                    "std_cell_lib": config.std_cell_lib,
                },
            ),
            metadata={
                "congestion_pct": parse.congestion_pct,
                "stage_reached": parse.stage_reached,
                # F21.2: SIGNOFF dispatch reads this to emit the
                # MULTI_CORNER_FALLBACK audit event when the per-corner
                # opt-in was requested but LibreLane tripped #6227.
                # Excluded from the content hash via metadata.
                "multi_corner_fallback": (
                    "true" if harvested.multi_corner_fallback else "false"
                ),
            },
        )


@dataclass(frozen=True)
class HarvestedOutputs:
    """Bytes + metadata pulled from a LibreLane run directory.

    Extended in F12.1 (SDF) + F12.2 (DRC report) to surface the
    auxiliary outputs SIGNOFF needs. Empty fields mean "not found" — the
    wrapper tolerates missing pieces because not every flow stage emits
    every artifact.
    """

    def_bytes: bytes
    metrics: dict[str, float]
    stage_reached: str | None
    sdf_bytes: bytes = b""
    drc_report_bytes: bytes = b""
    layout_spice_bytes: bytes = b""
    mapped_netlist_bytes: bytes = b""
    powered_netlist_bytes: bytes = b""
    # F21.2: per-corner STA reports keyed by corner tag (tt/ss/ff). Empty
    # dict when ``sta_corners`` was unset OR when LibreLane's multi-corner
    # STA tripped the sky130A segfault (OpenROAD #6227) — the segfault
    # detection short-circuits the harvest so SIGNOFF takes the typical-
    # only fallback path. Same shape for ``per_corner_power``.
    per_corner_timing: dict[str, bytes] = field(default_factory=dict)
    per_corner_power: dict[str, bytes] = field(default_factory=dict)
    # F21.2: ``True`` when the harvest detected the OpenROAD #6227
    # multi-corner segfault and skipped per-corner collection. The
    # service emits a ``MULTI_CORNER_FALLBACK`` audit event in that case;
    # surfaced here so the layout builder can stamp a metric and so
    # tests can assert the fallback path was taken.
    multi_corner_fallback: bool = False


# F21.2: OpenROAD #6227 multi-corner STA segfault signature on sky130A.
# Matches both the bare ``signal 11`` line and the more specific
# ``OpenROAD ... corner`` framing the issue tracker shows. Conservative
# on purpose: a false positive triggers a clean single-corner fallback
# (no data loss); a false negative leaves empty per-corner blobs and
# SIGNOFF takes the same fallback via the dispatch branch in Phase 7.
_OPENROAD_6227_SIGNATURE_RE = re.compile(
    r"(?:OpenROAD.*?(?:segmentation fault|signal 11|SIGSEGV).*?corner"
    r"|signal 11.*?multi[_-]?corner"
    r"|multi[_-]?corner.*?(?:segmentation fault|signal 11|SIGSEGV))",
    re.IGNORECASE | re.DOTALL,
)


def _detect_multi_corner_segfault(stderr_text: str) -> bool:
    """F21.2: scan LibreLane stderr for the OpenROAD #6227 signature.

    Returns ``True`` iff the multi-corner STA tripped the known sky130A
    segfault and the caller should degrade to single-corner. Empty input
    is a clean ``False`` (no fallback needed).
    """
    if not stderr_text:
        return False
    return _OPENROAD_6227_SIGNATURE_RE.search(stderr_text) is not None


def _harvest_outputs(
    work_path: Path,
    *,
    run_tag: str,
    design_name: str,
    sta_corners: tuple[str, ...] | None = None,
    sta_report_power: bool = False,
    stderr_text: str = "",
) -> HarvestedOutputs:
    """Read the routed DEF + metrics.json + CTS SDF from a LibreLane run dir.

    LibreLane writes under ``runs/<tag>/final/{def,reports}/`` for the
    canonical outputs; the CTS SDF lives under ``runs/<tag>/cts/`` (and a
    duplicate often appears at ``runs/<tag>/final/sdf/<design>.sdf`` in
    newer flow versions). We tolerate a handful of layouts because the
    exact paths drift by flow version — pinned candidates first, then a
    last-resort glob.
    """
    candidates_def = (
        work_path / "runs" / run_tag / "final" / "def" / f"{design_name}.def",
        work_path / "runs" / run_tag / "final" / f"{design_name}.def",
        work_path / "final" / f"{design_name}.def",
    )
    def_bytes = b""
    for c in candidates_def:
        if c.exists():
            def_bytes = c.read_bytes()
            break

    candidates_metrics = (
        work_path / "runs" / run_tag / "final" / "metrics.json",
        work_path / "runs" / run_tag / "metrics.json",
        work_path / "final" / "metrics.json",
    )
    metrics: dict[str, float] = {}
    for c in candidates_metrics:
        if c.exists():
            try:
                parsed = json.loads(c.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(v, int | float):
                        metrics[k] = float(v)
            break

    sdf_bytes = _find_first(
        work_path,
        candidates=(
            work_path / "runs" / run_tag / "final" / "sdf" / f"{design_name}.sdf",
            work_path / "runs" / run_tag / "cts" / f"{design_name}.sdf",
            work_path / "runs" / run_tag / "final" / f"{design_name}.sdf",
        ),
        fallback_glob=f"runs/{run_tag}/**/*.sdf",
    )

    # F12.2: Magic DRC report from LibreLane's `route` step. The exact
    # filename varies — older flows emit ``magic.drc`` / ``magic_drc.rpt``,
    # newer ones use ``<design>.magic.drc`` — so we glob defensively after
    # the pinned candidates.
    drc_report_bytes = _find_first(
        work_path,
        candidates=(
            work_path / "runs" / run_tag / "route" / "reports" / "magic_drc.rpt",
            work_path / "runs" / run_tag / "final" / "reports" / "magic_drc.rpt",
            work_path / "runs" / run_tag / "route" / f"{design_name}.magic.drc",
        ),
        fallback_glob=f"runs/{run_tag}/**/magic*drc*",
    )

    # F12.3: SPICE netlist extracted by LibreLane's ``extract`` step.
    # Path varies — newer flows write under ``final/spice/``, older flows
    # write directly under ``extraction/`` — fall back to a glob.
    layout_spice_bytes = _find_first(
        work_path,
        candidates=(
            work_path / "runs" / run_tag / "final" / "spice" / f"{design_name}.spice",
            work_path / "runs" / run_tag / "extraction" / f"{design_name}.spice",
            work_path / "runs" / run_tag / "final" / f"{design_name}.spice",
        ),
        fallback_glob=f"runs/{run_tag}/**/*.spice",
    )

    # F13.1: final post-synth Verilog netlist with sky130 standard cells
    # mapped in — what SIGNOFF's STA leg wants, NOT the F6.x intermediate
    # Yosys netlist on ``NetlistArtifact``. Path candidates in priority
    # order: the final mapped output first, then the yosys synth output,
    # then powered netlist, then a generic ``*.nl.v`` glob. The PNL
    # fallback is intentional — it's the last-resort source for STA when
    # the unpowered mapped output is missing; F13.4-B's separate harvest
    # below is what LVS actually wants.
    mapped_netlist_bytes = _find_first(
        work_path,
        candidates=(
            work_path / "runs" / run_tag / "final" / "nl" / f"{design_name}.nl.v",
            work_path / "runs" / run_tag / "yosys.synthesis" / f"{design_name}.nl.v",
            work_path / "runs" / run_tag / "final" / "pnl" / f"{design_name}.pnl.v",
            work_path / "runs" / run_tag / "final" / f"{design_name}.nl.v",
        ),
        fallback_glob=f"runs/{run_tag}/**/*.nl.v",
    )

    # F13.4-B: powered netlist (PNL). LVS needs this — its ports +
    # cells carry VPWR/VGND + bit-blasted buses to match the extracted
    # SPICE one-for-one. The unpowered mapped netlist (used by STA) has
    # a different port shape (busses kept whole, no power pins) and
    # makes Netgen bail with ``LVS.UNKNOWN``. Path candidates in priority
    # order match the canonical LibreLane PNL emit; a recursive glob
    # picks up flow versions where the directory layout drifts.
    powered_netlist_bytes = _find_first(
        work_path,
        candidates=(
            work_path / "runs" / run_tag / "final" / "pnl" / f"{design_name}.pnl.v",
            work_path / "runs" / run_tag / "yosys.synthesis" / f"{design_name}.pnl.v",
        ),
        fallback_glob=f"runs/{run_tag}/**/*.pnl.v",
    )

    # F21.2: per-corner STA + power harvest. Only fires when the caller
    # opted in via ``sta_corners``; skipped entirely when LibreLane's
    # multi-corner run tripped the known sky130A segfault so SIGNOFF
    # takes the typical-only fallback. Path probing uses the same
    # candidates-then-glob idiom as the SDF / mapped-netlist harvests
    # above; warn-on-miss surfaces image drift without losing the run.
    per_corner_timing: dict[str, bytes] = {}
    per_corner_power: dict[str, bytes] = {}
    multi_corner_fallback = False
    if sta_corners:
        if _detect_multi_corner_segfault(stderr_text):
            multi_corner_fallback = True
        else:
            for corner in sta_corners:
                timing_bytes = _find_first(
                    work_path,
                    candidates=(
                        work_path / "runs" / run_tag / "final" / "timing" / corner / "sta.rpt",
                        work_path / "runs" / run_tag / "final" / "timing" / f"sta_{corner}.rpt",
                        work_path / "runs" / run_tag / "signoff" / "timing" / corner / "sta.rpt",
                        work_path / "runs" / run_tag / "signoff" / f"sta_{corner}.rpt",
                    ),
                    fallback_glob=f"runs/{run_tag}/**/sta_{corner}*.rpt",
                )
                if timing_bytes:
                    per_corner_timing[corner] = timing_bytes
                if sta_report_power:
                    power_bytes = _find_first(
                        work_path,
                        candidates=(
                            work_path / "runs" / run_tag / "final" / "timing" / corner / "power.rpt",
                            work_path / "runs" / run_tag / "final" / "reports" / f"power_{corner}.rpt",
                            work_path / "runs" / run_tag / "signoff" / "timing" / corner / "power.rpt",
                        ),
                        fallback_glob=f"runs/{run_tag}/**/power_{corner}*.rpt",
                    )
                    if power_bytes:
                        per_corner_power[corner] = power_bytes
            # Warn-on-miss when sta_corners was requested but nothing landed
            # — operator sees a one-liner pointing at the path probes that
            # came up empty instead of silently reverting to single-corner.
            if not per_corner_timing:
                warnings.warn(
                    f"LibreLane STA harvest: no per-corner reports found for "
                    f"requested sta_corners={list(sta_corners)!r}. Probed "
                    f"runs/{run_tag}/final/timing/<corner>/sta.rpt + variants. "
                    f"SIGNOFF will fall back to single-corner. Update the path "
                    f"candidates in chip_agent/tools/librelane.py:_harvest_outputs "
                    f"for the new IIC image tag.",
                    stacklevel=2,
                )

    stage_reached: str | None = None
    if def_bytes:
        stage_reached = "routed"
    return HarvestedOutputs(
        def_bytes=def_bytes, metrics=metrics, stage_reached=stage_reached,
        sdf_bytes=sdf_bytes, drc_report_bytes=drc_report_bytes,
        layout_spice_bytes=layout_spice_bytes,
        mapped_netlist_bytes=mapped_netlist_bytes,
        powered_netlist_bytes=powered_netlist_bytes,
        per_corner_timing=per_corner_timing,
        per_corner_power=per_corner_power,
        multi_corner_fallback=multi_corner_fallback,
    )


def _find_first(
    work_path: Path,
    *,
    candidates: tuple[Path, ...],
    fallback_glob: str | None = None,
) -> bytes:
    """Return the bytes of the first existing candidate, or the glob match.

    Used by the harvest pass for paths that drift across LibreLane
    versions: try the pinned spots first, fall back to a recursive glob
    so a tag bump doesn't require an immediate code change.
    """
    for c in candidates:
        if c.exists():
            return c.read_bytes()
    if fallback_glob is None:
        return b""
    matches = sorted(work_path.glob(fallback_glob))
    if not matches:
        return b""
    return matches[0].read_bytes()
