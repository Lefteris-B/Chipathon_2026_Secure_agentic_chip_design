"""Signoff metrics dashboard (F14.6).

Four-quadrant summary of the F13.x signoff reports for the active
design: STA, DRC, LVS, Security. Each quadrant shows the load-bearing
metrics for that leg + a single ``gate_ok`` verdict line.

The dashboard reads the F11.6-mirrored signoff JSON files (under
``exports/<design_id>/signoff/<top>.<leg>.json``) and Pydantic-validates
them back into typed verification artifacts. Missing files render an
"awaiting" quadrant — useful when the operator opens the dashboard
mid-flow.

Used by :class:`SignoffDashboardScreen`, the :class:`ModalScreen` the
``[Ctrl+S]`` keybind pushes from the main app. The widget itself is
mounted directly in unit tests via :meth:`App.run_test` so the
quadrant rendering can be exercised without modal lifecycle.

LVS.UNKNOWN — the F13.3 sentinel for "netgen produced no verdict" —
gets a banner across the LVS quadrant. F13.4-B will add a split
netlist harvest for STA vs LVS; until then the banner reminds the
operator to read the LVS report carefully.
"""

from __future__ import annotations

import json
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from chip_agent.design_state import (
    DRCReport,
    LVSReport,
    SecurityReport,
    TimingReport,
)

__all__ = [
    "SignoffDashboardPane",
    "SignoffDashboardScreen",
    "SignoffReports",
    "load_signoff_reports",
]


_LVS_UNKNOWN_CODE = "LVS.UNKNOWN"


# Tiny typed record carrying the four loaded reports — ``None`` for any
# leg whose JSON hasn't been written yet (mid-flow / failed earlier).
class SignoffReports:
    """Four parsed signoff artifacts + the source paths.

    Plain class rather than a dataclass so tests can construct an
    instance from typed objects + None placeholders without ceremony.
    """

    __slots__ = ("drc", "lvs", "security", "sta")

    def __init__(
        self,
        *,
        sta: TimingReport | None = None,
        drc: DRCReport | None = None,
        lvs: LVSReport | None = None,
        security: SecurityReport | None = None,
    ) -> None:
        self.sta = sta
        self.drc = drc
        self.lvs = lvs
        self.security = security


# --------------------------------------------------------------------------- #
# File-system loader.
# --------------------------------------------------------------------------- #
def load_signoff_reports(*, exports_dir: Path) -> SignoffReports:
    """Walk ``exports/<id>/signoff/`` for the four signoff JSONs.

    Auto-detects the top-module prefix by globbing for ``*.sta.json``
    etc. — the F14.5 exports pane writes ``<top>.<leg>.json`` per
    F11.6, but the dashboard shouldn't have to know the planner's
    top-module name to render.

    Missing files leave their leg as ``None``; the dashboard handles
    that by rendering an "awaiting" quadrant. Parse errors leave the
    leg as None too (we don't want a malformed JSON to crash the
    modal).
    """
    signoff_dir = exports_dir / "signoff"
    if not signoff_dir.exists():
        return SignoffReports()
    return SignoffReports(
        sta=_first_match(signoff_dir, ".sta.json", TimingReport),
        drc=_first_match(signoff_dir, ".drc.json", DRCReport),
        lvs=_first_match(signoff_dir, ".lvs.json", LVSReport),
        security=_first_match(signoff_dir, ".security.json", SecurityReport),
    )


