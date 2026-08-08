"""GDSII emit stage driver (F6.5).

Wraps :class:`GDSIIEmitService` and returns a typed
:class:`GDSIIStageOutcome` so the control graph's gdsii_emit node has the
same outcome-style contract as the other stage drivers (synth / physical
/ signoff). There is no inner loop here — Magic either writes a GDS or
it doesn't — and the control graph guarantees this driver runs **after**
the F5.3 ``await_human`` interrupt resolves, so "GDS emitted only after
approval" is the gate ordering, not a check inside the driver.
"""

from __future__ import annotations

from dataclasses import dataclass

from chip_agent.design_state import (
    ArtifactRef,
    GDSIIArtifact,
    LayoutArtifact,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from chip_agent.tools.gdsii_emit import GDSIIEmitService

__all__ = [
    "GDSIIStageDriver",
    "GDSIIStageError",
    "GDSIIStageOutcome",
]


class GDSIIStageError(ValueError):
    """The driver was misconfigured (design_id mismatch, empty id, …)."""


@dataclass(frozen=True)
class GDSIIStageOutcome:
    """Result of one :meth:`GDSIIStageDriver.drive` call."""

    gds: GDSIIArtifact
    gds_ref: ArtifactRef


class GDSIIStageDriver:
    """Drive one GDS-write attempt over a routed :class:`LayoutArtifact`."""

    def __init__(
        self,
        *,
        service: GDSIIEmitService,
        store: SqliteArtifactStore,
        design_id: str,
    ) -> None:
        if not design_id:
            raise GDSIIStageError("design_id must be non-empty")
        self.service = service
        self.store = store
        self.design_id = design_id

    def drive(
        self,
        layout: LayoutArtifact,
        *,
        top_module: str | None = None,
        time_limit_s: int | None = None,
    ) -> GDSIIStageOutcome:
        """Emit a GDSII from ``layout``; persist + return the artifact."""
        if layout.design_id != self.design_id:
            raise GDSIIStageError(
                f"layout.design_id {layout.design_id!r} != driver.design_id "
                f"{self.design_id!r}"
            )
        gds = self.service.emit(
            layout, top_module=top_module, time_limit_s=time_limit_s,
        )
        gds_ref = self.store.put(gds)
        return GDSIIStageOutcome(gds=gds, gds_ref=gds_ref)
