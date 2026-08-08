"""F7.1 acceptance: a complete trace tree keyed by ``design_id``.

The trace surface captures the F7.1-required signals — tokens, latency,
cost, escalation level — and forms a tree of spans under one root per
run. Tests exercise:

* The Protocol contract (``InMemoryTracer``, ``NoopTracer``).
* Span tree shape: parent / child / sibling ordering.
* ``design_id`` keying: a tracer hosting two concurrent runs slices
  cleanly by ``design_id``.
* Field plumbing: tokens / cost / escalation / latency land on the
  recorded :class:`SpanRecord` and survive a ``build_trace_tree`` walk.
* Error capture: a span that raises records ``status=ERROR`` and the
  exception text.
"""

from __future__ import annotations

import pytest

from chip_agent.design_state import EscalationLevel
from chip_agent.obs.tracing import (
    InMemoryTracer,
    NoopTracer,
    SpanKind,
    SpanStatus,
    Tracer,
    build_trace_tree,
)


# --------------------------------------------------------------------------- #
# Smoke: basic run + child span
# --------------------------------------------------------------------------- #
def test_run_records_root_span() -> None:
    tracer = InMemoryTracer()
    with tracer.run("d0") as run:
        assert run.kind is SpanKind.RUN
        assert run.design_id == "d0"

    # Root span is the only recorded span.
    assert len(tracer.spans) == 1
    rec = tracer.spans[0]
    assert rec.kind is SpanKind.RUN
    assert rec.parent_id is None
    assert rec.status is SpanStatus.OK
    assert rec.duration_ms is not None and rec.duration_ms >= 0.0


def test_child_span_links_to_run() -> None:
    tracer = InMemoryTracer()
    with tracer.run("d0") as run, tracer.span("rtl_gen", kind=SpanKind.MODEL) as model:
        assert model.parent_id == run.id
        assert model.design_id == "d0"

    by_kind = {s.kind: s for s in tracer.spans}
    assert by_kind[SpanKind.MODEL].parent_id == by_kind[SpanKind.RUN].span_id


def test_span_outside_run_raises() -> None:
    tracer = InMemoryTracer()
    with pytest.raises(RuntimeError), tracer.span("orphan", kind=SpanKind.MODEL):
        pass


def test_run_rejects_empty_design_id() -> None:
    tracer = InMemoryTracer()
    with pytest.raises(ValueError, match="design_id"), tracer.run(""):
        pass


# --------------------------------------------------------------------------- #
# Tokens / cost / escalation / latency
# --------------------------------------------------------------------------- #
def test_model_span_records_tokens_cost_escalation() -> None:
    # F7.1 AC: every model call traces tokens, latency, cost, escalation.
    tracer = InMemoryTracer()
    with tracer.run("d0"), tracer.span("rtl_gen", kind=SpanKind.MODEL) as span:
        span.set_tokens(prompt=120, completion=480)
        span.set_cost(0.0042)
        span.set_escalation(EscalationLevel.OUTER)
        span.set_attribute("model", "claude-opus-4-7")

    model = next(s for s in tracer.spans if s.kind is SpanKind.MODEL)
    assert model.prompt_tokens == 120
    assert model.completion_tokens == 480
    assert model.cost_usd == 0.0042
    assert model.escalation is EscalationLevel.OUTER
    assert model.attributes["model"] == "claude-opus-4-7"
    assert model.duration_ms is not None and model.duration_ms >= 0.0


def test_tool_span_records_attributes() -> None:
    # F7.1 AC: tool calls also traced. Tools don't have tokens/cost, but the
    # latency + escalation + attributes still land.
    tracer = InMemoryTracer()
    with tracer.run("d0"), tracer.span("verilator", kind=SpanKind.TOOL) as span:
        span.set_escalation(EscalationLevel.INNER)
        span.set_attribute("returncode", 0)
        span.set_attribute("violations", 3)

    tool = next(s for s in tracer.spans if s.kind is SpanKind.TOOL)
    assert tool.escalation is EscalationLevel.INNER
    assert tool.attributes["returncode"] == 0
    assert tool.attributes["violations"] == 3
    # No tokens — model-only.
    assert tool.prompt_tokens is None
    assert tool.completion_tokens is None


