"""F22.2-A acceptance: ``JsonlTracer`` writes one NDJSON line per closed
span, preserves the in-memory composition invariant, and survives the
concurrent-close pattern that LiteLLM streaming exercises.

The tests intentionally stay at the unit level — they drive the tracer
directly without going through the LiteLLM gateway or the tool services
that Phase 2 + Phase 3 will instrument. Wiring those into the tracer is
a separate acceptance.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from chip_agent.design_state import EscalationLevel
from chip_agent.obs.jsonl_tracer import (
    JsonlTracer,
    span_record_to_jsonl_line,
)
from chip_agent.obs.tracing import (
    SpanKind,
    SpanRecord,
    SpanStatus,
    build_trace_tree,
)


# --------------------------------------------------------------------------- #
# Composition invariant — wrapping an InMemoryTracer doesn't break
# the existing ``tracer.spans`` API.
# --------------------------------------------------------------------------- #
def test_in_memory_spans_list_still_populated(tmp_path: Path) -> None:
    """Composition: ``JsonlTracer.spans`` mirrors the wrapped
    InMemoryTracer's list so existing call sites that introspect
    ``tracer.spans`` keep working."""
    tracer = JsonlTracer(tmp_path / "reasoning.jsonl")
    with tracer.run("d"), tracer.span("rtl", kind=SpanKind.STAGE):  # noqa: SIM117
        with tracer.span("model:claude", kind=SpanKind.MODEL) as s:
            s.set_tokens(prompt=100, completion=50)
            s.set_cost(0.012)
    kinds = [r.kind for r in tracer.spans]
    assert kinds == [SpanKind.MODEL, SpanKind.STAGE, SpanKind.RUN]


def test_build_trace_tree_works_against_jsonl_tracer_spans(
    tmp_path: Path,
) -> None:
    """``build_trace_tree`` walks ``tracer.spans`` — must still work
    when the tracer is JSONL-backed (the in-memory list is
    pass-through, not synthesized)."""
    tracer = JsonlTracer(tmp_path / "reasoning.jsonl")
    with tracer.run("d"), tracer.span("stage:rtl", kind=SpanKind.STAGE):  # noqa: SIM117
        with tracer.span("model:claude", kind=SpanKind.MODEL):
            pass
    roots = build_trace_tree(tracer.spans, design_id="d")
    assert len(roots) == 1
    assert roots[0].record.kind is SpanKind.RUN
    # Pre-order walk: RUN, STAGE, MODEL.
    walked = [r.kind for r in roots[0].walk()]
    assert walked == [SpanKind.RUN, SpanKind.STAGE, SpanKind.MODEL]


# --------------------------------------------------------------------------- #
# JSONL on-disk contract — one line per span, close-ordered, parseable.
# --------------------------------------------------------------------------- #
def test_jsonl_has_one_line_per_closed_span(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with tracer.run("d"):
        with tracer.span("stage:rtl", kind=SpanKind.STAGE):  # noqa: SIM117
            with tracer.span("model:claude", kind=SpanKind.MODEL):
                pass
        with tracer.span("tool:verible", kind=SpanKind.TOOL):
            pass
    lines = path.read_text().splitlines()
    assert len(lines) == 4  # MODEL, STAGE, TOOL, RUN (close order)


def test_jsonl_lines_are_close_ordered(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with tracer.run("d"), tracer.span("stage:rtl", kind=SpanKind.STAGE):  # noqa: SIM117
        with tracer.span("model:claude", kind=SpanKind.MODEL):
            pass  # closes first
        # STAGE closes next, RUN last.
    parsed = [json.loads(ln) for ln in path.read_text().splitlines()]
    kinds = [p["kind"] for p in parsed]
    assert kinds == ["model", "stage", "run"]


def test_each_line_is_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with tracer.run("d"), tracer.span("stage:rtl", kind=SpanKind.STAGE):
        pass
    for ln in path.read_text().splitlines():
        json.loads(ln)  # raises if malformed


def test_attributes_pass_through_to_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with tracer.run("d"), tracer.span("model:claude", kind=SpanKind.MODEL) as s:
        s.set_attribute("model_id", "claude-opus-4-7")
        s.set_attribute("temperature", 0.7)
        s.set_attribute("seed", 42)
        s.set_attribute("prompt", "describe the module")
        s.set_attribute("completion", "Here's the spec.")
        s.set_tokens(prompt=100, completion=50)
        s.set_cost(0.012)
    parsed = [json.loads(ln) for ln in path.read_text().splitlines()]
    model_line = next(p for p in parsed if p["kind"] == "model")
    assert model_line["attributes"]["model_id"] == "claude-opus-4-7"
    assert model_line["attributes"]["temperature"] == 0.7
    assert model_line["attributes"]["seed"] == 42
    assert model_line["attributes"]["prompt"] == "describe the module"
    assert model_line["attributes"]["completion"] == "Here's the spec."
    assert model_line["prompt_tokens"] == 100
    assert model_line["completion_tokens"] == 50
    assert model_line["cost_usd"] == 0.012


def test_datetime_serialized_as_iso8601(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with tracer.run("d"):
        pass
    line = json.loads(path.read_text().splitlines()[0])
    # ISO 8601 with timezone — ``datetime.fromisoformat`` round-trips.
    assert "T" in line["started_at"]
    assert line["started_at"].endswith("+00:00") or line["started_at"].endswith("Z")


def test_escalation_level_serialized_as_string(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with tracer.run("d"), tracer.span("loop:outer", kind=SpanKind.LOOP) as s:
        s.set_escalation(EscalationLevel.OUTER)
    line = next(
        json.loads(ln) for ln in path.read_text().splitlines()
        if json.loads(ln)["kind"] == "loop"
    )
    assert line["escalation"] == "outer"


def test_error_span_carries_status_and_error_message(tmp_path: Path) -> None:
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with pytest.raises(RuntimeError), tracer.run("d"):  # noqa: SIM117
        with tracer.span("tool:verilator", kind=SpanKind.TOOL):
            raise RuntimeError("verilator: syntax error")
    parsed = [json.loads(ln) for ln in path.read_text().splitlines()]
    tool = next(p for p in parsed if p["kind"] == "tool")
    assert tool["status"] == "error"
    assert "verilator: syntax error" in tool["error"]


# --------------------------------------------------------------------------- #
# File lifecycle — lazy open, append on resume, idempotent close.
# --------------------------------------------------------------------------- #
def test_file_created_lazily_only_on_first_span_close(tmp_path: Path) -> None:
    path = tmp_path / "subdir" / "reasoning.jsonl"
    JsonlTracer(path)
    # No span closed → no file (and parent subdir not yet materialised).
    assert not path.parent.exists()


def test_parent_directory_created_lazily(tmp_path: Path) -> None:
    path = tmp_path / "fresh-subdir" / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    with tracer.run("d"):
        pass
    assert path.exists()
    assert path.read_text().count("\n") == 1


def test_appending_to_existing_jsonl_preserves_prior_content(
    tmp_path: Path,
) -> None:
    """A resumed run opens the same JSONL in append mode so the TUI's
    byte-offset tail reader sees a continuous stream."""
    path = tmp_path / "reasoning.jsonl"
    # First run.
    tracer_a = JsonlTracer(path)
    with tracer_a.run("d"), tracer_a.span("stage:spec", kind=SpanKind.STAGE):
        pass
    first_run_lines = len(path.read_text().splitlines())
    # Second run on the same path.
    tracer_b = JsonlTracer(path)
    with tracer_b.run("d"), tracer_b.span("stage:plan", kind=SpanKind.STAGE):
        pass
    total_lines = len(path.read_text().splitlines())
    assert total_lines == first_run_lines + 2  # +STAGE +RUN


# --------------------------------------------------------------------------- #
# Concurrency — LiteLLM streaming finalizes spans from background threads.
# --------------------------------------------------------------------------- #
def test_concurrent_span_closes_do_not_interleave_lines(tmp_path: Path) -> None:
    """Twenty threads each open + close a span concurrently. The lock
    around the append must keep every line a complete JSON object."""
    path = tmp_path / "reasoning.jsonl"
    tracer = JsonlTracer(path)
    barrier = threading.Barrier(20)

    def worker(idx: int) -> None:
        barrier.wait()
        with tracer.span(f"model:t{idx}", kind=SpanKind.MODEL) as s:
            s.set_attribute("worker", idx)

    with tracer.run("d"):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    lines = path.read_text().splitlines()
    # 20 worker spans + 1 RUN.
    assert len(lines) == 21
    for ln in lines:
        json.loads(ln)  # malformed line would raise


# --------------------------------------------------------------------------- #
# Pure-function: span_record_to_jsonl_line.
# --------------------------------------------------------------------------- #
def test_span_record_to_jsonl_line_terminates_with_newline() -> None:
    rec = _sample_record()
    line = span_record_to_jsonl_line(rec)
    assert line.endswith("\n")
    # Exactly one newline — no embedded newlines from str payloads.
    assert line.count("\n") == 1


def test_span_record_to_jsonl_line_round_trips_via_json_loads() -> None:
    rec = _sample_record()
    parsed = json.loads(span_record_to_jsonl_line(rec))
    assert parsed["span_id"] == rec.span_id
    assert parsed["kind"] == rec.kind.value
    assert parsed["status"] == rec.status.value


def test_span_record_to_jsonl_line_rejects_unserializable_attribute() -> None:
    """Defensive: an attribute that isn't str/int/float/None becomes a
    loud TypeError rather than a silent crash on the streaming write
    path."""
    rec = _sample_record(attributes={"weird": object()})
    with pytest.raises(TypeError):
        span_record_to_jsonl_line(rec)


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Regression: ``_attach_jsonl_tracer`` returns a populated ``RunArgs``
# instead of mutating the frozen dataclass.
# --------------------------------------------------------------------------- #
def test_attach_jsonl_tracer_handles_frozen_run_args(tmp_path: Path) -> None:
    """``RunArgs`` is ``@dataclass(frozen=True)`` so attribute assignment
    raises ``FrozenInstanceError``. The TUI worker must clone the args
    via ``dataclasses.replace`` and use the returned value."""
    from chip_agent.cli import RunArgs
    from chip_agent.obs.tracing import InMemoryTracer
    from chip_agent.tui.workers.run_worker import _attach_jsonl_tracer

    args = RunArgs(
        cmd="run",
        spec_path=None,
        name="uart_rx",
        run_dir=tmp_path,
        design_id="uart_rx",
        hmac_key=b"k",
    )

    new_args = _attach_jsonl_tracer(args)

    # Original stays untouched.
    assert args.tracer is None
    # The new args carry a JsonlTracer pointed at the per-run reasoning file.
    assert isinstance(new_args.tracer, JsonlTracer)

    # A caller-supplied tracer must be preserved (tests inject InMemoryTracer).
    pre = InMemoryTracer()
    pre_args = RunArgs(
        cmd="run",
        spec_path=None,
        name="uart_rx",
        run_dir=tmp_path,
        design_id="uart_rx",
        hmac_key=b"k",
        tracer=pre,
    )
    assert _attach_jsonl_tracer(pre_args) is pre_args


def _sample_record(
    *,
    attributes: dict[str, object] | None = None,
) -> SpanRecord:
    from datetime import UTC, datetime
    return SpanRecord(
        span_id="abc123",
        parent_id=None,
        name="model:claude",
        kind=SpanKind.MODEL,
        design_id="d",
        started_at=datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 6, 17, 12, 0, 1, tzinfo=UTC),
        duration_ms=1000.0,
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=0.012,
        status=SpanStatus.OK,
        attributes=attributes or {"model_id": "claude-opus-4-7"},
    )
