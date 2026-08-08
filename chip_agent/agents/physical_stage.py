"""Physical implementation stage driver (F6.2).

Runs :class:`LibreLanePhysicalService` in a bounded inner-loop:

* attempt 1: the service is called with the supplied :class:`PhysicalConfig`;
* on a **congested** failure (route overflow above the hard threshold)
  the config is retuned (``target_utilization`` lowered by a fixed delta)
  and the service is re-invoked;
* the loop stops on the first ``passed`` result OR when
  ``max_inner_attempts`` is reached. Exhaustion escalates to ``OUTER`` so
  the control graph can hand a frontier model the failure picture; HUMAN
  only fires when ``decide_transition`` rules out further repair.

The retune rule is intentionally deterministic — F6.2's AC is "a forced-
overcongested config triggers a config retune attempt", which means the
*existence* of a config retune in the loop, not a model-driven one. A
semantic outer loop (RTL re-open via :mod:`chip_agent.graph.feedback`,
or a CONFIG_TUNE-routed LLM call) plugs in later without changing this
driver's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chip_agent.design_state import (
    ArtifactRef,
    EscalationLevel,
    LayoutArtifact,
    NetlistArtifact,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.librelane import (
    LibreLanePhysicalService,
    PhysicalConfig,
    PhysicalRun,
)

__all__ = [
    "PhysicalStageDriver",
    "PhysicalStageError",
    "PhysicalStageOutcome",
]


class PhysicalStageError(ValueError):
    """The driver was misconfigured (design_id mismatch, bad budget, …)."""


@dataclass(frozen=True)
class PhysicalStageOutcome:
    """Result of one ``drive`` call.

    ``passed`` is True iff the most recent attempt's gate closed.
    ``escalate_to`` is ``None`` on success, ``OUTER`` when the inner loop
    exhausted its budget but the cause is repairable (signoff/RTL feedback),
    and ``HUMAN`` only when the driver itself decided the work can't be
    completed (e.g. a non-congestion hard failure on the first attempt
    when no retune is meaningful).
    """

    passed: bool
    escalate_to: EscalationLevel | None
    layout: LayoutArtifact | None
    layout_ref: ArtifactRef | None
    final_run: PhysicalRun
    attempts: int
    runs: list[PhysicalRun] = field(default_factory=list)
    final_config: PhysicalConfig | None = None


class PhysicalStageDriver:
    """Run LibreLane up to ``max_inner_attempts`` times with deterministic
    config retune on congestion."""

    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_UTIL_DELTA = 0.05
    DEFAULT_UTIL_FLOOR = 0.30

    def __init__(
        self,
        *,
        service: LibreLanePhysicalService,
        store: SqliteArtifactStore,
        design_id: str,
        max_inner_attempts: int = DEFAULT_MAX_ATTEMPTS,
        utilization_delta: float = DEFAULT_UTIL_DELTA,
        floor_utilization: float = DEFAULT_UTIL_FLOOR,
    ) -> None:
        if not design_id:
            raise PhysicalStageError("design_id must be non-empty")
        if max_inner_attempts < 1:
            raise PhysicalStageError("max_inner_attempts must be >= 1")
        if not 0.0 < utilization_delta < 1.0:
            raise PhysicalStageError("utilization_delta must lie in (0, 1)")
        if not 0.0 < floor_utilization <= 1.0:
            raise PhysicalStageError("floor_utilization must lie in (0, 1]")
        self.service = service
        self.store = store
        self.design_id = design_id
        self.max_inner_attempts = max_inner_attempts
        self.utilization_delta = utilization_delta
        self.floor_utilization = floor_utilization

    def drive(
        self,
        netlist: NetlistArtifact,
        *,
        config: PhysicalConfig,
        time_limit_s: int | None = None,
    ) -> PhysicalStageOutcome:
        """Run the inner-loop physical flow against ``netlist``."""
        if netlist.design_id != self.design_id:
            raise PhysicalStageError(
                f"netlist.design_id {netlist.design_id!r} != driver.design_id "
                f"{self.design_id!r}"
            )

        runs: list[PhysicalRun] = []
        last_layout: LayoutArtifact | None = None
        last_layout_ref: ArtifactRef | None = None
        current = config

        for attempt in range(1, self.max_inner_attempts + 1):
            layout, run = self.service.run_flow(
                netlist, config=current, time_limit_s=time_limit_s,
            )
            layout_ref = self.store.put(layout)
            runs.append(run)
            last_layout = layout
            last_layout_ref = layout_ref

            if run.passed:
                return PhysicalStageOutcome(
                    passed=True,
                    escalate_to=None,
                    layout=last_layout,
                    layout_ref=last_layout_ref,
                    final_run=run,
                    attempts=attempt,
                    runs=runs,
                    final_config=current,
                )

            # No more attempts left, OR the failure isn't a congestion the
            # retune rule can address — escalate without spending the rest
            # of the budget on a known-bad knob.
            if attempt == self.max_inner_attempts or not run.congested:
                escalate_to = EscalationLevel.OUTER
                return PhysicalStageOutcome(
                    passed=False,
                    escalate_to=escalate_to,
                    layout=last_layout,
                    layout_ref=last_layout_ref,
                    final_run=run,
                    attempts=attempt,
                    runs=runs,
                    final_config=current,
                )

            next_config = current.retuned(
                utilization_delta=self.utilization_delta,
                floor_utilization=self.floor_utilization,
            )
            # If we're already pinned at the floor the retune is a no-op —
            # treat that as exhaustion and escalate without re-running.
            if next_config.target_utilization == current.target_utilization:
                return PhysicalStageOutcome(
                    passed=False,
                    escalate_to=EscalationLevel.OUTER,
                    layout=last_layout,
                    layout_ref=last_layout_ref,
                    final_run=run,
                    attempts=attempt,
                    runs=runs,
                    final_config=current,
                )
            current = next_config

        # Unreachable: the loop always exits via one of the returns above.
        raise PhysicalStageError("inner loop fell through without a result")
