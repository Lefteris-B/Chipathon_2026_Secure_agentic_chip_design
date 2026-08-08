"""F9.5 — testbench production from the spec.

Covers the four AC strands:

* templated cocotb skeleton for the counter spec drives clk + rst_n +
  en and asserts an observable evolves;
* the persisted testbench provenance links to the plan;
* multi-module plans fall through to the LLM router with
  ``TaskType.TB_GEN``;
* a single-module plan with unrecognisable port shapes also falls
  through to the LLM router.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.agents.testbench_gen import (
    TestbenchGenAgent,
    TestbenchGenError,
)
from chip_agent.design_state import (
    ArtifactKind,
    DesignPlan,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    ModelInvocation,
    ModuleDecl,
    Port,
    Provenance,
    Spec,
    Stage,
    TaskType,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteArtifactStore]:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    yield s
    s.close()


@dataclass
class _RecordingRouter:
    """Stub :class:`ModelRouter` that returns canned cocotb + records every call."""

    text: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="anthropic", model="claude-sonnet-4-6",
            temperature=0.2, seed=None,
            prompt_tokens=200, completion_tokens=120, cost_usd=0.003,
        ),
    )

    def generate(
        self,
        task: TaskType,
        *,
        context: dict[str, Any],
        failure: FailureDiagnosis | None = None,
        escalation: EscalationLevel = EscalationLevel.INNER,
        n: int | None = None,
    ) -> GenerationResult:
        self.calls.append({"task": task, "context": context})
        return GenerationResult(
            candidates=[self.text], chosen=self.text,
            invocation=self.invocation,
        )


def _counter_module() -> ModuleDecl:
    return ModuleDecl(
        module_id="counter",
        name="counter",
        description="8-bit synchronous up-counter.",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="en", direction="in", width=1),
            Port(name="q", direction="out", width=8),
        ],
    )


def _make_plan(
    store: SqliteArtifactStore, *, design_id: str, modules: list[ModuleDecl],
) -> DesignPlan:
    plan = DesignPlan(
        artifact_id=f"{design_id}.plan",
        design_id=design_id,
        top_module_id=modules[0].module_id,
        modules=modules,
        provenance=Provenance(produced_by=Stage.PLAN, agent="test"),
    )
    store.put(plan)
    loaded = store.get_by_id(plan.artifact_id)
    assert isinstance(loaded, DesignPlan)
    return loaded


def _make_spec(
    store: SqliteArtifactStore, *, design_id: str, raw: str = "counter",
) -> Spec:
    spec = Spec(
        artifact_id=f"{design_id}.spec",
        design_id=design_id,
        raw_text=raw, normalized=raw,
        provenance=Provenance(produced_by=Stage.SPEC, agent="test"),
    )
    store.put(spec)
    loaded = store.get_by_id(spec.artifact_id)
    assert isinstance(loaded, Spec)
    return loaded


# --------------------------------------------------------------------------- #
# AC strand 1 — templated cocotb skeleton drives clk + rst_n + en + asserts q
# --------------------------------------------------------------------------- #
def test_templated_counter_skeleton_emits_clk_rst_en_drivers(
    store: SqliteArtifactStore,
) -> None:
    router = _RecordingRouter(text="should not be called")
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    plan = _make_plan(store, design_id="d0", modules=[_counter_module()])

    tb = agent.generate_module(_counter_module(), plan)
    body = store.get_blob(tb.source).decode("utf-8")

    assert "import cocotb" in body
    assert "@cocotb.test()" in body
    assert "dut.clk" in body
    assert "dut.rst_n" in body
    assert "dut.en" in body
    assert "dut.q" in body
    assert "assert " in body
    # No router call — the templated path is purely deterministic.
    assert router.calls == []


def test_templated_path_produces_self_contained_python(
    store: SqliteArtifactStore,
) -> None:
    """The rendered cocotb file is valid Python (compileable)."""
    router = _RecordingRouter(text="unused")
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    plan = _make_plan(store, design_id="d0", modules=[_counter_module()])

    tb = agent.generate_module(_counter_module(), plan)
    body = store.get_blob(tb.source).decode("utf-8")
    compile(body, "<tb>", "exec")  # raises SyntaxError on malformed Python


# --------------------------------------------------------------------------- #
# AC strand 2 — persisted testbench provenance links to plan
# --------------------------------------------------------------------------- #
def test_templated_path_provenance_links_to_plan(
    store: SqliteArtifactStore,
) -> None:
    router = _RecordingRouter(text="unused")
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    plan = _make_plan(store, design_id="d0", modules=[_counter_module()])

    tb = agent.generate_module(_counter_module(), plan)
    assert tb.provenance.inputs == [plan.ref()]
    assert tb.provenance.model is None  # templated path → no model invocation
    assert tb.framework == "cocotb"
    assert tb.target_module == "counter"
    assert tb.kind is ArtifactKind.TESTBENCH


def test_llm_path_provenance_includes_spec_when_supplied(
    store: SqliteArtifactStore,
) -> None:
    router = _RecordingRouter(
        text="import cocotb\n@cocotb.test()\nasync def t(dut):\n    pass",
    )
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    plan = _make_plan(store, design_id="d0", modules=[
        ModuleDecl(module_id="alu", name="alu", description="alu",
                   ports=[Port(name="op", direction="in", width=4)]),
    ])
    spec = _make_spec(store, design_id="d0", raw="ALU spec")

    tb = agent.generate_module(plan.modules[0], plan, spec=spec)
    assert plan.ref() in tb.provenance.inputs
    assert spec.ref() in tb.provenance.inputs
    assert tb.provenance.model == router.invocation


# --------------------------------------------------------------------------- #
# AC strand 3 — multi-module plans fall through to the LLM router
# --------------------------------------------------------------------------- #
def test_multi_module_plan_routes_to_tb_gen_task(
    store: SqliteArtifactStore,
) -> None:
    router = _RecordingRouter(
        text="import cocotb\n@cocotb.test()\nasync def t(dut):\n    pass",
    )
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    plan = _make_plan(store, design_id="d0", modules=[
        _counter_module(),
        ModuleDecl(module_id="rom", name="rom", description="rom",
                   ports=[Port(name="addr", direction="in", width=4)]),
    ])

    tb = agent.generate_module(_counter_module(), plan)

    assert len(router.calls) == 1
    assert router.calls[0]["task"] is TaskType.TB_GEN
    # The system prompt sets the cocotb-only output rule.
    assert "cocotb" in router.calls[0]["context"]["system"].lower()
    body = store.get_blob(tb.source).decode("utf-8")
    assert "import cocotb" in body


# --------------------------------------------------------------------------- #
# AC strand 4 — single-module unusual port shape also falls through to LLM
# --------------------------------------------------------------------------- #
def test_unrecognised_port_shape_falls_through_to_llm(
    store: SqliteArtifactStore,
) -> None:
    """No clk-shaped port → LLM router fires instead of the template."""
    router = _RecordingRouter(
        text="import cocotb\n@cocotb.test()\nasync def t(dut):\n    pass",
    )
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    # Combinational module — no clock, no reset.
    combo = ModuleDecl(
        module_id="adder", name="adder", description="2-input adder",
        ports=[
            Port(name="a", direction="in", width=8),
            Port(name="b", direction="in", width=8),
            Port(name="sum", direction="out", width=9),
        ],
    )
    plan = _make_plan(store, design_id="d0", modules=[combo])

    agent.generate_module(combo, plan)
    assert len(router.calls) == 1
    assert router.calls[0]["task"] is TaskType.TB_GEN


def test_no_output_ports_also_falls_through_to_llm(
    store: SqliteArtifactStore,
) -> None:
    """A clk + rst but no observable output isn't a templated shape."""
    router = _RecordingRouter(
        text="import cocotb\n@cocotb.test()\nasync def t(dut):\n    pass",
    )
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    no_out = ModuleDecl(
        module_id="sink", name="sink", description="sink",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="data", direction="in", width=8),
        ],
    )
    plan = _make_plan(store, design_id="d0", modules=[no_out])

    agent.generate_module(no_out, plan)
    assert len(router.calls) == 1


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
def test_plan_design_id_mismatch_raises(store: SqliteArtifactStore) -> None:
    router = _RecordingRouter(text="unused")
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    plan = _make_plan(store, design_id="d-other", modules=[_counter_module()])
    with pytest.raises(TestbenchGenError, match="design_id"):
        agent.generate_module(_counter_module(), plan)