# --------------------------------------------------------------------------- #
# Nested span tree
# --------------------------------------------------------------------------- #
def test_nested_spans_record_full_tree() -> None:
    # Intentional nesting — the indentation mirrors the trace tree we assert
    # on. Collapsing into a single `with` tuple destroys that readability.
    tracer = InMemoryTracer()
    with tracer.run("d0") as run, tracer.span("rtl_stage", kind=SpanKind.STAGE) as stage:  # noqa: SIM117
        with tracer.span("inner_attempt", kind=SpanKind.LOOP) as loop:
            with tracer.span("verible", kind=SpanKind.TOOL) as t1:
                pass
            with tracer.span("verilator", kind=SpanKind.TOOL) as t2:
                pass

    # Walk the tree shape.
    by_id = {s.span_id: s for s in tracer.spans}
    assert by_id[stage.id].parent_id == run.id
    assert by_id[loop.id].parent_id == stage.id
    assert by_id[t1.id].parent_id == loop.id
    assert by_id[t2.id].parent_id == loop.id


# --------------------------------------------------------------------------- #
# AC: build_trace_tree slices by design_id
# --------------------------------------------------------------------------- #
def test_build_trace_tree_keys_by_design_id() -> None:
    tracer = InMemoryTracer()
    # Run 1: design "alpha"
    with tracer.run("alpha"), tracer.span("rtl_gen", kind=SpanKind.MODEL):
        pass
    # Run 2: design "beta"
    with tracer.run("beta"):
        with tracer.span("verible", kind=SpanKind.TOOL):
            pass
        with tracer.span("verilator", kind=SpanKind.TOOL):
            pass

    alpha = build_trace_tree(tracer.spans, design_id="alpha")
    beta = build_trace_tree(tracer.spans, design_id="beta")

    assert len(alpha) == 1
    assert alpha[0].record.kind is SpanKind.RUN
    assert len(alpha[0].children) == 1
    assert alpha[0].children[0].record.kind is SpanKind.MODEL

    assert len(beta) == 1
    assert beta[0].record.kind is SpanKind.RUN
    assert len(beta[0].children) == 2
    assert {c.record.name for c in beta[0].children} == {"verible", "verilator"}


def test_trace_tree_walk_pre_orders_records() -> None:
    tracer = InMemoryTracer()
    with tracer.run("d0"), tracer.span("rtl_stage", kind=SpanKind.STAGE):  # noqa: SIM117
        with tracer.span("rtl_gen", kind=SpanKind.MODEL):
            pass

    [root] = build_trace_tree(tracer.spans, design_id="d0")
    walked = list(root.walk())
    kinds = [r.kind for r in walked]
    assert kinds == [SpanKind.RUN, SpanKind.STAGE, SpanKind.MODEL]


def test_build_trace_tree_filters_other_designs() -> None:
    tracer = InMemoryTracer()
    with tracer.run("alpha"), tracer.span("m", kind=SpanKind.MODEL):
        pass
    with tracer.run("beta"), tracer.span("t", kind=SpanKind.TOOL):
        pass

    # Only alpha's spans should land in the alpha tree.
    [alpha_root] = build_trace_tree(tracer.spans, design_id="alpha")
    walked = {r.kind for r in alpha_root.walk()}
    assert walked == {SpanKind.RUN, SpanKind.MODEL}


def test_build_trace_tree_unknown_design_returns_empty() -> None:
    tracer = InMemoryTracer()
    with tracer.run("d0"):
        pass
    assert build_trace_tree(tracer.spans, design_id="other") == []


def test_build_trace_tree_rejects_empty_design_id() -> None:
    tracer = InMemoryTracer()
    with tracer.run("d0"):
        pass
    with pytest.raises(ValueError, match="design_id"):
        build_trace_tree(tracer.spans, design_id="")


def test_child_sibling_order_is_deterministic() -> None:
    tracer = InMemoryTracer()
    with tracer.run("d0"):
        with tracer.span("first", kind=SpanKind.TOOL):
            pass
        with tracer.span("second", kind=SpanKind.TOOL):
            pass
        with tracer.span("third", kind=SpanKind.TOOL):
            pass
    [root] = build_trace_tree(tracer.spans, design_id="d0")
    assert [c.record.name for c in root.children] == ["first", "second", "third"]


