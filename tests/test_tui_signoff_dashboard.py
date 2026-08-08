"""F14.6 acceptance: signoff metrics dashboard + help screen.

Pins:

* ``load_signoff_reports`` auto-detects the top-module prefix and
  Pydantic-validates each leg's JSON back into the typed report.
* All four legs render in a 2x2 :class:`Grid` with the load-bearing
  metrics for each (WNS / TNS for STA, violation count for DRC,
  matched + mismatch_count for LVS, checks_run + suspicious_structures
  for Security).
* Each quadrant carries one of ``leg-passed`` / ``leg-failed`` /
  ``leg-awaiting`` so the border color tracks ``gate_ok``.
* The LVS quadrant shows the ``[LVS.UNKNOWN — Netgen produced no
  verdict line]`` banner when the report's violations carry that
  code (F13.4-B reminder).
* The :class:`HelpScreen` modal renders the keybinding reference.

Tests mount :class:`SignoffDashboardPane` directly via
:meth:`App.run_test` — no real LangGraph spine, no real signoff
flow. The signoff JSONs are written into ``tmp_path`` matching the
F11.6 export shape so ``load_signoff_reports`` exercises the actual
filesystem walk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Static

from chip_agent.design_state import (
    ArtifactKind,
    ArtifactStatus,
    DRCReport,
    LVSReport,
    Provenance,
    SecurityReport,
    Stage,
    TimingReport,
    Violation,
)
from chip_agent.tui.panes.help import HelpScreen
from chip_agent.tui.panes.signoff import (
    SignoffDashboardPane,
    SignoffReports,
    _format_drc,
    _format_lvs,
    _format_security,
    _format_sta,
    load_signoff_reports,
)


def _arun(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Report builders — minimal valid VerificationArtifact instances.
# --------------------------------------------------------------------------- #
def _sta(*, passed: bool = True) -> TimingReport:
    return TimingReport(
        artifact_id="d.counter.sta", design_id="d",
        status=ArtifactStatus.ACCEPTED,
        kind=ArtifactKind.STA,
        passed=passed,
        wns_ns=0.123 if passed else -0.456,
        tns_ns=0.0 if passed else -1.5,
        setup_violations=0 if passed else 3,
        hold_violations=0,
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _drc(*, passed: bool = True, count: int = 0) -> DRCReport:
    return DRCReport(
        artifact_id="d.counter.drc", design_id="d",
        status=ArtifactStatus.ACCEPTED,
        kind=ArtifactKind.DRC,
        passed=passed, violation_count=count,
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _lvs(*, matched: bool = True, mismatch_count: int = 0,
         unknown: bool = False) -> LVSReport:
    violations = []
    if unknown:
        violations.append(Violation(
            code="LVS.UNKNOWN", severity="error",
            message="Netgen produced no match verdict",
        ))
    return LVSReport(
        artifact_id="d.counter.lvs", design_id="d",
        status=ArtifactStatus.ACCEPTED,
        kind=ArtifactKind.LVS,
        passed=matched and not unknown,
        matched=matched, mismatch_count=mismatch_count,
        violations=violations,
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


def _sec(*, passed: bool = True, suspicious: int = 0) -> SecurityReport:
    return SecurityReport(
        artifact_id="d.counter.security", design_id="d",
        status=ArtifactStatus.ACCEPTED,
        kind=ArtifactKind.SECURITY,
        passed=passed,
        checks_run=["reset_strap", "scan_chain"],
        suspicious_structures=suspicious,
        provenance=Provenance(produced_by=Stage.SIGNOFF),
    )


# --------------------------------------------------------------------------- #
# Pure-function tests: per-leg formatters.
# --------------------------------------------------------------------------- #
def test_format_sta_renders_wns_tns_and_violation_counts() -> None:
    body = _format_sta(_sta(passed=False))
    assert "STA" in body
    assert "FAIL" in body
    assert "-0.456 ns" in body  # WNS sign-prefixed
    assert "-1.500 ns" in body  # TNS
    assert "setup violations: 3" in body


def test_format_sta_passed_uses_pass_verdict() -> None:
    body = _format_sta(_sta(passed=True))
    assert "PASS" in body


def test_format_sta_awaiting_when_none() -> None:
    body = _format_sta(None)
    assert "awaiting" in body


def test_format_drc_shows_violation_count() -> None:
    body = _format_drc(_drc(passed=False, count=42))
    assert "DRC" in body
    assert "FAIL" in body
    assert "violation_count: 42" in body


def test_format_lvs_clean_run_passes() -> None:
    body = _format_lvs(_lvs(matched=True, mismatch_count=0))
    assert "PASS" in body
    assert "matched:" in body
    assert "True" in body


def test_format_lvs_shows_lvs_unknown_banner_when_violation_present() -> None:
    """F13.x sentinel: LVS.UNKNOWN violation → banner."""
    body = _format_lvs(_lvs(matched=False, unknown=True))
    assert "FAIL" in body
    assert "[LVS.UNKNOWN" in body


def test_format_lvs_no_banner_for_clean_lvs() -> None:
    body = _format_lvs(_lvs(matched=True))
    assert "LVS.UNKNOWN" not in body


def test_format_security_counts_checks_and_suspicious_structures() -> None:
    body = _format_security(_sec(passed=False, suspicious=2))
    assert "Security" in body
    assert "FAIL" in body
    assert "checks_run:" in body
    assert "2" in body  # checks_run length is 2
    assert "suspicious_structures: 2" in body


# --------------------------------------------------------------------------- #
# load_signoff_reports: filesystem walk + JSON round-trip.
# --------------------------------------------------------------------------- #
def _write_reports(
    exports_dir: Path, *, top: str = "counter",
    sta: TimingReport | None = None,
    drc: DRCReport | None = None,
    lvs: LVSReport | None = None,
    security: SecurityReport | None = None,
) -> None:
    signoff_dir = exports_dir / "signoff"
    signoff_dir.mkdir(parents=True, exist_ok=True)
    if sta is not None:
        (signoff_dir / f"{top}.sta.json").write_text(sta.model_dump_json(indent=2))
    if drc is not None:
        (signoff_dir / f"{top}.drc.json").write_text(drc.model_dump_json(indent=2))
    if lvs is not None:
        (signoff_dir / f"{top}.lvs.json").write_text(lvs.model_dump_json(indent=2))
    if security is not None:
        (signoff_dir / f"{top}.security.json").write_text(
            security.model_dump_json(indent=2),
        )


def test_load_signoff_reports_round_trips_all_four_legs(tmp_path: Path) -> None:
    exports_dir = tmp_path / "exports" / "d"
    _write_reports(
        exports_dir, sta=_sta(passed=True), drc=_drc(passed=True),
        lvs=_lvs(matched=True), security=_sec(passed=True),
    )
    reports = load_signoff_reports(exports_dir=exports_dir)
    assert reports.sta is not None
    assert reports.drc is not None
    assert reports.lvs is not None
    assert reports.security is not None
    assert reports.sta.wns_ns == 0.123
    assert reports.drc.violation_count == 0
    assert reports.lvs.matched is True


def test_load_signoff_reports_returns_empty_when_signoff_dir_missing(
    tmp_path: Path,
) -> None:
    """No signoff/ subdir yet → all four legs are None."""
    exports_dir = tmp_path / "exports" / "fresh"
    exports_dir.mkdir(parents=True, exist_ok=True)
    reports = load_signoff_reports(exports_dir=exports_dir)
    assert reports.sta is None
    assert reports.drc is None
    assert reports.lvs is None
    assert reports.security is None


def test_load_signoff_reports_handles_partial_state(tmp_path: Path) -> None:
    """Only STA written → other three legs stay None, no crash."""
    exports_dir = tmp_path / "exports" / "d"
    _write_reports(exports_dir, sta=_sta(passed=True))
    reports = load_signoff_reports(exports_dir=exports_dir)
    assert reports.sta is not None
    assert reports.drc is None
    assert reports.lvs is None
    assert reports.security is None


def test_load_signoff_reports_tolerates_malformed_json(tmp_path: Path) -> None:
    """Corrupt sta.json → that leg is None; other legs unaffected."""
    exports_dir = tmp_path / "exports" / "d"
    (exports_dir / "signoff").mkdir(parents=True)
    (exports_dir / "signoff" / "counter.sta.json").write_text("not json at all")
    _write_reports(exports_dir, drc=_drc(passed=True))
    reports = load_signoff_reports(exports_dir=exports_dir)
    assert reports.sta is None
    assert reports.drc is not None


# --------------------------------------------------------------------------- #
# Mounted-pane tests.
# --------------------------------------------------------------------------- #
def _drive(
    drive: Callable[[SignoffDashboardPane, object], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield SignoffDashboardPane()

    async def _go() -> dict[str, Any]:
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            pane = host.query_one(SignoffDashboardPane)
            return await drive(pane, pilot)

    return _arun(_go())


def _leg_classes(pane: SignoffDashboardPane, leg: str) -> set[str]:
    cell = pane.query_one(f"#leg-{leg}", Static)
    return {c for c in cell.classes if c.startswith("leg-")}  # type: ignore[attr-defined]


def _leg_text(pane: SignoffDashboardPane, leg: str) -> str:
    return str(pane.query_one(f"#leg-{leg}", Static).renderable)


def test_dashboard_renders_four_legs_from_signoff_reports() -> None:
    reports = SignoffReports(
        sta=_sta(passed=True), drc=_drc(passed=True),
        lvs=_lvs(matched=True), security=_sec(passed=True),
    )

    async def drive(pane: SignoffDashboardPane, pilot: object) -> dict[str, Any]:
        pane.apply_reports(reports)
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "sta_text": _leg_text(pane, "sta"),
            "drc_text": _leg_text(pane, "drc"),
            "lvs_text": _leg_text(pane, "lvs"),
            "security_text": _leg_text(pane, "security"),
            "sta_cls": _leg_classes(pane, "sta"),
            "drc_cls": _leg_classes(pane, "drc"),
            "lvs_cls": _leg_classes(pane, "lvs"),
            "sec_cls": _leg_classes(pane, "security"),
        }

    c = _drive(drive)
    assert "STA" in c["sta_text"] and "PASS" in c["sta_text"]
    assert "DRC" in c["drc_text"] and "PASS" in c["drc_text"]
    assert "LVS" in c["lvs_text"] and "PASS" in c["lvs_text"]
    assert "Security" in c["security_text"] and "PASS" in c["security_text"]
    for cls in (c["sta_cls"], c["drc_cls"], c["lvs_cls"], c["sec_cls"]):
        assert "leg-passed" in cls


def test_pass_indicator_red_when_lvs_unknown() -> None:
    """LVS.UNKNOWN → quadrant shows FAIL + banner + leg-failed CSS."""
    reports = SignoffReports(
        sta=_sta(passed=True), drc=_drc(passed=True),
        lvs=_lvs(matched=False, unknown=True),
        security=_sec(passed=True),
    )

    async def drive(pane: SignoffDashboardPane, pilot: object) -> dict[str, Any]:
        pane.apply_reports(reports)
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "lvs_text": _leg_text(pane, "lvs"),
            "lvs_cls": _leg_classes(pane, "lvs"),
        }

    c = _drive(drive)
    assert "FAIL" in c["lvs_text"]
    assert "[LVS.UNKNOWN" in c["lvs_text"]
    assert "leg-failed" in c["lvs_cls"]


def test_dashboard_renders_awaiting_legs_when_reports_missing() -> None:
    """No reports loaded → all four quadrants render as awaiting (warning)."""
    reports = SignoffReports()  # all legs = None

    async def drive(pane: SignoffDashboardPane, pilot: object) -> dict[str, Any]:
        pane.apply_reports(reports)
        await pilot.pause()  # type: ignore[attr-defined]
        return {
            "sta_cls": _leg_classes(pane, "sta"),
            "drc_cls": _leg_classes(pane, "drc"),
            "lvs_cls": _leg_classes(pane, "lvs"),
            "sec_cls": _leg_classes(pane, "security"),
        }

    c = _drive(drive)
    for cls in (c["sta_cls"], c["drc_cls"], c["lvs_cls"], c["sec_cls"]):
        assert "leg-awaiting" in cls


# --------------------------------------------------------------------------- #
# Help screen.
# --------------------------------------------------------------------------- #
def test_help_screen_lists_all_app_keybinds() -> None:
    """Help body covers every Ctrl+ keybind the main app advertises."""

    class _Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(HelpScreen())

    async def _go() -> dict[str, Any]:
        host = _Host()
        async with host.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            body = host.query_one("#help-body", Static)
            return {"text": str(body.renderable)}

    captured = _arun(_go())
    text = captured["text"]
    for binding in ("Ctrl+R", "Ctrl+A", "Ctrl+S", "Ctrl+H", "Ctrl+Q"):
        assert binding in text
    assert "/run" in text
    assert "Esc" in text