def test_unknown_module_raises(store: SqliteArtifactStore) -> None:
    router = _RecordingRouter(text="unused")
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    plan = _make_plan(store, design_id="d0", modules=[_counter_module()])
    ghost = ModuleDecl(module_id="ghost", name="ghost", description="g",
                       ports=[])
    with pytest.raises(TestbenchGenError, match="not in plan"):
        agent.generate_module(ghost, plan)


def test_empty_router_output_raises(store: SqliteArtifactStore) -> None:
    """LLM path with whitespace-only output surfaces a typed error."""
    router = _RecordingRouter(text="   \n  ")
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    combo = ModuleDecl(
        module_id="adder", name="adder", description="adder",
        ports=[Port(name="x", direction="in", width=4)],
    )
    plan = _make_plan(store, design_id="d0", modules=[combo])
    with pytest.raises(TestbenchGenError, match="empty cocotb body"):
        agent.generate_module(combo, plan)


def test_persisted_testbench_ends_with_newline(
    store: SqliteArtifactStore,
) -> None:
    """Mirror the F9.3 RTL-blob normalisation so downstream tools see EOF."""
    router = _RecordingRouter(text="unused")
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    plan = _make_plan(store, design_id="d0", modules=[_counter_module()])

    tb = agent.generate_module(_counter_module(), plan)
    body = store.get_blob(tb.source)
    assert body.endswith(b"\n")
    assert not body.endswith(b"\n\n")