# --------------------------------------------------------------------------- #
# Error capture
# --------------------------------------------------------------------------- #
def test_span_records_error_when_block_raises() -> None:
    tracer = InMemoryTracer()
    with pytest.raises(RuntimeError, match="boom"), tracer.run("d0"):  # noqa: SIM117
        with tracer.span("rtl_gen", kind=SpanKind.MODEL):
            raise RuntimeError("boom")

    # The inner span captured the error; the run captured it too (the
    # exception propagated through both ``with`` blocks).
    by_name = {s.name: s for s in tracer.spans}
    assert by_name["rtl_gen"].status is SpanStatus.ERROR
    assert by_name["rtl_gen"].error is not None
    assert "boom" in by_name["rtl_gen"].error
    assert by_name["run"].status is SpanStatus.ERROR


# --------------------------------------------------------------------------- #
# NoopTracer — same Protocol, no records
# --------------------------------------------------------------------------- #
def test_noop_tracer_skips_recording() -> None:
    tracer: Tracer = NoopTracer()
    with tracer.run("d0") as run:
        with tracer.span("m", kind=SpanKind.MODEL) as m:
            m.set_tokens(prompt=1, completion=2)
        assert run.design_id == "d0"
    # Spans list stays empty on the noop tracer.
    assert tracer.spans == []  # type: ignore[attr-defined]


def test_noop_tracer_rejects_empty_design_id() -> None:
    tracer = NoopTracer()
    with pytest.raises(ValueError), tracer.run(""):
        pass


def test_noop_tracer_span_outside_run_does_not_raise() -> None:
    # NoopTracer is for the "tracing disabled" path — orphan spans become a
    # no-op rather than a hard failure so callers don't have to branch on
    # whether tracing is enabled.
    tracer = NoopTracer()
    with tracer.span("orphan", kind=SpanKind.MODEL) as span:
        assert span.kind is SpanKind.MODEL


# --------------------------------------------------------------------------- #
# AC: a simulated run produces a complete trace tree
# --------------------------------------------------------------------------- #
def test_simulated_run_produces_complete_tree() -> None:
    """End-to-end shape test — a synthetic run with one RTL stage, an
    inner repair loop, two tool calls, a model call, and a gate decision.
    The recorded forest under ``design_id`` covers every kind of span we
    expect production code to emit."""
    tracer = InMemoryTracer()
    with tracer.run("synthetic"):
        with tracer.span("rtl_stage", kind=SpanKind.STAGE):
            with tracer.span("inner_attempt_1", kind=SpanKind.LOOP) as loop:
                loop.set_escalation(EscalationLevel.INNER)
                with tracer.span("verible", kind=SpanKind.TOOL) as t:
                    t.set_attribute("violations", 2)
                with tracer.span("rtl_repair", kind=SpanKind.MODEL) as m:
                    m.set_tokens(prompt=80, completion=240)
                    m.set_cost(0.0012)
                    m.set_escalation(EscalationLevel.INNER)
                with tracer.span("verilator", kind=SpanKind.TOOL) as t2:
                    t2.set_attribute("returncode", 0)
            with tracer.span("gate", kind=SpanKind.GATE) as g:
                g.set_attribute("decision", "advance")
        with tracer.span("await_human", kind=SpanKind.HUMAN):
            pass

    [root] = build_trace_tree(tracer.spans, design_id="synthetic")
    kinds_seen = {r.kind for r in root.walk()}
    # The trace tree is complete: every span kind we exercised shows up.
    assert SpanKind.RUN in kinds_seen
    assert SpanKind.STAGE in kinds_seen
    assert SpanKind.LOOP in kinds_seen
    assert SpanKind.MODEL in kinds_seen
    assert SpanKind.TOOL in kinds_seen
    assert SpanKind.GATE in kinds_seen
    assert SpanKind.HUMAN in kinds_seen

    # Cost / token totals on the model spans are summable from the tree.
    total_cost = sum(
        (r.cost_usd or 0.0) for r in root.walk() if r.kind is SpanKind.MODEL
    )
    assert total_cost == pytest.approx(0.0012)
    # Every span carries the run's design_id.
    assert all(r.design_id == "synthetic" for r in root.walk())
