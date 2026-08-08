"""Cocotb testbench-generation agent (F9.5).

Produces a typed :class:`TestbenchArtifact` (cocotb framework) for each
module the planner declares. F9.5 ships two paths:

* **Templated path** — single-module plans whose interface has a
  recognisable ``clk`` / ``rst_n`` (or ``reset``) port shape get a
  deterministic cocotb skeleton: drive the clock, release the reset,
  pulse every other input, and assert the outputs evolve. This is
  pure Python (no router call) and fully unit-testable.
* **LLM path** — multi-module plans or unusual port shapes route to
  the model via ``router.generate(TaskType.TB_GEN, ...)``. The agent
  asks the bound model for a cocotb skeleton against the module's
  port list, then persists the response as the testbench source.

The agent is intentionally stateless: each ``generate_module`` call
produces a single artifact. The CLI / RTL stage owns caching across
repair attempts so the same testbench is re-used inside the outer
loop (and the content-addressed store dedupes on disk).
"""

from __future__ import annotations

import re
from typing import Any

from chip_agent.design_state import (
    DesignPlan,
    ModelInvocation,
    ModelRouter,
    ModuleDecl,
    Port,
    Provenance,
    Spec,
    Stage,
    TaskType,
    TestbenchArtifact,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore

__all__ = ["TestbenchGenAgent", "TestbenchGenError"]


_CLOCK_NAMES = frozenset({"clk", "clock"})
_RESET_NAMES = frozenset({"rst", "rst_n", "rstn", "reset", "reset_n"})

# Defensive strip for ```python ... ``` / bare ``` fences the LLM emits
# despite the system-prompt instruction. Mirrors rtl_gen._FENCE_RE — same
# DOTALL + non-greedy shape — but matches Python-flavoured language tags.
# Why: a fenced body lands as the testbench .py file on disk and cocotb
# fails to import it with SyntaxError, producing SIM.NO_RESULTS with no
# parseable failure for the outer loop to repair from.
_FENCE_RE = re.compile(
    r"^\s*```(?:python|py|cocotb)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Search-anywhere variant: a fenced block may follow prose. Same
# motivation as ``rtl_gen._FENCE_SEARCH_RE`` — the model occasionally
# wraps its Python in a reasoning preamble, and persisting the prose
# breaks the cocotb import.
_FENCE_SEARCH_RE = re.compile(
    r"```(?:python|py|cocotb)?\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

_TB_SYSTEM_PROMPT = """\
You generate cocotb (Python) testbenches for synthesisable Verilog modules.

Constraints:
* Output ONLY Python source — no markdown fences, no prose.
* Use ``import cocotb``, ``cocotb.start_soon``, ``cocotb.triggers``, and
  ``cocotb.clock.Clock``.
* Decorate every test with ``@cocotb.test()``.
* Drive any clock with a 10 ns period; release any reset after one cycle.
* Assert at least one observable: an output port's value evolving over a
  small number of cycles, or a primary output settling after stimulus.
* Keep the file self-contained — no external imports, no fixtures.
""".strip()


class TestbenchGenError(ValueError):
    """The agent was misconfigured or the router produced no usable text."""

    __test__ = False  # not a pytest test class despite the leading "Test"


class TestbenchGenAgent:
    """Mint a cocotb :class:`TestbenchArtifact` for one module at a time."""

    __test__ = False  # not a pytest test class despite the leading "Test"
    SYSTEM_PROMPT = _TB_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        router: ModelRouter,
        store: SqliteArtifactStore,
        design_id: str,
        agent_name: str = "tb_gen",
    ) -> None:
        if not design_id:
            raise TestbenchGenError("design_id must be non-empty")
        self.router = router
        self.store = store
        self.design_id = design_id
        self.agent_name = agent_name

    def generate_module(
        self,
        module: ModuleDecl,
        plan: DesignPlan,
        *,
        spec: Spec | None = None,
    ) -> TestbenchArtifact:
        """Generate + persist a cocotb testbench for ``module``."""
        if plan.design_id != self.design_id:
            raise TestbenchGenError(
                f"plan.design_id {plan.design_id!r} != agent.design_id "
                f"{self.design_id!r}",
            )
        if not _module_in_plan(plan, module.module_id):
            raise TestbenchGenError(
                f"module {module.module_id!r} is not in plan "
                f"{plan.artifact_id!r}",
            )

        if _use_templated_path(plan, module):
            source, invocation = _render_templated_tb(module), None
        else:
            source, invocation = self._render_llm_tb(module, plan, spec)

        return self._persist(
            module=module, source=source, invocation=invocation, plan=plan,
            spec=spec,
        )

    # ---------------------------------------------------------------- LLM path
    def _render_llm_tb(
        self,
        module: ModuleDecl,
        plan: DesignPlan,
        spec: Spec | None,
    ) -> tuple[str, ModelInvocation]:
        user_prompt = _llm_user_prompt(module=module, plan=plan, spec=spec)
        result = self.router.generate(
            TaskType.TB_GEN,
            context={
                "prompt": user_prompt,
                "system": self.SYSTEM_PROMPT,
            },
        )
        source = _strip_fences(result.chosen)
        if not source:
            raise TestbenchGenError(
                f"router returned empty cocotb body for module "
                f"{module.module_id!r}",
            )
        return source + "\n" if not source.endswith("\n") else source, result.invocation

    # ------------------------------------------------------------- persistence
    def _persist(
        self,
        *,
        module: ModuleDecl,
        source: str,
        invocation: ModelInvocation | None,
        plan: DesignPlan,
        spec: Spec | None,
    ) -> TestbenchArtifact:
        normalised = source.rstrip("\n") + "\n"
        blob = self.store.put_blob(
            normalised.encode("utf-8"), media_type="text/x-python",
        )
        inputs = [plan.ref()]
        if spec is not None:
            inputs.append(spec.ref())
        tb = TestbenchArtifact(
            artifact_id=f"{self.design_id}.{module.module_id}.tb",
            design_id=self.design_id,
            module_id=module.module_id,
            framework="cocotb",
            target_module=module.name,
            source=blob,
            provenance=Provenance(
                produced_by=Stage.RTL,
                agent=self.agent_name,
                inputs=inputs,
                model=invocation,
            ),
        )
        self.store.put(tb)
        loaded = self.store.get_by_id(tb.artifact_id)
        assert isinstance(loaded, TestbenchArtifact)
        return loaded


# --------------------------------------------------------------------------- #
# Path selection
# --------------------------------------------------------------------------- #
def _module_in_plan(plan: DesignPlan, module_id: str) -> bool:
    return any(m.module_id == module_id for m in plan.modules)


def _use_templated_path(plan: DesignPlan, module: ModuleDecl) -> bool:
    """Decide whether the deterministic skeleton fits.

    The templated path handles the common "clocked sequential module with
    a reset" shape: one clk-named input + one reset-named input + at
    least one observable output. Anything else (multi-module plans,
    unusual port shapes) falls through to the LLM router so the
    generator stays bounded but extensible.
    """
    if len(plan.modules) != 1:
        return False
    return _has_clk_and_reset(module.ports)


def _has_clk_and_reset(ports: list[Port]) -> bool:
    has_clk = any(
        p.direction == "in" and p.name.lower() in _CLOCK_NAMES for p in ports
    )
    has_rst = any(
        p.direction == "in" and p.name.lower() in _RESET_NAMES for p in ports
    )
    has_out = any(p.direction == "out" for p in ports)
    return has_clk and has_rst and has_out


def _strip_fences(text: str) -> str:
    """Extract Python testbench from a model response.

    Tries strict full-string fence first, then a search-anywhere fence
    so prose-then-fence shapes survive. There is no Python analogue of
    ``rtl_gen._MODULE_RE`` (no canonical start/end token), so a truly
    no-fence prose-with-code response falls through unchanged — cocotb's
    Python import error will surface it loudly at sim time.
    """
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group("body").strip()
    m = _FENCE_SEARCH_RE.search(stripped)
    if m:
        return m.group("body").strip()
    return stripped


# --------------------------------------------------------------------------- #
# Templated path — pure renderer
# --------------------------------------------------------------------------- #
_TB_TEMPLATE = '''\
"""F9.5 templated cocotb testbench for ``{top}``."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge


@cocotb.test()
async def test_{top}_evolves(dut):
    """Drive clk + reset, pulse other inputs, and check the outputs evolve."""
    cocotb.start_soon(Clock(dut.{clk}, 10, units="ns").start())

    # Assert reset for one cycle, then release.
    dut.{rst}.value = {rst_active}
    {pre_stim}
    await RisingEdge(dut.{clk})
    dut.{rst}.value = {rst_inactive}
    {stim}

    # Snapshot the outputs, then let the design run for a few cycles.
    initial = {{ {snapshot_pre} }}
    for _ in range(8):
        await RisingEdge(dut.{clk})
    evolved = {{ {snapshot_post} }}

    # At least one output must move; if every observable is identical to
    # its post-reset value the design isn't being driven correctly.
    assert evolved != initial, (
        f"no {top} output evolved after 8 cycles: {{evolved!r}}"
    )
'''


def _render_templated_tb(module: ModuleDecl) -> str:
    clk = _find_port(module.ports, _CLOCK_NAMES)
    rst = _find_port(module.ports, _RESET_NAMES)
    assert clk is not None
    assert rst is not None
    rst_active_low = clk.name != rst.name and rst.name.lower().endswith(
        ("_n", "n"),
    )
    rst_active = "0" if rst_active_low else "1"
    rst_inactive = "1" if rst_active_low else "0"

    other_inputs = [
        p for p in module.ports
        if p.direction == "in" and p.name != clk.name and p.name != rst.name
    ]
    outputs = [p for p in module.ports if p.direction == "out"]

    pre_stim_lines = [f"dut.{p.name}.value = 0" for p in other_inputs]
    stim_lines = [f"dut.{p.name}.value = 1" for p in other_inputs]
    pre_stim = "\n    ".join(pre_stim_lines) or "# no extra inputs to clear"
    stim = "\n    ".join(stim_lines) or "# no extra inputs to drive"

    snapshot_pre = ", ".join(
        f'"{p.name}": int(dut.{p.name}.value)' for p in outputs
    )
    snapshot_post = snapshot_pre

    return _TB_TEMPLATE.format(
        top=module.name,
        clk=clk.name,
        rst=rst.name,
        rst_active=rst_active,
        rst_inactive=rst_inactive,
        pre_stim=pre_stim,
        stim=stim,
        snapshot_pre=snapshot_pre,
        snapshot_post=snapshot_post,
    )


def _find_port(ports: list[Port], names: frozenset[str]) -> Port | None:
    for p in ports:
        if p.direction == "in" and p.name.lower() in names:
            return p
    return None


# --------------------------------------------------------------------------- #
# LLM-path prompt
# --------------------------------------------------------------------------- #
def _llm_user_prompt(
    *, module: ModuleDecl, plan: DesignPlan, spec: Spec | None,
) -> str:
    parts: list[str] = [
        f"Module: {module.name} (id={module.module_id})",
        "",
        "Port list:",
    ]
    for p in module.ports:
        width = f"[{p.width - 1}:0]" if p.width > 1 else ""
        parts.append(f"  {p.direction:5s} {width:>8s} {p.name}")
    parts.append("")
    parts.append(f"Plan top module: {plan.top_module_id}")
    parts.append(f"Plan modules: {[m.module_id for m in plan.modules]}")
    if module.description:
        parts.append("")
        parts.append(f"Description: {module.description}")
    if spec is not None and spec.normalized:
        parts.append("")
        parts.append("Source spec (normalised):")
        parts.append(spec.normalized.rstrip())
    parts.append("")
    parts.append(
        "Generate a self-contained cocotb testbench that drives every "
        "clock + reset, stimulates the inputs, and asserts the outputs "
        "evolve in a meaningful way. Output ONLY Python.",
    )
    return "\n".join(parts)


# Convenience for callers that want to peek at the system prompt without
# reaching into the class.
def _system_prompt(*, _self: Any = None) -> str:  # pragma: no cover — trivial
    return _TB_SYSTEM_PROMPT