def _first_match(signoff_dir: Path, suffix: str, model_cls):  # type: ignore[no-untyped-def]
    """Return the first parsed report whose filename ends with ``suffix``."""
    for f in sorted(signoff_dir.iterdir()):
        if not f.is_file() or not f.name.endswith(suffix):
            continue
        try:
            return model_cls.model_validate_json(
                f.read_text(encoding="utf-8"),
            )
        except (json.JSONDecodeError, ValueError, OSError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Pane widget — embeddable in tests + wrapped by the ModalScreen.
# --------------------------------------------------------------------------- #
class SignoffDashboardPane(Vertical):
    """4-quadrant signoff summary widget.

    Layout: a 2x2 :class:`Grid` of quadrants, each a :class:`Static`
    with the leg's header + metrics. ``apply_reports`` rebuilds all
    four bodies + their pass/fail CSS classes.
    """

    DEFAULT_CSS = """
    SignoffDashboardPane {
        height: 1fr;
        padding: 1 2;
    }

    SignoffDashboardPane > #signoff-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
    }

    SignoffDashboardPane > #signoff-grid {
        grid-size: 2 2;
        grid-gutter: 1;
        height: 1fr;
    }

    SignoffDashboardPane > #signoff-grid > Static {
        border: solid $panel;
        padding: 1 2;
        height: 1fr;
        background: $surface;
    }

    SignoffDashboardPane > #signoff-grid > Static.leg-passed {
        border: solid $success;
    }

    SignoffDashboardPane > #signoff-grid > Static.leg-failed {
        border: solid $error;
    }

    SignoffDashboardPane > #signoff-grid > Static.leg-awaiting {
        border: solid $warning;
        color: $text-muted;
    }

    SignoffDashboardPane > #signoff-footer {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    """

    LEG_IDS: tuple[str, ...] = ("sta", "drc", "lvs", "security")

    def compose(self) -> ComposeResult:
        yield Static("Signoff dashboard", id="signoff-title")
        with Grid(id="signoff-grid"):
            for leg in self.LEG_IDS:
                cell = Static("(awaiting)", id=f"leg-{leg}")
                cell.add_class("leg-awaiting")
                yield cell
        yield Static(
            "Press Esc to close.", id="signoff-footer",
        )

    # ------------------------------------------------------------- public API
    def apply_reports(self, reports: SignoffReports) -> None:
        """Rebuild each quadrant's text + pass/fail border class."""
        self._render_leg("sta", _format_sta(reports.sta), reports.sta)
        self._render_leg("drc", _format_drc(reports.drc), reports.drc)
        self._render_leg("lvs", _format_lvs(reports.lvs), reports.lvs)
        self._render_leg(
            "security", _format_security(reports.security), reports.security,
        )

    # ---------------------------------------------------------------- helpers
    def _render_leg(
        self, leg: str, text: str, report: object | None,
    ) -> None:
        cell = self.query_one(f"#leg-{leg}", Static)
        cell.update(text)
        for c in ("leg-passed", "leg-failed", "leg-awaiting"):
            cell.remove_class(c)
        cell.add_class(_leg_class(report))


# --------------------------------------------------------------------------- #
# Modal wrapper — what ``[Ctrl+S]`` pushes from the main app.
# --------------------------------------------------------------------------- #
class SignoffDashboardScreen(ModalScreen[None]):
    """Modal screen wrapping :class:`SignoffDashboardPane`.

    Reads the four signoff JSONs from ``exports_dir`` at mount time
    and feeds them through ``apply_reports``. ``Esc`` closes the
    modal — the app's main BINDINGS table keeps Ctrl+Q for quit, so
    Esc is unambiguous here.
    """

    BINDINGS = [  # noqa: RUF012  (mypy expects list per Screen base class)
        Binding("escape", "close", "Close", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    SignoffDashboardScreen {
        align: center middle;
        background: $background 70%;
    }

    SignoffDashboardScreen > SignoffDashboardPane {
        width: 80%;
        height: 80%;
        background: $panel;
        border: thick $primary;
    }
    """

    def __init__(self, *, exports_dir: Path) -> None:
        super().__init__()
        self._exports_dir = exports_dir

    def compose(self) -> ComposeResult:
        yield SignoffDashboardPane()

    def on_mount(self) -> None:
        pane = self.query_one(SignoffDashboardPane)
        reports = load_signoff_reports(exports_dir=self._exports_dir)
        pane.apply_reports(reports)

    def action_close(self) -> None:
        self.dismiss()


# --------------------------------------------------------------------------- #
# Pure formatters — easy to unit-test.
# --------------------------------------------------------------------------- #
def _format_sta(report: TimingReport | None) -> str:
    if report is None:
        return "STA\n\n(awaiting timing report)"
    verdict = _verdict(report.gate_ok)
    wns = _format_float(report.wns_ns, "ns")
    tns = _format_float(report.tns_ns, "ns")
    return (
        f"STA — {verdict}\n\n"
        f"WNS:    {wns}\n"
        f"TNS:    {tns}\n"
        f"setup violations: {report.setup_violations}\n"
        f"hold  violations: {report.hold_violations}"
    )


def _format_drc(report: DRCReport | None) -> str:
    if report is None:
        return "DRC\n\n(awaiting DRC report)"
    verdict = _verdict(report.gate_ok)
    return (
        f"DRC — {verdict}\n\n"
        f"violation_count: {report.violation_count}"
    )


def _format_lvs(report: LVSReport | None) -> str:
    if report is None:
        return "LVS\n\n(awaiting LVS report)"
    verdict = _verdict(report.gate_ok)
    body = (
        f"LVS — {verdict}\n\n"
        f"matched:        {report.matched}\n"
        f"mismatch_count: {report.mismatch_count}"
    )
    if _has_lvs_unknown(report):
        # F13.4-B reminder banner — Netgen gave no verdict, so the
        # operator should read the LVS JSON before trusting the result.
        body += "\n\n[LVS.UNKNOWN — Netgen produced no verdict line]"
    return body


def _format_security(report: SecurityReport | None) -> str:
    if report is None:
        return "Security\n\n(awaiting security report)"
    verdict = _verdict(report.gate_ok)
    checks = len(report.checks_run)
    return (
        f"Security — {verdict}\n\n"
        f"checks_run:            {checks}\n"
        f"suspicious_structures: {report.suspicious_structures}"
    )


def _verdict(gate_ok: bool) -> str:
    return "PASS" if gate_ok else "FAIL"


def _format_float(v: float | None, unit: str) -> str:
    return "n/a" if v is None else f"{v:+.3f} {unit}"


def _has_lvs_unknown(report: LVSReport) -> bool:
    return any(v.code == _LVS_UNKNOWN_CODE for v in report.violations)


def _leg_class(report: object | None) -> str:
    if report is None:
        return "leg-awaiting"
    gate_ok = getattr(report, "gate_ok", None)
    if gate_ok is True:
        return "leg-passed"
    return "leg-failed"
