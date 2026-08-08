"""Signoff stage driver (F6.3 + F6.4).

Runs the four signoff legs (STA, DRC, LVS, security) over a synth netlist
+ a routed layout and emits a typed :class:`SignoffStageOutcome`. The gate
is the **conjunction** of each report's ``gate_ok``: a single failing leg
closes the stage. This matches CLAUDE.md's invariant:

    SIGNOFF gate is a conjunction over STA + DRC + LVS + security
    `gate_ok`.

The driver does NOT implement an inner repair loop: signoff failures are
attribution-shaped — they typically point upstream (RTL for timing, the
physical config for DRC, the synth/place pipeline for LVS, RTL again for
security). F5.4's :func:`apply_cross_stage_feedback` is the route. This
module just lays the verification artifacts on the blackboard so the
gate can read them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chip_agent.design_state import (
    ArtifactRef,
    DRCReport,
    LayoutArtifact,
    LVSReport,
    MultiCornerSTAReport,
    NetlistArtifact,
    SecurityReport,
    TimingReport,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.opensta import parse_multi_corner_sta

__all__ = [
    "DRCRunner",
    "LVSRunner",
    "STARunner",
    "SecurityRunner",
    "SignoffStageDriver",
    "SignoffStageError",
    "SignoffStageOutcome",
]


class STARunner(Protocol):
    """Seam over :class:`OpenSTAService.check_timing`."""

    def check_timing(
        self,
        netlist: NetlistArtifact,
        *,
        clock_period_ns: float,
        sdc_text: str | None = ...,
        top_module: str | None = ...,
        time_limit_s: int | None = ...,
        sdf_bytes: bytes | None = ...,
        netlist_bytes_override: bytes | None = ...,
    ) -> TimingReport: ...


class DRCRunner(Protocol):
    """Seam over :class:`MagicDRCService.check_drc`."""

    def check_drc(
        self,
        layout: LayoutArtifact,
        *,
        top_module: str | None = ...,
        time_limit_s: int | None = ...,
        librelane_report_bytes: bytes | None = ...,
    ) -> DRCReport: ...


class LVSRunner(Protocol):
    """Seam over :class:`NetgenLVSService.check_lvs`."""

    def check_lvs(
        self,
        netlist: NetlistArtifact,
        layout: LayoutArtifact,
        *,
        layout_netlist_bytes: bytes,
        top_module: str | None = ...,
        time_limit_s: int | None = ...,
        netlist_bytes_override: bytes | None = ...,
    ) -> LVSReport: ...


class SecurityRunner(Protocol):
    """Seam over :class:`SecurityCheckService.check_security`."""

    def check_security(
        self,
        netlist: NetlistArtifact,
        *,
        layout: LayoutArtifact | None = ...,
        top_module: str | None = ...,
    ) -> SecurityReport: ...


class SignoffStageError(ValueError):
    """The driver was misconfigured or the inputs disagree (design_id, …)."""


@dataclass(frozen=True)
class SignoffStageOutcome:
    """Result of one :meth:`SignoffStageDriver.drive` call.

    ``passed`` is the conjunction over all leg ``gate_ok`` values; each
    individual report is persisted, and the refs are exposed for the
    control graph's ``record_failure`` / ``promote_to_head`` calls.

    F21.2: ``multi_corner_timing`` / ``multi_corner_timing_ref`` carry
    the per-corner verdict when SIGNOFF dispatched the multi-corner
    path (LayoutArtifact had per-corner harvest blobs). Exactly one of
    ``timing`` / ``multi_corner_timing`` is non-None per run; the gate
    conjunction reads whichever path fired. ``multi_corner_fallback``
    is ``True`` when the operator opted in but LibreLane tripped
    OpenROAD #6227 and the harvest reverted to single-corner — surfaces
    on the outcome so the handler can emit the audit event without
    re-reading the layout's metadata.
    """

    passed: bool
    timing: TimingReport | None
    drc: DRCReport
    lvs: LVSReport
    security: SecurityReport
    timing_ref: ArtifactRef | None
    drc_ref: ArtifactRef
    lvs_ref: ArtifactRef
    security_ref: ArtifactRef
    multi_corner_timing: MultiCornerSTAReport | None = None
    multi_corner_timing_ref: ArtifactRef | None = None
    multi_corner_fallback: bool = False

    def failing_codes(self) -> list[str]:
        """Return the verification codes that closed the gate (empty on pass)."""
        codes: list[str] = []
        # F21.2: read whichever timing artifact actually fired this run.
        timing_gate = (
            self.multi_corner_timing.gate_ok
            if self.multi_corner_timing is not None
            else self.timing.gate_ok if self.timing is not None
            else True
        )
        if not timing_gate:
            codes.append("STA")
        if not self.drc.gate_ok:
            codes.append("DRC")
        if not self.lvs.gate_ok:
            codes.append("LVS")
        if not self.security.gate_ok:
            codes.append("SECURITY")
        return codes


class SignoffStageDriver:
    """Run STA + DRC + LVS + security over a synth/physical pair and conjoin."""

    def __init__(
        self,
        *,
        sta: STARunner,
        drc: DRCRunner,
        lvs: LVSRunner,
        security: SecurityRunner,
        store: SqliteArtifactStore,
        design_id: str,
    ) -> None:
        if not design_id:
            raise SignoffStageError("design_id must be non-empty")
        self.sta = sta
        self.drc = drc
        self.lvs = lvs
        self.security = security
        self.store = store
        self.design_id = design_id

    def drive(
        self,
        netlist: NetlistArtifact,
        layout: LayoutArtifact,
        *,
        clock_period_ns: float,
        layout_netlist_bytes: bytes,
        sdc_text: str | None = None,
        top_module: str | None = None,
        time_limit_s: int | None = None,
    ) -> SignoffStageOutcome:
        if netlist.design_id != self.design_id:
            raise SignoffStageError(
                f"netlist.design_id {netlist.design_id!r} != driver.design_id "
                f"{self.design_id!r}"
            )
        if layout.design_id != self.design_id:
            raise SignoffStageError(
                f"layout.design_id {layout.design_id!r} != driver.design_id "
                f"{self.design_id!r}"
            )

        # F12.3: prefer the LibreLane-extracted SPICE on the layout when
        # available — that's the real layout netlist. Fall back to the
        # explicit ``layout_netlist_bytes`` arg only when the layout doesn't
        # carry one (rare). Both missing = SignoffStageError: LVS is no
        # longer allowed to silently pass on missing data.
        if layout.librelane_layout_spice is not None:
            lvs_bytes = self.store.get_blob(layout.librelane_layout_spice)
        elif layout_netlist_bytes:
            lvs_bytes = layout_netlist_bytes
        else:
            raise SignoffStageError(
                "LVS requires a layout netlist: layout.librelane_layout_spice "
                "is None and no explicit ``layout_netlist_bytes`` was supplied. "
                "Did LibreLane's extract step run? Check "
                "harvest paths and the librelane runs/ tree.",
            )

        # F12.1: when the LibreLane harvest captured a CTS SDF, feed it into
        # OpenSTA so the timing verdict reflects post-layout delays. Falls
        # back to the pre-layout estimate-only path when no SDF is present.
        sdf_bytes: bytes | None = None
        if layout.librelane_sdf is not None:
            sdf_bytes = self.store.get_blob(layout.librelane_sdf)
        # F13.1: prefer LibreLane's sky130-mapped netlist over Yosys's
        # intermediate one — OpenSTA's read_verilog parses the mapped
        # output cleanly. ``None`` -> the runner uses NetlistArtifact.netlist
        # (the F6.x intermediate, used by the stub flow).
        mapped_netlist_bytes: bytes | None = None
        if layout.librelane_mapped_netlist is not None:
            mapped_netlist_bytes = self.store.get_blob(
                layout.librelane_mapped_netlist,
            )
        # F13.4-B: LVS needs the *powered* netlist (port shape matches the
        # extracted SPICE), not the unpowered mapped one STA reads. Falls
        # back to the mapped netlist when the PNL isn't present so older
        # harvest passes keep producing a verdict.
        lvs_netlist_override: bytes | None = None
        if layout.librelane_powered_netlist is not None:
            lvs_netlist_override = self.store.get_blob(
                layout.librelane_powered_netlist,
            )
        elif mapped_netlist_bytes is not None:
            lvs_netlist_override = mapped_netlist_bytes
        # F21.2: dispatch on whether LibreLane harvested per-corner
        # reports. Exactly one branch runs per call: the multi-corner
        # path parses LibreLane's existing STA output bytes (no NoeSI
        # OpenSTA invocation) and produces a typed MultiCornerSTAReport;
        # the single-corner path is today's OpenSTAService.check_timing()
        # over the typical corner. When neither path produced a timing
        # verdict (defensive), the conjunction below treats it as a pass
        # — but the multi-corner path always emits a report when blobs
        # exist, so this case is unreachable in practice.
        timing: TimingReport | None = None
        multi_corner_timing: MultiCornerSTAReport | None = None
        multi_corner_timing_ref: ArtifactRef | None = None
        multi_corner_fallback = bool(
            layout.metadata.get("multi_corner_fallback") == "true"
        )
        if layout.librelane_per_corner_timing:
            corner_bytes = {
                corner: self.store.get_blob(blob)
                for corner, blob in layout.librelane_per_corner_timing.items()
            }
            power_bytes: dict[str, bytes] | None = None
            if layout.librelane_per_corner_power:
                power_bytes = {
                    corner: self.store.get_blob(blob)
                    for corner, blob in layout.librelane_per_corner_power.items()
                }
            multi_corner_timing = parse_multi_corner_sta(
                corner_bytes,
                power_bytes,
                design_id=self.design_id,
                artifact_id=f"{netlist.design_id}.{netlist.module_id or 'top'}.multi_sta",
                module_id=netlist.module_id,
            )
        else:
            timing = self.sta.check_timing(
                netlist,
                clock_period_ns=clock_period_ns,
                sdc_text=sdc_text,
                top_module=top_module,
                time_limit_s=time_limit_s,
                sdf_bytes=sdf_bytes,
                netlist_bytes_override=mapped_netlist_bytes,
            )
        # F12.2: when the LibreLane harvest captured a Magic DRC report,
        # feed it into the DRC leg so the report can cross-check our
        # re-run against LibreLane's own verdict.
        librelane_drc_bytes: bytes | None = None
        if layout.librelane_drc_report is not None:
            librelane_drc_bytes = self.store.get_blob(layout.librelane_drc_report)
        drc = self.drc.check_drc(
            layout, top_module=top_module, time_limit_s=time_limit_s,
            librelane_report_bytes=librelane_drc_bytes,
        )
        lvs = self.lvs.check_lvs(
            netlist, layout,
            layout_netlist_bytes=lvs_bytes,
            top_module=top_module,
            time_limit_s=time_limit_s,
            netlist_bytes_override=lvs_netlist_override,
        )
        security = self.security.check_security(
            netlist, layout=layout, top_module=top_module,
        )

        # F21.2: persist whichever timing artifact actually fired.
        timing_ref: ArtifactRef | None = None
        if multi_corner_timing is not None:
            multi_corner_timing_ref = self.store.put(multi_corner_timing)
        elif timing is not None:
            timing_ref = self.store.put(timing)
        drc_ref = self.store.put(drc)
        lvs_ref = self.store.put(lvs)
        security_ref = self.store.put(security)

        # F21.2: gate conjunction reads whichever timing path produced a
        # verdict. Defensive: no timing at all (unreachable in practice)
        # treats timing as a pass; DRC / LVS / security must still pass.
        timing_gate_ok = (
            multi_corner_timing.gate_ok if multi_corner_timing is not None
            else timing.gate_ok if timing is not None
            else True
        )
        passed = (
            timing_gate_ok and drc.gate_ok and lvs.gate_ok and security.gate_ok
        )
        return SignoffStageOutcome(
            passed=passed,
            timing=timing, drc=drc, lvs=lvs, security=security,
            timing_ref=timing_ref, drc_ref=drc_ref, lvs_ref=lvs_ref,
            security_ref=security_ref,
            multi_corner_timing=multi_corner_timing,
            multi_corner_timing_ref=multi_corner_timing_ref,
            multi_corner_fallback=multi_corner_fallback,
        )
