"""F4.3 acceptance: RTL gen + inner loop converges, escalates, and versions per attempt."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chip_agent.agents.rtl_gen import (
    RTLGenerationAgent,
    RTLGenerationError,
    RTLGenerationOutcome,
)
from chip_agent.design_state import (
    ArtifactKind,
    ArtifactStatus,
    DesignConstraints,
    DesignPlan,
    EscalationLevel,
    FailureDiagnosis,
    GenerationResult,
    LintResult,
    ModelInvocation,
    ModuleDecl,
    Port,
    Provenance,
    RTLArtifact,
    Spec,
    Stage,
    TaskType,
    Violation,
)
from chip_agent.store import SqliteArtifactStore


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
@dataclass
class StubRouter:
    """Returns a queue of canned texts. Records which TaskType each call used.

    F20.5: when ``multi_texts`` is set AND the i-th call falls within
    its range, returns ``GenerationResult(candidates=multi_texts[i],
    chosen=multi_texts[i][0])`` so the agent's multi-candidate
    dispatch branch fires. Existing tests that don't set
    ``multi_texts`` see byte-identical single-candidate behaviour.
    """

    texts: list[str]
    multi_texts: list[list[str]] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    invocation: ModelInvocation = field(
        default_factory=lambda: ModelInvocation(
            provider="ollama", model="qwen2.5-coder:7b",
            temperature=0.4, seed=None,
            prompt_tokens=200, completion_tokens=80, cost_usd=0.0,
        )
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
        idx = len(self.calls)
        self.calls.append({
            "task": task, "context": dict(context),
            "escalation": escalation, "n": n,
        })
        if self.multi_texts is not None and idx < len(self.multi_texts):
            cands = list(self.multi_texts[idx])
            chosen = cands[0]
            return GenerationResult(
                candidates=cands, chosen=chosen,
                invocation=self.invocation,
            )
        text = self.texts[idx] if idx < len(self.texts) else self.texts[-1]
        return GenerationResult(
            candidates=[text], chosen=text, invocation=self.invocation,
        )


@dataclass
class StubChecker:
    """Used for both Linter and Elaborator — same call shape."""

    passed_per_call: list[bool]
    violations: list[Violation] = field(
        default_factory=lambda: [Violation(
            code="LATCH_INFERRED", severity="error",
            message="case statement missing default",
            location="counter.v:8:5",
        )]
    )
    seen: list[RTLArtifact] = field(default_factory=list)

    def _result(self, rtl: RTLArtifact) -> LintResult:
        idx = len(self.seen)
        passed = self.passed_per_call[idx] if idx < len(self.passed_per_call) else True
        self.seen.append(rtl)
        return LintResult(
            artifact_id=f"{rtl.design_id}.{rtl.module_id}.lint",
            design_id=rtl.design_id, module_id=rtl.module_id,
            passed=passed,
            violations=[] if passed else list(self.violations),
            provenance=Provenance(
                produced_by=Stage.RTL, inputs=[rtl.ref()],
            ),
        )

    def lint(self, rtl: RTLArtifact) -> LintResult:
        return self._result(rtl)

    def elaborate(self, rtl: RTLArtifact) -> LintResult:
        return self._result(rtl)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path: Path) -> SqliteArtifactStore:
    s = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "runs",
    )
    yield s
    s.close()


def _plan(design_id: str = "d0") -> DesignPlan:
    return DesignPlan(
        artifact_id=f"{design_id}.plan",
        design_id=design_id,
        top_module_id="counter",
        modules=[
            ModuleDecl(
                module_id="counter", name="counter",
                description="4-bit synchronous counter",
                ports=[
                    Port(name="clk", direction="in", width=1),
                    Port(name="rst", direction="in", width=1),
                    Port(name="q", direction="out", width=4),
                ],
            )
        ],
        rationale="Single-module",
        provenance=Provenance(produced_by=Stage.PLAN),
    )


def _spec(design_id: str = "d0") -> Spec:
    return Spec(
        artifact_id=f"{design_id}.spec",
        design_id=design_id,
        raw_text="4-bit counter",
        normalized="* Ports: clk, rst, q\n* Reset: active-high sync\n",
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC),
    )


_DIRTY_RTL = "module counter; case (x) endcase endmodule"
_CLEAN_RTL = "module counter(input clk, input rst, output [3:0] q); endmodule"
_FENCED_CLEAN = f"```verilog\n{_CLEAN_RTL}\n```"


# --------------------------------------------------------------------------- #
# AC: dirty -> clean converges within the budget.
# --------------------------------------------------------------------------- #
def test_inner_loop_converges_from_dirty_to_clean(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(texts=[_DIRTY_RTL, _CLEAN_RTL])
    # Lint fails on attempt 1, passes on attempt 2; elaborate passes when reached.
    linter = StubChecker(passed_per_call=[False, True])
    elaborator = StubChecker(passed_per_call=[True])
    agent = RTLGenerationAgent(
        router=router, store=store, linter=linter, elaborator=elaborator,
        design_id="d0", max_attempts=3,
    )

    outcome = agent.generate_module(_plan(), "counter", spec=_spec())

    assert isinstance(outcome, RTLGenerationOutcome)
    assert outcome.passed is True
    assert outcome.attempts == 2
    assert outcome.last_failure is None
    assert outcome.lint.gate_ok
    assert outcome.elaborate is not None and outcome.elaborate.gate_ok
    # Two router calls: the first was RTL_GEN, the second was RTL_REPAIR.
    assert [c["task"] for c in router.calls] == [
        TaskType.RTL_GEN, TaskType.RTL_REPAIR,
    ]
    # Repair prompt carries the previous source and the failing violation.
    repair_ctx = router.calls[1]["context"]
    assert "Previous RTL" in repair_ctx["prompt"]
    assert "LATCH_INFERRED" in repair_ctx["prompt"]


# --------------------------------------------------------------------------- #
# AC: every attempt is a new artifact version.
# --------------------------------------------------------------------------- #
def test_every_attempt_writes_a_new_version(store: SqliteArtifactStore) -> None:
    router = StubRouter(texts=[_DIRTY_RTL, _CLEAN_RTL])
    linter = StubChecker(passed_per_call=[False, True])
    elaborator = StubChecker(passed_per_call=[True])
    agent = RTLGenerationAgent(
        router=router, store=store, linter=linter, elaborator=elaborator,
        design_id="d0", max_attempts=3,
    )

    outcome = agent.generate_module(_plan(), "counter")

    assert outcome.versions == [1, 2]
    history = store.history("d0.counter.rtl")
    assert [a.version for a in history] == [1, 2]
    # The content hashes differ — distinct sources land in the store.
    assert {a.content_hash for a in history} == {history[0].content_hash, history[1].content_hash}
    assert history[0].content_hash != history[1].content_hash


# --------------------------------------------------------------------------- #
# AC: escalates when budget exhausted (passed=False + last_failure set).
# --------------------------------------------------------------------------- #
def test_budget_exhaustion_returns_failure_with_last_failure_ref(
    store: SqliteArtifactStore,
) -> None:
    # Every attempt produces RTL that lint refuses. Each is textually distinct
    # so the content-addressed store assigns a fresh version per attempt
    # (the AC: every attempt is a new artifact version). The distinctness is
    # inside the module body — leading prose/comments are stripped by the
    # F18 ``_strip_fences`` extractor before storage.
    router = StubRouter(texts=[
        "module counter; case (1) endcase endmodule",
        "module counter; case (2) endcase endmodule",
        "module counter; case (3) endcase endmodule",
    ])
    linter = StubChecker(passed_per_call=[False, False, False, False])
    elaborator = StubChecker(passed_per_call=[True])  # never reached
    agent = RTLGenerationAgent(
        router=router, store=store, linter=linter, elaborator=elaborator,
        design_id="d0", max_attempts=3,
    )

    outcome = agent.generate_module(_plan(), "counter")

    assert outcome.passed is False
    assert outcome.attempts == 3
    assert outcome.last_failure is not None
    assert outcome.last_failure.kind is ArtifactKind.LINT
    assert outcome.versions == [1, 2, 3]
    # The two follow-up attempts were both repairs.
    tasks = [c["task"] for c in router.calls]
    assert tasks == [TaskType.RTL_GEN, TaskType.RTL_REPAIR, TaskType.RTL_REPAIR]


def test_identical_router_output_dedupes_to_same_version(
    store: SqliteArtifactStore,
) -> None:
    # If the model returns identical content twice (cache, low-temperature, etc.),
    # the store's content-addressed dedupe re-uses the existing version. The
    # outer "every attempt is a new version" promise then holds *per distinct
    # content*; the attempts counter still ticks so the budget is respected.
    router = StubRouter(texts=[_DIRTY_RTL, _DIRTY_RTL])
    linter = StubChecker(passed_per_call=[False, False])
    elaborator = StubChecker(passed_per_call=[True])
    agent = RTLGenerationAgent(
        router=router, store=store, linter=linter, elaborator=elaborator,
        design_id="d0", max_attempts=2,
    )

    outcome = agent.generate_module(_plan(), "counter")
    assert outcome.passed is False
    assert outcome.attempts == 2
    assert outcome.versions == [1, 1]  # store dedupe collapses to one version
    assert len(store.history("d0.counter.rtl")) == 1


def test_elaborate_failure_triggers_repair(store: SqliteArtifactStore) -> None:
    # Lint always passes; elaborate fails on first call, passes on second.
    router = StubRouter(texts=[_DIRTY_RTL, _CLEAN_RTL])
    linter = StubChecker(passed_per_call=[True, True])
    elaborator = StubChecker(
        passed_per_call=[False, True],
        violations=[Violation(
            code="PORT_WIDTH_MISMATCH", severity="error",
            message="expects 8 bits, got 4",
            location="counter.v:10:5",
        )],
    )
    agent = RTLGenerationAgent(
        router=router, store=store, linter=linter, elaborator=elaborator,
        design_id="d0", max_attempts=3,
    )

    outcome = agent.generate_module(_plan(), "counter")

    assert outcome.passed is True
    assert outcome.attempts == 2
    # Repair prompt was driven by the elaborate violation.
    repair_ctx = router.calls[1]["context"]
    assert "PORT_WIDTH_MISMATCH" in repair_ctx["prompt"]


def test_lint_failure_short_circuits_elaborate(store: SqliteArtifactStore) -> None:
    # First lint fails; elaborate must NOT be called for that attempt.
    router = StubRouter(texts=[_DIRTY_RTL, _CLEAN_RTL])
    linter = StubChecker(passed_per_call=[False, True])
    elaborator = StubChecker(passed_per_call=[True])
    agent = RTLGenerationAgent(
        router=router, store=store, linter=linter, elaborator=elaborator,
        design_id="d0", max_attempts=3,
    )
    agent.generate_module(_plan(), "counter")
    # Elaborator only saw the second (clean) attempt.
    assert len(elaborator.seen) == 1
    assert linter.seen[0].version == 1
    assert elaborator.seen[0].version == 2


# --------------------------------------------------------------------------- #
# Prompts + provenance
# --------------------------------------------------------------------------- #
def test_first_attempt_uses_rtl_gen_system_prompt(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(texts=[_CLEAN_RTL])
    agent = RTLGenerationAgent(
        router=router, store=store,
        linter=StubChecker(passed_per_call=[True]),
        elaborator=StubChecker(passed_per_call=[True]),
        design_id="d0",
    )
    agent.generate_module(_plan(), "counter", spec=_spec())
    ctx = router.calls[0]["context"]
    assert ctx["system"] == RTLGenerationAgent.GEN_SYSTEM_PROMPT
    assert "Module: counter" in ctx["prompt"]
    assert "Ports:" in ctx["prompt"]
    assert "- clk" in ctx["prompt"]
    # The spec's normalised text is included when supplied.
    assert "Source spec" in ctx["prompt"]
    assert "Reset: active-high sync" in ctx["prompt"]


def test_provenance_links_plan_spec_and_previous_rtl(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(texts=[_DIRTY_RTL, _CLEAN_RTL])
    linter = StubChecker(passed_per_call=[False, True])
    elaborator = StubChecker(passed_per_call=[True])
    agent = RTLGenerationAgent(
        router=router, store=store, linter=linter, elaborator=elaborator,
        design_id="d0", max_attempts=3,
    )
    spec = _spec()
    plan = _plan()
    outcome = agent.generate_module(plan, "counter", spec=spec)

    history = store.history("d0.counter.rtl")
    first, second = history
    # First attempt: inputs include plan + spec; no previous RTL yet.
    first_input_ids = {r.artifact_id for r in first.provenance.inputs}
    assert "d0.plan" in first_input_ids
    assert "d0.spec" in first_input_ids
    # Second attempt: also references the previous RTL.
    second_input_ids = {r.artifact_id for r in second.provenance.inputs}
    assert "d0.plan" in second_input_ids
    assert "d0.spec" in second_input_ids
    assert "d0.counter.rtl" in second_input_ids
    # Provenance.notes records the attempt number.
    assert first.provenance.notes == "attempt 1"
    assert second.provenance.notes == "attempt 2"
    # Model invocation flows through.
    assert outcome.rtl.provenance.model is not None


# --------------------------------------------------------------------------- #
# Code-fence stripping + error paths
# --------------------------------------------------------------------------- #
def test_code_fences_stripped(store: SqliteArtifactStore) -> None:
    router = StubRouter(texts=[_FENCED_CLEAN])
    agent = RTLGenerationAgent(
        router=router, store=store,
        linter=StubChecker(passed_per_call=[True]),
        elaborator=StubChecker(passed_per_call=[True]),
        design_id="d0",
    )
    outcome = agent.generate_module(_plan(), "counter")
    body = store.get_blob(outcome.rtl.source)
    # The agent normalises to exactly one trailing newline so Verible's
    # posix-eof style rule doesn't trip the non-zero exit code.
    assert body.decode() == _CLEAN_RTL + "\n"


# --------------------------------------------------------------------------- #
# _strip_fences extraction — direct unit tests on the three-layer extractor.
#
# Background: the UART RX outer-loop produced a 2085-char response where the
# first ~1500 chars were reasoning prose ("Looking at the failing test...",
# "Let me reconsider:") and only the last ~500 chars were the Verilog. The
# pre-fix ``_strip_fences`` used ``_FENCE_RE.match`` (strict full-string),
# so the entire prose+code blob was persisted as RTL. Verilator parsed the
# inline-code backticks (``\`test_incremental_shift_evolution``) as
# undefined macros and the outer loop spiralled into worse and worse
# garbage (5 → 8 → 17 → 9 syntax errors before exhausting).
# --------------------------------------------------------------------------- #
def test_strip_fences_extracts_fence_anywhere() -> None:
    """A fenced block following prose must be extracted — the strict
    full-string match used to drop the entire response straight through."""
    from chip_agent.agents.rtl_gen import _strip_fences
    prose_then_fence = (
        "Looking at the failing test, the issue is the shift direction.\n"
        "Let me try the left-shift variant:\n"
        "\n"
        "```verilog\n"
        f"{_CLEAN_RTL}\n"
        "```\n"
        "\n"
        "This should fix it.\n"
    )
    assert _strip_fences(prose_then_fence) == _CLEAN_RTL


def test_strip_fences_falls_back_to_module_span() -> None:
    """Prose with no fences at all but a real ``module..endmodule`` body
    must extract just the module span. Live observed shape from the
    failed UART RX run: prose preamble + bare Verilog (no fences)."""
    from chip_agent.agents.rtl_gen import _strip_fences
    prose_then_bare_rtl = (
        "Looking at the failing test `test_incremental_shift_evolution`, "
        "the issue is likely with the shift direction. Let me try the "
        "left-shift variant as the fix:\n"
        "\n"
        f"{_CLEAN_RTL}\n"
    )
    extracted = _strip_fences(prose_then_bare_rtl)
    assert extracted == _CLEAN_RTL
    # Critical: the backtick-wrapped test name from the prose must not
    # appear in the extracted output (it caused the live Verilator
    # ``Define or directive not defined`` errors).
    assert "`test_incremental_shift_evolution" not in extracted
    assert "Looking at the failing test" not in extracted


def test_strip_fences_module_span_handles_multi_line_body() -> None:
    """The non-greedy DOTALL must not stop at the first ``endmodule``-
    looking string inside a string literal or comment; a real
    multi-line module body must survive intact."""
    from chip_agent.agents.rtl_gen import _strip_fences
    multiline = (
        "Here is the fix:\n"
        "\n"
        "module rx_shift_register (\n"
        "    input  wire       clk,\n"
        "    input  wire       rst,\n"
        "    output reg  [7:0] shift_out\n"
        ");\n"
        "    always @(posedge clk) begin\n"
        "        if (rst) shift_out <= 8'b0;\n"
        "        else     shift_out <= {shift_out[6:0], 1'b1};\n"
        "    end\n"
        "endmodule\n"
        "\n"
        "Note: this is correct.\n"
    )
    extracted = _strip_fences(multiline)
    assert extracted.startswith("module rx_shift_register")
    assert extracted.endswith("endmodule")
    assert "Note: this is correct." not in extracted
    assert "Here is the fix:" not in extracted


def test_strip_fences_does_not_extract_module_keyword_in_prose() -> None:
    """The word "module" appears in prose all the time — the extractor
    must anchor on a real Verilog ``module <name>`` start-of-line, not
    mid-sentence. If we false-match the prose word, the resulting RTL
    has prose interleaved with the actual body."""
    from chip_agent.agents.rtl_gen import _strip_fences
    prose = (
        "I will write a new module that handles the edge case.\n"
        "The previous module had the wrong shift direction.\n"
        "\n"
        f"{_CLEAN_RTL}\n"
    )
    extracted = _strip_fences(prose)
    assert extracted == _CLEAN_RTL


def test_strip_fences_pure_code_unchanged() -> None:
    """The most common case: the model obeys the system prompt and
    returns just the Verilog. Must pass through identical."""
    from chip_agent.agents.rtl_gen import _strip_fences
    assert _strip_fences(_CLEAN_RTL) == _CLEAN_RTL
    assert _strip_fences(_FENCED_CLEAN) == _CLEAN_RTL


def test_empty_router_output_rejected(store: SqliteArtifactStore) -> None:
    router = StubRouter(texts=["   \n  "])
    agent = RTLGenerationAgent(
        router=router, store=store,
        linter=StubChecker(passed_per_call=[True]),
        elaborator=StubChecker(passed_per_call=[True]),
        design_id="d0",
    )
    with pytest.raises(RTLGenerationError):
        agent.generate_module(_plan(), "counter")


def test_unknown_module_rejected(store: SqliteArtifactStore) -> None:
    router = StubRouter(texts=[_CLEAN_RTL])
    agent = RTLGenerationAgent(
        router=router, store=store,
        linter=StubChecker(passed_per_call=[True]),
        elaborator=StubChecker(passed_per_call=[True]),
        design_id="d0",
    )
    with pytest.raises(RTLGenerationError) as ei:
        agent.generate_module(_plan(), "ghost")
    assert "ghost" in str(ei.value)


def test_design_id_mismatch_rejected(store: SqliteArtifactStore) -> None:
    router = StubRouter(texts=[_CLEAN_RTL])
    agent = RTLGenerationAgent(
        router=router, store=store,
        linter=StubChecker(passed_per_call=[True]),
        elaborator=StubChecker(passed_per_call=[True]),
        design_id="alpha",
    )
    with pytest.raises(RTLGenerationError):
        agent.generate_module(_plan(design_id="beta"), "counter")


def test_max_attempts_validated() -> None:
    with pytest.raises(ValueError):
        RTLGenerationAgent(
            router=StubRouter(texts=["x"]),
            store=None,  # type: ignore[arg-type]
            linter=StubChecker(passed_per_call=[True]),
            elaborator=StubChecker(passed_per_call=[True]),
            design_id="d0", max_attempts=0,
        )


def test_empty_design_id_rejected() -> None:
    with pytest.raises(ValueError):
        RTLGenerationAgent(
            router=StubRouter(texts=["x"]),
            store=None,  # type: ignore[arg-type]
            linter=StubChecker(passed_per_call=[True]),
            elaborator=StubChecker(passed_per_call=[True]),
            design_id="",
        )


# --------------------------------------------------------------------------- #
# Outcome correctness
# --------------------------------------------------------------------------- #
def test_outcome_rtl_status_reflects_lifecycle(
    store: SqliteArtifactStore,
) -> None:
    router = StubRouter(texts=[_CLEAN_RTL])
    agent = RTLGenerationAgent(
        router=router, store=store,
        linter=StubChecker(passed_per_call=[True]),
        elaborator=StubChecker(passed_per_call=[True]),
        design_id="d0",
    )
    outcome = agent.generate_module(_plan(), "counter")
    # The agent only puts; it does NOT promote — that's the blackboard's job.
    # So the artifact is DRAFT until the control graph promotes_to_head.
    assert outcome.rtl.status is ArtifactStatus.DRAFT


# --------------------------------------------------------------------------- #
# F20.2 — Cross-module RTL prompt context.
# --------------------------------------------------------------------------- #
def _uart_rx_plan(design_id: str = "d0") -> DesignPlan:
    """Five-module UART RX plan: top + four children. ``rx_fsm`` has its
    parent (``uart_rx_top``) and three siblings (``baud_gen``,
    ``rx_sync``, ``rx_shift_reg``). A floating leaf (``unrelated``)
    sits outside the parent's depends_on to verify the isolation
    invariant (it must NOT appear in ``rx_fsm``'s prompt)."""
    return DesignPlan(
        artifact_id=f"{design_id}.plan",
        design_id=design_id,
        top_module_id="uart_rx_top",
        modules=[
            ModuleDecl(
                module_id="baud_gen", name="baud_gen",
                description="Baud rate generator",
                ports=[
                    Port(name="clk", direction="in", width=1),
                    Port(name="rst_n", direction="in", width=1),
                    Port(name="baud_tick", direction="out", width=1),
                ],
            ),
            ModuleDecl(
                module_id="rx_sync", name="rx_sync",
                description="Two-stage synchroniser",
                ports=[
                    Port(name="clk", direction="in", width=1),
                    Port(name="rst_n", direction="in", width=1),
                    Port(name="rx_in", direction="in", width=1),
                    Port(name="rx_clean", direction="out", width=1),
                ],
            ),
            ModuleDecl(
                module_id="rx_fsm", name="rx_fsm",
                description="Receive state machine",
                ports=[
                    Port(name="clk", direction="in", width=1),
                    Port(name="rst_n", direction="in", width=1),
                    Port(name="rx_clean", direction="in", width=1),
                    Port(name="baud_tick", direction="in", width=1),
                    Port(name="shift_en", direction="out", width=1),
                    Port(name="valid", direction="out", width=1),
                ],
            ),
            ModuleDecl(
                module_id="rx_shift_reg", name="rx_shift_reg",
                description="Shift register",
                ports=[
                    Port(name="clk", direction="in", width=1),
                    Port(name="rst_n", direction="in", width=1),
                    Port(name="rx_clean", direction="in", width=1),
                    Port(name="shift_en", direction="in", width=1),
                    Port(name="data", direction="out", width=8),
                ],
            ),
            ModuleDecl(
                module_id="uart_rx_top", name="uart_rx_top",
                description="UART RX top",
                ports=[
                    Port(name="clk", direction="in", width=1),
                    Port(name="rst_n", direction="in", width=1),
                    Port(name="rx", direction="in", width=1),
                    Port(name="data", direction="out", width=8),
                    Port(name="valid", direction="out", width=1),
                ],
                depends_on=["baud_gen", "rx_sync", "rx_fsm", "rx_shift_reg"],
            ),
            # Isolation control: a separate leaf with no parent (no module
            # has it in its depends_on). Must NOT appear in rx_fsm's prompt.
            ModuleDecl(
                module_id="unrelated", name="unrelated",
                description="Leaf with no parent",
                ports=[Port(name="x", direction="in", width=1)],
            ),
        ],
        rationale="Five-module UART RX with isolation control.",
        provenance=Provenance(produced_by=Stage.PLAN),
    )


def test_find_parent_returns_none_for_top_module() -> None:
    """Top module has no parent."""
    from chip_agent.agents.rtl_gen import _find_parent
    plan = _uart_rx_plan()
    assert _find_parent(plan, "uart_rx_top") is None


def test_find_parent_returns_parent_for_child_module() -> None:
    """A child module's parent is the one whose depends_on lists it."""
    from chip_agent.agents.rtl_gen import _find_parent
    plan = _uart_rx_plan()
    parent = _find_parent(plan, "rx_fsm")
    assert parent is not None
    assert parent.module_id == "uart_rx_top"


def test_find_parent_returns_none_for_unrelated_leaf() -> None:
    """A leaf module that no other module depends on has no parent."""
    from chip_agent.agents.rtl_gen import _find_parent
    plan = _uart_rx_plan()
    assert _find_parent(plan, "unrelated") is None


def test_sibling_modules_excludes_self_and_unrelated() -> None:
    """``rx_fsm``'s siblings are the parent's other children, in
    declaration order; the unrelated leaf is excluded."""
    from chip_agent.agents.rtl_gen import _sibling_modules
    plan = _uart_rx_plan()
    siblings = _sibling_modules(plan, "rx_fsm")
    assert [s.module_id for s in siblings] == [
        "baud_gen", "rx_sync", "rx_shift_reg",
    ]


def test_sibling_modules_empty_for_top() -> None:
    from chip_agent.agents.rtl_gen import _sibling_modules
    plan = _uart_rx_plan()
    assert _sibling_modules(plan, "uart_rx_top") == []


def test_render_inferred_instantiation_shape() -> None:
    """The rendered instantiation lists every port .name(name) wired."""
    from chip_agent.agents.rtl_gen import _render_inferred_instantiation
    m = ModuleDecl(
        module_id="counter", name="counter", description="c",
        ports=[
            Port(name="clk", direction="in", width=1),
            Port(name="rst_n", direction="in", width=1),
            Port(name="q", direction="out", width=8),
        ],
    )
    rendered = _render_inferred_instantiation(m)
    assert rendered.startswith("counter u_counter (")
    assert rendered.endswith(");")
    assert "    .clk(clk)" in rendered
    assert "    .rst_n(rst_n)" in rendered
    assert "    .q(q)" in rendered


def test_render_inferred_instantiation_no_ports() -> None:
    """Degenerate zero-port module yields a single-line instantiation."""
    from chip_agent.agents.rtl_gen import _render_inferred_instantiation
    m = ModuleDecl(
        module_id="void", name="void", description="v", ports=[],
    )
    assert _render_inferred_instantiation(m) == "void u_void ();"


def test_gen_prompt_omits_parent_and_siblings_sections_when_no_kwargs() -> None:
    """Backwards compat: ``_gen_prompt(module, spec)`` without the new
    kwargs produces a prompt without the F20.2 section headers — pins
    that demo goldens / single-module plans see no semantic drift."""
    from chip_agent.agents.rtl_gen import _gen_prompt
    m = ModuleDecl(
        module_id="counter", name="counter", description="c",
        ports=[Port(name="clk", direction="in", width=1)],
    )
    prompt = _gen_prompt(m, _spec())
    assert "Parent module instantiation" not in prompt
    assert "Sibling modules" not in prompt


def test_gen_prompt_includes_parent_section_when_supplied() -> None:
    """``parent=...`` adds an inferred instantiation inside a verilog fence."""
    from chip_agent.agents.rtl_gen import _gen_prompt
    plan = _uart_rx_plan()
    rx_fsm = next(m for m in plan.modules if m.module_id == "rx_fsm")
    parent = next(m for m in plan.modules if m.module_id == "uart_rx_top")
    prompt = _gen_prompt(rx_fsm, _spec(), parent=parent)

    assert "Parent module instantiation (parent: uart_rx_top):" in prompt
    assert "```verilog" in prompt
    assert "rx_fsm u_rx_fsm (" in prompt
    assert ".clk(clk)" in prompt
    assert "Sibling modules" not in prompt  # we only passed parent


def test_gen_prompt_includes_siblings_section_when_supplied() -> None:
    """``siblings=...`` enumerates each sibling's name + ports."""
    from chip_agent.agents.rtl_gen import _gen_prompt, _sibling_modules
    plan = _uart_rx_plan()
    rx_fsm = next(m for m in plan.modules if m.module_id == "rx_fsm")
    siblings = _sibling_modules(plan, "rx_fsm")
    prompt = _gen_prompt(rx_fsm, _spec(), siblings=siblings)

    assert "Sibling modules (ports you may share signal names with):" in prompt
    assert "- baud_gen (id=baud_gen):" in prompt
    assert "- rx_sync (id=rx_sync):" in prompt
    assert "- rx_shift_reg (id=rx_shift_reg):" in prompt
    # ports of each sibling appear
    assert "baud_tick: out" in prompt
    assert "rx_clean: out" in prompt
    # the unrelated leaf is NOT listed.
    assert "unrelated" not in prompt


def test_gen_prompt_for_uart_rx_fsm_contains_parent_and_three_siblings_only() -> None:
    """**AC**: a multi-module UART RX run shows the parent module's
    instantiation + sibling ports in ``rx_fsm``'s prompt; isolation of
    unrelated modules preserved.
    """
    from chip_agent.agents.rtl_gen import (
        _find_parent,
        _gen_prompt,
        _sibling_modules,
    )
    plan = _uart_rx_plan()
    rx_fsm = next(m for m in plan.modules if m.module_id == "rx_fsm")
    parent = _find_parent(plan, "rx_fsm")
    siblings = _sibling_modules(plan, "rx_fsm")
    prompt = _gen_prompt(rx_fsm, _spec(), parent=parent, siblings=siblings)

    # Parent instantiation present.
    assert "Parent module instantiation (parent: uart_rx_top):" in prompt
    assert "rx_fsm u_rx_fsm (" in prompt
    # Three siblings present.
    assert "- baud_gen (id=baud_gen):" in prompt
    assert "- rx_sync (id=rx_sync):" in prompt
    assert "- rx_shift_reg (id=rx_shift_reg):" in prompt
    # Isolation: the unrelated leaf must NOT appear.
    assert "unrelated" not in prompt
    # rx_fsm is not listed as its own sibling.
    siblings_section = prompt.split("Sibling modules", 1)[1]
    assert "(id=rx_fsm)" not in siblings_section


# --------------------------------------------------------------------------- #
# F20.5 — Functional candidate ranking (lint + elaborate + LOC over n>1
# router output, with per-candidate scores stamped on the winner's
# ``provenance.config``).
# --------------------------------------------------------------------------- #
# Counter-shaped fixtures sized by LOC so the AC test can pin which
# survivor wins by line count. ``_RTL_3LINE`` < ``_RTL_5LINE``.
_RTL_3LINE = (
    "module counter(input clk, input rst, output [3:0] q);\n"
    "  // single-line\n"
    "endmodule\n"
)
_RTL_5LINE = (
    "module counter(input clk, input rst, output [3:0] q);\n"
    "  // a\n"
    "  // b\n"
    "  // c\n"
    "endmodule\n"
)
_RTL_7LINE = (
    "module counter(input clk, input rst, output [3:0] q);\n"
    "  // a\n"
    "  // b\n"
    "  // c\n"
    "  // d\n"
    "  // e\n"
    "endmodule\n"
)
_RTL_9LINE = (
    "module counter(input clk, input rst, output [3:0] q);\n"
    "  // a\n"
    "  // b\n"
    "  // c\n"
    "  // d\n"
    "  // e\n"
    "  // f\n"
    "  // g\n"
    "endmodule\n"
)


def _rank_args(store: SqliteArtifactStore) -> dict[str, Any]:
    """Common kwargs for direct ``_rank_candidates`` calls."""
    from chip_agent.agents.rtl_gen import _rank_candidates  # noqa: F401
    return {
        "module": _plan().modules[0],
        "store": store,
        "language": "verilog",
        "design_id": "d0",
        "agent_name": "rtl_gen",
    }


def test_rank_candidates_two_survivors_smaller_wins(
    store: SqliteArtifactStore,
) -> None:
    """Both candidates pass lint + elaborate; the shorter-by-LOC one
    wins; both score rows are present with rank 1 = winner."""
    from chip_agent.agents.rtl_gen import _rank_candidates
    # ``passed_per_call`` is consumed call-by-call across both linter
    # and elaborator: 2 lints + 2 elaborates = 4 entries each.
    linter = StubChecker(passed_per_call=[True, True])
    elaborator = StubChecker(passed_per_call=[True, True])
    winner, scores = _rank_candidates(
        # Note: pass the LARGER (5-line) candidate first so the
        # ranker has to actively pick the smaller one.
        [_RTL_5LINE, _RTL_3LINE],
        linter=linter, elaborator=elaborator,
        **_rank_args(store),
    )
    assert winner == _RTL_3LINE
    assert len(scores) == 2
    assert scores[0] == {
        "rank": 1, "lines": 3, "lint_ok": True, "elab_ok": True,
    }
    assert scores[1] == {
        "rank": 2, "lines": 5, "lint_ok": True, "elab_ok": True,
    }


def test_rank_candidates_drops_failing_elaborate(
    store: SqliteArtifactStore,
) -> None:
    """4 candidates, 2 fail elaborate (positions 1 + 3). The 2 survivors
    (positions 0 + 2) get ranks 1+2 by LOC; losers get ranks 3+4 in
    original-list order with ``elab_ok=False``.
    """
    from chip_agent.agents.rtl_gen import _rank_candidates
    # All 4 lint pass; elaborate sees them in order: cand0 OK, cand1
    # FAIL, cand2 OK, cand3 FAIL. So passed_per_call for the
    # elaborator follows the candidate order minus lint-failures
    # (none here).
    linter = StubChecker(passed_per_call=[True, True, True, True])
    elaborator = StubChecker(
        passed_per_call=[True, False, True, False],
    )
    # Candidates ordered so survivors (idx 0+2) are NOT in LOC order;
    # the ranker has to actively re-sort them.
    cands = [_RTL_7LINE, _RTL_5LINE, _RTL_3LINE, _RTL_9LINE]
    winner, scores = _rank_candidates(
        cands,
        linter=linter, elaborator=elaborator,
        **_rank_args(store),
    )
    # Survivor at original-idx 2 (_RTL_3LINE, 3 lines) wins over
    # survivor at original-idx 0 (_RTL_7LINE, 7 lines).
    assert winner == _RTL_3LINE
    assert len(scores) == 4
    # Survivors first, sorted by LOC.
    assert scores[0] == {
        "rank": 1, "lines": 3, "lint_ok": True, "elab_ok": True,
    }
    assert scores[1] == {
        "rank": 2, "lines": 7, "lint_ok": True, "elab_ok": True,
    }
    # Losers next, preserving original candidate-list order.
    assert scores[2] == {
        "rank": 3, "lines": 5, "lint_ok": True, "elab_ok": False,
    }
    assert scores[3] == {
        "rank": 4, "lines": 9, "lint_ok": True, "elab_ok": False,
    }


def test_rank_candidates_drops_failing_lint(
    store: SqliteArtifactStore,
) -> None:
    """4 candidates, 2 fail lint. Survivors at the head sorted by LOC;
    lint losers in original order with ``lint_ok=False``. Lint failures
    short-circuit elaborate — those rows carry ``elab_ok=False``."""
    from chip_agent.agents.rtl_gen import _rank_candidates
    # cand0 lint OK, cand1 lint FAIL, cand2 lint OK, cand3 lint FAIL.
    linter = StubChecker(
        passed_per_call=[True, False, True, False],
    )
    # Elaborator only sees the 2 lint-survivors → 2 entries.
    elaborator = StubChecker(passed_per_call=[True, True])
    cands = [_RTL_7LINE, _RTL_5LINE, _RTL_3LINE, _RTL_9LINE]
    winner, scores = _rank_candidates(
        cands,
        linter=linter, elaborator=elaborator,
        **_rank_args(store),
    )
    assert winner == _RTL_3LINE  # 3 < 7 among lint survivors
    assert len(scores) == 4
    assert scores[0]["rank"] == 1 and scores[0]["lines"] == 3
    assert scores[0]["lint_ok"] is True and scores[0]["elab_ok"] is True
    assert scores[1]["rank"] == 2 and scores[1]["lines"] == 7
    assert scores[1]["lint_ok"] is True and scores[1]["elab_ok"] is True
    # Losers — lint failed → elab_ok False (short-circuit).
    assert scores[2]["lines"] == 5
    assert scores[2]["lint_ok"] is False and scores[2]["elab_ok"] is False
    assert scores[3]["lines"] == 9
    assert scores[3]["lint_ok"] is False and scores[3]["elab_ok"] is False


def test_rank_candidates_raises_when_no_survivors(
    store: SqliteArtifactStore,
) -> None:
    """Every candidate fails elaborate ⇒ RTLGenerationError so the
    inner-loop retry budget can decide what's next."""
    from chip_agent.agents.rtl_gen import (
        RTLGenerationError,
        _rank_candidates,
    )
    linter = StubChecker(passed_per_call=[True, True])
    elaborator = StubChecker(passed_per_call=[False, False])
    with pytest.raises(RTLGenerationError, match="failed lint or elaborate"):
        _rank_candidates(
            [_RTL_3LINE, _RTL_5LINE],
            linter=linter, elaborator=elaborator,
            **_rank_args(store),
        )


def test_n1_path_omits_candidate_scores(
    store: SqliteArtifactStore,
) -> None:
    """Back-compat invariant: when ``len(result.candidates) == 1``
    (today's demo default), the persisted RTL's
    ``provenance.config`` has NO ``candidate_scores`` key.
    """
    router = StubRouter(texts=[_CLEAN_RTL])
    agent = RTLGenerationAgent(
        router=router, store=store,
        linter=StubChecker(passed_per_call=[True]),
        elaborator=StubChecker(passed_per_call=[True]),
        design_id="d0",
    )
    outcome = agent.generate_module(_plan(), "counter")
    assert "candidate_scores" not in outcome.rtl.provenance.config


def test_multi_candidate_ranking_picks_smallest_surviving_candidate(
    store: SqliteArtifactStore,
) -> None:
    """**AC**: a multi-candidate router call with 4 candidates of
    which 2 fail elaborate yields a 2-candidate survivor pool; the
    smaller-by-LOC survivor wins; the 4-entry ranking lands on the
    winner's ``provenance.config['candidate_scores']`` with the exact
    AC-named payload shape.

    Survivor positions chosen so neither is the smallest in original
    order — the ranker has to actively re-sort.

      candidate 0 (_RTL_7LINE)  lint OK  elab OK  ← survivor, 7 lines
      candidate 1 (_RTL_5LINE)  lint OK  elab FAIL
      candidate 2 (_RTL_3LINE)  lint OK  elab OK  ← survivor, 3 lines (WINNER)
      candidate 3 (_RTL_9LINE)  lint OK  elab FAIL

    Then the inner loop re-verifies the winner via lint + elaborate
    once more (cheap; the winner's blob is content-addressed), so
    ``passed_per_call`` must cover those tail entries too.
    """
    router = StubRouter(
        texts=[_CLEAN_RTL],
        multi_texts=[[_RTL_7LINE, _RTL_5LINE, _RTL_3LINE, _RTL_9LINE]],
    )
    # 4 lints during ranking + 1 lint on the winner during the
    # inner-loop's redundant verification = 5 lint calls.
    linter = StubChecker(
        passed_per_call=[True, True, True, True, True],
    )
    # 4 elaborates during ranking (lint passed for all) + 1 elaborate
    # on the winner = 5 elab calls. Match the cand0..3 pass/fail
    # pattern, then the winner.
    elaborator = StubChecker(
        passed_per_call=[True, False, True, False, True],
    )
    agent = RTLGenerationAgent(
        router=router, store=store,
        linter=linter, elaborator=elaborator,
        design_id="d0",
    )
    outcome = agent.generate_module(_plan(), "counter")

    # Winner is the smaller-by-LOC survivor — _RTL_3LINE.
    persisted = store.get_blob(outcome.rtl.source).decode()
    # The agent normalises trailing newline; assert on the trimmed body.
    assert persisted.strip() == _RTL_3LINE.strip()

    scores = outcome.rtl.provenance.config["candidate_scores"]
    assert len(scores) == 4
    # Survivors first, by LOC ascending; losers after, in original
    # candidate-list order. Each row has the exact AC-named shape.
    assert scores[0] == {
        "rank": 1, "lines": 3, "lint_ok": True, "elab_ok": True,
    }
    assert scores[1] == {
        "rank": 2, "lines": 7, "lint_ok": True, "elab_ok": True,
    }
    assert scores[2] == {
        "rank": 3, "lines": 5, "lint_ok": True, "elab_ok": False,
    }
    assert scores[3] == {
        "rank": 4, "lines": 9, "lint_ok": True, "elab_ok": False,
    }
