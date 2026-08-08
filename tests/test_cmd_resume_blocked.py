"""F23.5: cmd_resume blocked / re-paused outcome hardening.

An interactive-repair resume can end without a GDS — the hinted retry
re-pauses (needs more guidance) or ends blocked. These pin the pieces
that must NOT crash on a missing GDSII head:

* ``_gds_head_ref_or_none`` returns None instead of raising.
* ``_print_resume`` renders re-paused / blocked outcomes.
* ``ResumeOutcome`` accepts a None gds_ref/manifest.
"""

from __future__ import annotations

from pathlib import Path

from chip_agent.cli import (
    ResumeOutcome,
    _gds_head_ref_or_none,
    _print_resume,
)
from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    DesignState,
    DesignStatus,
    PendingHumanRepair,
    Stage,
    StageState,
)


def _gds_ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="d0.gds", version=1, kind=ArtifactKind.GDSII,
        content_hash=f"sha256:{'a' * 64}",
    )


def test_gds_head_or_none_missing_returns_none() -> None:
    state = DesignState(design_id="d0", name="present80")
    assert _gds_head_ref_or_none(state) is None


def test_gds_head_or_none_present_returns_ref() -> None:
    state = DesignState(design_id="d0", name="present80")
    state.stages[Stage.GDSII] = StageState(stage=Stage.GDSII, head=_gds_ref())
    assert _gds_head_ref_or_none(state) == _gds_ref()


def _lines(**kwargs: object) -> list[str]:
    out: list[str] = []
    _print_resume(out=out.append, **kwargs)  # type: ignore[arg-type]
    return out


def test_print_resume_repaused_outcome() -> None:
    state = DesignState(
        design_id="d0", name="present80",
        status=DesignStatus.AWAITING_HUMAN, current_stage=Stage.RTL,
        pending_human_repair=PendingHumanRepair(module_id="m", stage=Stage.RTL),
    )
    text = "\n".join(_lines(
        design_id="d0", final=state, gds_ref=None, manifest_path=None,
    ))
    assert "re-paused" in text
    assert "--hint" in text
    assert "'m'" in text


def test_print_resume_blocked_outcome() -> None:
    state = DesignState(
        design_id="d0", name="present80",
        status=DesignStatus.AWAITING_HUMAN, current_stage=Stage.RTL,
    )
    text = "\n".join(_lines(
        design_id="d0", final=state, gds_ref=None, manifest_path=None,
    ))
    assert "blocked" in text


def test_print_resume_completed_outcome() -> None:
    state = DesignState(
        design_id="d0", name="present80",
        status=DesignStatus.COMPLETED, current_stage=Stage.GDSII,
    )
    text = "\n".join(_lines(
        design_id="d0", final=state, gds_ref=_gds_ref(),
        manifest_path=Path("/tmp/manifest.json"),
    ))
    assert "d0.gds@v1" in text
    assert "manifest" in text


def test_resume_outcome_allows_no_gds() -> None:
    state = DesignState(design_id="d0", name="present80")
    outcome = ResumeOutcome(
        design_id="d0", final_state=state, exports_dir=Path("/tmp/exports"),
    )
    assert outcome.gds_ref is None
    assert outcome.manifest is None
    assert outcome.manifest_path is None
