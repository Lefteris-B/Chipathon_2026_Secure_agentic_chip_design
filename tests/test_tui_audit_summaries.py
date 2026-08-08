"""F22.1-B — plain-English summaries for the 8 EventType members that
previously fell through to a JSON dump.

Pins the typed dispatch in :func:`chip_agent.tui.panes.audit._typed_summary`
so a future schema drift (extra/renamed payload key, different value type)
surfaces as the generic JSON fallback rather than silently producing a
misleading "looks right but isn't" summary.

One parametrised test per EventType for the happy path, plus a defensive
test that an empty / wrong-shape payload returns ``None`` (so the caller
falls through to the existing shape-based handlers or the JSON dump).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from chip_agent.obs.audit_log import AuditEvent, EventType
from chip_agent.tui.panes.audit import _payload_summary, _typed_summary

DESIGN_ID = "d0"


def _ev(event_type: EventType, payload: dict[str, Any]) -> AuditEvent:
    """Build an AuditEvent shaped like SqliteAuditLog.append would emit."""
    return AuditEvent(
        design_id=DESIGN_ID,
        sequence=1,
        timestamp=datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
        event_type=event_type,
        payload=payload,
        prev_hash="GENESIS",
        content_hash="sha256:00",
        signature="hmac-sha256:00",
    )


# --------------------------------------------------------------------------- #
# Happy-path: one test per EventType.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "event_type,payload,expected",
    [
        # STAGE_TRANSITION — "from"/"to" canonical shape.
        (
            EventType.STAGE_TRANSITION,
            {"from": "rtl", "to": "synth"},
            "advanced rtl → synth",
        ),
        # STAGE_TRANSITION — retry shape with stage + kind.
        (
            EventType.STAGE_TRANSITION,
            {"stage": "rtl", "kind": "retry"},
            "retry rtl",
        ),
        # ESCALATION — stage + from + to.
        (
            EventType.ESCALATION,
            {"stage": "rtl", "from": "inner", "to": "outer"},
            "escalated rtl inner → outer",
        ),
        # ESCALATION — from/to only.
        (
            EventType.ESCALATION,
            {"from": "outer", "to": "human"},
            "escalated outer → human",
        ),
        # FEEDBACK_FIRED — cross-stage feedback.
        (
            EventType.FEEDBACK_FIRED,
            {"from": "signoff", "to": "rtl"},
            "cross-stage feedback: signoff → rtl",
        ),
        # BACKEND_FALLBACK — task + to-model.
        (
            EventType.BACKEND_FALLBACK,
            {"task": "rtl_gen", "from": "frontier", "to": "local-coder"},
            "router fell back to local-coder for rtl_gen",
        ),
        # BACKEND_FALLBACK — only "fallback" key (older shape).
        (
            EventType.BACKEND_FALLBACK,
            {"task": "tb_gen", "fallback": "haiku"},
            "router fell back to haiku for tb_gen",
        ),
        # RTL_FRONTIER_FALLBACK — module-aware.
        (
            EventType.RTL_FRONTIER_FALLBACK,
            {"module_id": "counter"},
            "RTL outer-loop exhausted: one frontier attempt on counter",
        ),
        # M19_FAST_PATH_DECISION — used / skipped.
        (
            EventType.M19_FAST_PATH_DECISION,
            {"module_id": "buffer", "m19_fast_path_used": True,
             "port_count": 2, "has_clock_or_reset": False, "max_ports": 2},
            "M19 fast-path: buffer used",
        ),
        (
            EventType.M19_FAST_PATH_DECISION,
            {"module_id": "counter", "m19_fast_path_used": False,
             "port_count": 4, "has_clock_or_reset": True, "max_ports": 2},
            "M19 fast-path: counter skipped",
        ),
        # MULTI_CORNER_FALLBACK — F21.2 OpenROAD #6227 default reason.
        (
            EventType.MULTI_CORNER_FALLBACK,
            {"reason": "openroad_6227_segfault", "module_id": "counter"},
            "multi-corner STA fallback: OpenROAD #6227 (sky130A segfault)",
        ),
        # MULTI_CORNER_FALLBACK — arbitrary reason.
        (
            EventType.MULTI_CORNER_FALLBACK,
            {"reason": "missing_liberty_file"},
            "multi-corner STA fallback: missing_liberty_file",
        ),
        # PHYSICAL_REPAIR_ROUTED — F21.3 attempt-numbered route.
        (
            EventType.PHYSICAL_REPAIR_ROUTED,
            {"kind": "lower_density", "reason": "ss corner WNS negative",
             "attempt": 1},
            "physical repair (attempt 1): lower_density",
        ),
        (
            EventType.PHYSICAL_REPAIR_ROUTED,
            {"kind": "relax_clock_period", "reason": "...", "attempt": 2},
            "physical repair (attempt 2): relax_clock_period",
        ),
    ],
    ids=[
        "stage_transition-advance",
        "stage_transition-retry",
        "escalation-with-stage",
        "escalation-without-stage",
        "feedback_fired",
        "backend_fallback-from-to",
        "backend_fallback-fallback-key",
        "rtl_frontier_fallback",
        "m19_fast_path-used",
        "m19_fast_path-skipped",
        "multi_corner_fallback-6227",
        "multi_corner_fallback-other-reason",
        "physical_repair_routed-attempt-1",
        "physical_repair_routed-attempt-2",
    ],
)
def test_typed_summary_happy_path(
    event_type: EventType, payload: dict[str, Any], expected: str,
) -> None:
    summary = _typed_summary(_ev(event_type, payload))
    assert summary == expected


# --------------------------------------------------------------------------- #
# Integration via _payload_summary — proves the typed dispatch fires
# before the legacy shape-based handlers.
# --------------------------------------------------------------------------- #
def test_payload_summary_dispatches_typed_handler_for_m19_fast_path() -> None:
    """The F22.1 typed dispatch must fire BEFORE the legacy shape-based
    handlers — M19_FAST_PATH_DECISION payloads don't carry stage/ref/etc
    so the JSON fallback would otherwise be what the operator sees today."""
    ev = _ev(
        EventType.M19_FAST_PATH_DECISION,
        {"module_id": "counter", "m19_fast_path_used": True,
         "port_count": 2, "has_clock_or_reset": False, "max_ports": 2},
    )
    assert _payload_summary(ev) == "M19 fast-path: counter used"


def test_payload_summary_dispatches_typed_handler_for_physical_repair() -> None:
    """F21.3 PHYSICAL_REPAIR_ROUTED is the visible feedback for the M21
    semantic recovery loop — the operator needs to see WHICH knob the
    agent flipped on each attempt."""
    ev = _ev(
        EventType.PHYSICAL_REPAIR_ROUTED,
        {"kind": "lower_density", "reason": "ss WNS negative", "attempt": 1},
    )
    assert _payload_summary(ev) == "physical repair (attempt 1): lower_density"


# --------------------------------------------------------------------------- #
# Defensive: missing / wrong-shape payload falls through to JSON dump.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "event_type,payload",
    [
        # STAGE_TRANSITION with neither from/to nor stage.
        (EventType.STAGE_TRANSITION, {"reason": "?"}),
        # ESCALATION missing the required keys.
        (EventType.ESCALATION, {"stage": "rtl"}),
        # FEEDBACK_FIRED with only stage.
        (EventType.FEEDBACK_FIRED, {"stage": "rtl"}),
        # BACKEND_FALLBACK with no model anywhere.
        (EventType.BACKEND_FALLBACK, {"task": "rtl_gen"}),
        # M19_FAST_PATH_DECISION with wrong-type bool flag.
        (
            EventType.M19_FAST_PATH_DECISION,
            {"module_id": "x", "m19_fast_path_used": "yes"},  # str not bool
        ),
        # PHYSICAL_REPAIR_ROUTED missing 'kind'.
        (EventType.PHYSICAL_REPAIR_ROUTED, {"attempt": 1}),
    ],
)
def test_typed_summary_returns_none_on_unexpected_shape(
    event_type: EventType, payload: dict[str, Any],
) -> None:
    """Defensive: missing / wrong-type keys make ``_typed_summary``
    return ``None`` so the caller falls through to the JSON dump. The
    operator sees the raw payload rather than a misleading summary."""
    assert _typed_summary(_ev(event_type, payload)) is None


def test_typed_summary_returns_none_for_handled_event_types() -> None:
    """Defensive: EventTypes that already have a shape-based handler
    (ARTIFACT_PROMOTED, GATE_DECISION, HUMAN_DECISION, ROUTING_CHANGED)
    are NOT covered by ``_typed_summary``. Returning ``None`` for them
    lets the legacy handlers stay byte-identical."""
    assert _typed_summary(_ev(
        EventType.ARTIFACT_PROMOTED,
        {"stage": "rtl", "ref": {"artifact_id": "x", "version": 1}},
    )) is None
    assert _typed_summary(_ev(
        EventType.GATE_DECISION, {"stage": "signoff", "verdict": "advance"},
    )) is None
    assert _typed_summary(_ev(
        EventType.HUMAN_DECISION, {"decision": "approve"},
    )) is None


def test_rtl_frontier_fallback_summary_without_module() -> None:
    """Even when payload is empty, the RTL_FRONTIER_FALLBACK event_type
    is informative enough to summarise — the message itself IS the signal."""
    assert _typed_summary(_ev(
        EventType.RTL_FRONTIER_FALLBACK, {},
    )) == "RTL outer-loop exhausted: one frontier attempt"


def test_multi_corner_fallback_summary_without_reason() -> None:
    """No reason key → generic fallback message rather than None — the
    event itself is meaningful even without details."""
    assert _typed_summary(_ev(
        EventType.MULTI_CORNER_FALLBACK, {},
    )) == "multi-corner STA fallback"