# --------------------------------------------------------------------------- #
# LLM-path defensive fence stripping — the model sometimes ignores the
# "no markdown fences" prompt rule. The TB lands on disk as a .py file
# cocotb tries to import; a ```python prefix causes SyntaxError and
# SIM.NO_RESULTS with no parseable failure for the outer loop.
# --------------------------------------------------------------------------- #
def test_llm_path_strips_python_code_fence(
    store: SqliteArtifactStore,
) -> None:
    fenced = (
        "```python\n"
        "import cocotb\n"
        "@cocotb.test()\n"
        "async def t(dut):\n"
        "    pass\n"
        "```"
    )
    router = _RecordingRouter(text=fenced)
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    combo = ModuleDecl(
        module_id="adder", name="adder", description="adder",
        ports=[
            Port(name="a", direction="in", width=8),
            Port(name="b", direction="in", width=8),
            Port(name="sum", direction="out", width=9),
        ],
    )
    plan = _make_plan(store, design_id="d0", modules=[combo])

    tb = agent.generate_module(combo, plan)
    body = store.get_blob(tb.source).decode("utf-8")

    assert not body.startswith("```"), "code fence leaked into TB body"
    assert body.startswith("import cocotb"), body[:40]
    compile(body, "<tb>", "exec")  # SyntaxError if fences leaked through


def test_llm_path_strips_prose_then_fence(
    store: SqliteArtifactStore,
) -> None:
    """The model occasionally emits prose-then-fence (ignoring the
    system prompt). The strict full-string fence match used to fail
    silently and persist the whole prose+code blob, which then broke
    cocotb's Python import at sim time. Verify the search-anywhere
    fallback now extracts just the fence body."""
    response = (
        "Here is the testbench for the adder. The plan ports are a/b/sum.\n"
        "\n"
        "```python\n"
        "import cocotb\n"
        "@cocotb.test()\n"
        "async def t(dut):\n"
        "    pass\n"
        "```\n"
        "\n"
        "Done.\n"
    )
    router = _RecordingRouter(text=response)
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    combo = ModuleDecl(
        module_id="adder", name="adder", description="adder",
        ports=[
            Port(name="a", direction="in", width=8),
            Port(name="b", direction="in", width=8),
            Port(name="sum", direction="out", width=9),
        ],
    )
    plan = _make_plan(store, design_id="d0", modules=[combo])
    tb = agent.generate_module(combo, plan)
    body = store.get_blob(tb.source).decode("utf-8")
    assert "Here is the testbench" not in body
    assert "Done." not in body
    assert body.startswith("import cocotb"), body[:80]
    compile(body, "<tb>", "exec")


def test_llm_path_strips_bare_code_fence(
    store: SqliteArtifactStore,
) -> None:
    """Bare ``` (no language tag) also strips — some models omit the tag."""
    fenced = (
        "```\n"
        "import cocotb\n"
        "@cocotb.test()\n"
        "async def t(dut):\n"
        "    pass\n"
        "```"
    )
    router = _RecordingRouter(text=fenced)
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    combo = ModuleDecl(
        module_id="adder", name="adder", description="adder",
        ports=[Port(name="x", direction="in", width=4)],
    )
    plan = _make_plan(store, design_id="d0", modules=[combo])

    tb = agent.generate_module(combo, plan)
    body = store.get_blob(tb.source).decode("utf-8")
    assert not body.startswith("```")
    compile(body, "<tb>", "exec")


def test_llm_path_fences_only_response_raises_empty_body(
    store: SqliteArtifactStore,
) -> None:
    """A response that's ONLY fences (no body inside) raises the same
    typed error as whitespace-only output — empty after stripping."""
    router = _RecordingRouter(text="```python\n\n```")
    agent = TestbenchGenAgent(router=router, store=store, design_id="d0")
    combo = ModuleDecl(
        module_id="adder", name="adder", description="adder",
        ports=[Port(name="x", direction="in", width=4)],
    )
    plan = _make_plan(store, design_id="d0", modules=[combo])
    with pytest.raises(TestbenchGenError, match="empty cocotb body"):
        agent.generate_module(combo, plan)
