"""
design_state.py — typed blackboard schema for the chat->GDSII agent framework.

The design state is an APPEND-ONLY, CONTENT-ADDRESSED graph of artifacts.

  * Every Artifact is immutable once written and identified by `content_hash`
    (a hash of its content only, not its provenance/timestamps — so identical
    content dedupes and reproductions match).
  * Edges are provenance: each artifact records the artifacts it was derived
    from (`provenance.inputs`) plus the step / tool / model / config that
    produced it. Walking inputs backward from any artifact yields a DAG rooted
    at the Spec — that is both the audit trail and the reproduction recipe.
  * DesignState / ModuleState hold HEAD POINTERS to the current accepted
    artifact at each stage; the ArtifactStore keeps every version.
  * Verification artifacts expose a uniform `passed` / `metrics` / `violations`
    shape (and `gate_ok`) so the control graph can read gate predicates without
    knowing artifact internals.

Pydantic v2. This is the schema only; the store implementation and the control
graph (pass 2) consume it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, computed_field

if TYPE_CHECKING:  # avoid a circular import with chip_agent.routing.gateway
    from chip_agent.routing.gateway import StreamChunk


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Stage(StrEnum):
    """Macro pipeline stages — the keys for head pointers and gating."""
    SPEC = "spec"
    PLAN = "plan"
    # F19.7: M19 Phase 1 nodes inserted between PLAN and RTL. Each
    # is per-module (lives in ``ModuleState.stages``), single-shot
    # (no repair budget), and the verification gate is warning-only
    # (gate_ok is recorded but does not block downstream RTL).
    CONTRACT = "contract"
    ORACLE = "oracle"
    ASSERTIONS = "assertions"
    ORACLE_VERIFICATION = "oracle_verification"
    RTL = "rtl"          # generation + lint + simulation inner loop
    SYNTH = "synth"      # logic synthesis -> gate-level netlist
    PHYSICAL = "physical"  # floorplan -> place -> cts -> route
    SIGNOFF = "signoff"  # STA + DRC + LVS + security
    GDSII = "gdsii"


class ArtifactKind(StrEnum):
    """Concrete node types in the artifact graph."""
    SPEC = "spec"
    PLAN = "plan"
    # F19.1: structured behavioural contract derived from spec + ModuleDecl,
    # consumed by F19.4 OracleGenAgent + F19.5 AssertionGenAgent for the
    # test-first / oracle-driven RTL workflow (M19).
    CONTRACT = "contract"
    # F19.2: derived from CONTRACT; consumed by F19.6 triangulation gate.
    # ORACLE = Python reference model emitted by F19.4 OracleGenAgent.
    # ASSERTIONS = callable assertions over (stim, observed) emitted by
    # F19.5 AssertionGenAgent. They're parallel siblings produced by
    # independent router calls so disagreement between them is real
    # evidence the spec was misread (the F19.6 triangulation premise).
    ORACLE = "oracle"
    ASSERTIONS = "assertions"
    RTL = "rtl"
    TESTBENCH = "testbench"
    LINT = "lint"
    SIM = "sim"
    NETLIST = "netlist"
    SYNTH_REPORT = "synth_report"
    LAYOUT = "layout"
    STA = "sta"
    # F21.2: per-corner timing analysis (tt/ss/ff) + report_power.
    # Produced by SIGNOFF when SignoffSettings.sta_corners is set and
    # LibreLane harvested per-corner reports onto LayoutArtifact.
    # Worst-corner WNS feeds the SIGNOFF gate; F21.3's timing-closure
    # agent reads the per-corner vectors to pick the right repair knob.
    MULTI_CORNER_STA = "multi_corner_sta"
    DRC = "drc"
    LVS = "lvs"
    SECURITY = "security"
    DIAGNOSIS = "diagnosis"
    # F19.6: triangulation gate verdict — did the F19.5 AssertionSpec
    # assertions agree with the F19.4 OracleArtifact's reference fn on
    # a given stimulus? Phase 1 is warning-only; F19.10 promotes the
    # gate to a hard block.
    ORACLE_VERIFICATION = "oracle_verification"
    # F20.1: persisted per-outer-loop-attempt rationale. Each attempt
    # produces one RepairAttempt artifact carrying the model's
    # hypothesis for that attempt; the next outer-loop iteration
    # consumes the last 1-2 rationales as prompt context so the
    # model can avoid repeating the same wrong hypothesis.
    REPAIR_ATTEMPT = "repair_attempt"
    # F23.1: operator guidance captured at an interactive HUMAN-escalation
    # turn, distilled from a free-text chat into a typed hint that re-seeds
    # a bounded repair retry. The raw transcript lives in provenance/trace,
    # NEVER in the content hash (see HumanHint._NON_CONTENT_FIELDS).
    HUMAN_HINT = "human_hint"
    GDSII = "gdsii"


class ArtifactStatus(StrEnum):
    DRAFT = "draft"            # produced, not yet verified
    VERIFIED = "verified"      # passed its stage gate
    ACCEPTED = "accepted"      # promoted to head
    REJECTED = "rejected"      # failed gate
    SUPERSEDED = "superseded"  # replaced by a newer version


class StageStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"
    BLOCKED = "blocked"


class DesignStatus(StrEnum):
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETED = "completed"
    FAILED = "failed"


class EscalationLevel(StrEnum):
    """The repair-loop slot. INNER < OUTER < EXHAUSTED < HUMAN. The MODEL bound
    to each loop is user config (see model_routing.md), so the level names the
    loop, not a model tier.

    F12.5 added the ``EXHAUSTED`` rung between ``OUTER`` and ``HUMAN``: when the
    outer loop's repair budget burns through without convergence, the controller
    fires one more attempt at this rung, which the policy resolver routes to
    the frontier model bound to ``tasks.plan`` (per CLAUDE.md's "frontier for
    semantic reasoning" line). Only if that final attempt also fails do we
    escalate to ``HUMAN``.
    """
    INNER = "inner"   # syntactic repair loop  -> loops.inner.model
    OUTER = "outer"   # semantic repair loop   -> loops.outer.model
    EXHAUSTED = "exhausted"   # F12.5: one frontier attempt before HUMAN
    HUMAN = "human"   # surface to a person     (no model call)

    @property
    def _rank(self) -> int:
        return {"inner": 0, "outer": 1, "exhausted": 2, "human": 3}[self.value]

    # Ordered comparisons (override str's), so `lvl < EscalationLevel.OUTER` and max() work.
    def __lt__(self, other: EscalationLevel) -> bool: return self._rank < other._rank  # type: ignore[override]
    def __le__(self, other: EscalationLevel) -> bool: return self._rank <= other._rank  # type: ignore[override]
    def __gt__(self, other: EscalationLevel) -> bool: return self._rank > other._rank  # type: ignore[override]
    def __ge__(self, other: EscalationLevel) -> bool: return self._rank >= other._rank  # type: ignore[override]

    def escalated(self) -> EscalationLevel:
        """Next level up; HUMAN is terminal."""
        return {EscalationLevel.INNER: EscalationLevel.OUTER,
                EscalationLevel.OUTER: EscalationLevel.EXHAUSTED,
                EscalationLevel.EXHAUSTED: EscalationLevel.HUMAN,
                EscalationLevel.HUMAN: EscalationLevel.HUMAN}[self]


class Transition(StrEnum):
    """Output of the control graph's gate decision (see control_graph.md)."""
    ADVANCE = "advance"
    RETRY = "retry"
    ESCALATE = "escalate"
    FEEDBACK_UPSTREAM = "feedback_upstream"
    HUMAN = "human"
    FAIL = "fail"


class TaskType(StrEnum):
    """What an agent is asked to do; the router maps this to a model (see model_routing.md)."""
    SPEC_INTAKE = "spec_intake"
    PLAN = "plan"
    # F19.3: extract a structured behavioural contract (ContractArtifact)
    # from Spec + ModuleDecl. Single-shot router call, JSON output. The
    # test-first / oracle-driven workflow (M19) hangs off this task —
    # F19.4 OracleGen and F19.5 AssertionGen both consume what this
    # produces.
    CONTRACT_EXTRACTION = "contract_extraction"
    # F19.4: generate a Python reference model from a ContractArtifact.
    # Independent router call (no shared context with F19.5
    # AssertionGen) — the F19.6 triangulation gate compares the two
    # independently-derived artifacts.
    ORACLE_GEN = "oracle_gen"
    # F19.5: three-stage AssertLLM-style assertion generation. Each
    # stage independently bindable so cheap structural tasks can route
    # to a smaller model while the codegen step uses the frontier
    # model. Triangulation independence from F19.4 OracleGen is the
    # whole point: distinct class, distinct tasks, distinct context.
    ASSERTION_EXTRACT = "assertion_extract"
    ASSERTION_MAP = "assertion_map"
    ASSERTION_TRANSLATE = "assertion_translate"
    RTL_GEN = "rtl_gen"
    RTL_REPAIR = "rtl_repair"
    TB_GEN = "tb_gen"
    DIAGNOSE = "diagnose"
    CONFIG_TUNE = "config_tune"
    SCORE = "score"
    NL_SUMMARIZE = "nl_summarize"
    SECURITY_REVIEW = "security_review"
    # F19.9: single-shot frontier classification step. Reads the RTL
    # outer-loop FailureDiagnosis + the per-module M19 lineage
    # (CONTRACT / ORACLE / ASSERTIONS / RTL refs) and returns a typed
    # ``ReflectionRoute`` picking among the four recovery routes.
    REFLECTION_ROUTING = "reflection_routing"
    # F20.1: extract a one-paragraph rationale ("what hypothesis did
    # you act on, what would you try next?") for each RTL outer-loop
    # repair attempt. The rationale is persisted on a RepairAttempt
    # artifact and inlined into the next attempt's prompt so the
    # model can avoid repeating its own past hypotheses.
    REPAIR_RATIONALE = "repair_rationale"
    # F19.4b: first stage of the F19.4 OracleGenAgent's two-stage
    # split. The model emits a JSON "plan" describing the module's
    # per-clock semantics + a hand-walked worked_example (4-6 cycles).
    # The plan feeds the existing F19.4 code-gen stage; the
    # worked_example drives a subprocess-based self-consistency check
    # that catches "syntactically valid but semantically wrong"
    # Python BEFORE the oracle is persisted (Faver arXiv 2510.08664,
    # ChatModel arXiv 2506.15066).
    ORACLE_PLAN = "oracle_plan"
    # F21.3: single-shot frontier classification step for the SIGNOFF
    # timing-failure recovery path. Reads the F21.2 MultiCornerSTAReport
    # (or single-corner TimingReport) + the current PhysicalConfig +
    # the route history and returns a typed ``PhysicalRepairRoute``
    # picking among LOWER_DENSITY / INCREASE_DELAY_OPTIMIZATION /
    # RELAX_CLOCK_PERIOD / ESCALATE_HUMAN.
    PHYSICAL_REPAIR_ROUTING = "physical_repair_routing"
    # F23.2: single-shot frontier classification step for the interactive
    # HUMAN-escalation turn. Reads the operator's free-text chat + the
    # FailureDiagnosis and returns a typed ``HumanHint`` (hint_kind +
    # model-facing summary + optional suggested route). Frontier-bound in
    # shipped configs; never sets a gate — it only seeds the next bounded
    # repair attempt.
    HUMAN_HINT_DISTILL = "human_hint_distill"


# --------------------------------------------------------------------------- #
# Provenance primitives
# --------------------------------------------------------------------------- #
class BlobRef(BaseModel):
    """Pointer to file-backed content (RTL, netlist, DEF, GDSII, waveforms)."""
    path: str                       # path within the content-addressed run store
    sha256: str                     # hash of the file bytes
    size_bytes: int
    media_type: str = "text/plain"  # e.g. text/x-verilog, application/octet-stream


class ToolVersion(BaseModel):
    """Pin a deterministic EDA tool invocation for reproducibility."""
    name: str                              # "yosys", "openroad", "verilator", "magic"
    version: str
    container_digest: str | None = None  # Docker image digest, the real repro anchor


class ModelInvocation(BaseModel):
    """Record the LLM call that produced an artifact, if any."""
    provider: str                  # "anthropic", "openai", "local-vllm"
    model: str
    temperature: float = 0.0
    top_p: float | None = None
    seed: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None


class ArtifactRef(BaseModel):
    """A lightweight typed edge/pointer — used in provenance and head pointers
    so we reference artifacts without embedding their content."""
    artifact_id: str   # stable logical id, shared across versions (e.g. "alu.rtl")
    version: int
    kind: ArtifactKind
    content_hash: str


class Provenance(BaseModel):
    """How an artifact came to be. `inputs` form the DAG edges."""
    produced_by: Stage
    agent: str | None = None              # which specialist agent
    tool: ToolVersion | None = None
    model: ModelInvocation | None = None
    inputs: list[ArtifactRef] = Field(default_factory=list)
    config_hash: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: str | None = None


class Violation(BaseModel):
    """One finding from any checker, in a uniform shape."""
    code: str                                       # "LATCH_INFERRED", "DRC.M1.SP", ...
    severity: str = "error"                         # error | warning | info
    message: str
    location: str | None = None                  # file:line, instance, net, path id
    detail: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Spec / plan payload structures
# --------------------------------------------------------------------------- #
class Port(BaseModel):
    name: str
    direction: str       # in | out | inout
    width: int = 1
    description: str | None = None


class DesignConstraints(BaseModel):
    pdk: str = "gf180mcuD"
    std_cell_lib: str = "gf180mcu_fd_sc_mcu7t5v0"
    target_clock_ns: float | None = None
    max_die_area_um2: float | None = None
    target_utilization: float | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class ModuleDecl(BaseModel):
    """One module in the plan: its interface contract and dependencies."""
    module_id: str
    name: str
    description: str
    ports: list[Port] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)  # submodule_ids


# --------------------------------------------------------------------------- #
# F19.1 — sub-models for ContractArtifact (the test-first workflow's
# structured intermediate between freeform spec text and the Python
# reference / cocotb assertions that F19.4 / F19.5 emit).
# --------------------------------------------------------------------------- #
class BehaviorInvariant(BaseModel):
    """One behavioural rule the contract asserts about the module.

    ``condition`` is intentionally an NL string, NOT a Python callable.
    F19.5's AssertionGenAgent translates it into a cocotb assertion
    callsite independently of F19.4's OracleGenAgent, so the two
    generators triangulate on the spec without sharing executable state.
    """
    name: str
    description: str
    condition: str


class PortAssumption(BaseModel):
    """Per-port assumption the contract carries (polarity, range, encoding).

    Defaults are explicit ``"n/a"`` rather than ``None`` so the contract
    surface is unambiguous: a missing assumption reads as "no constraint",
    not "the agent forgot to fill this in".
    """
    port_name: str
    polarity: Literal["positive", "negative", "n/a"] = "n/a"
    expected_range: tuple[int, int] | None = None
    encoding: Literal["binary", "one_hot", "gray", "twos_complement", "n/a"] = "n/a"
    notes: str = ""


class ClockDomain(BaseModel):
    """One clock domain. Most modules have exactly one."""
    name: str
    frequency_mhz: float | None = None
    source: str | None = None
    notes: str = ""


class ResetSpec(BaseModel):
    """Reset semantic for this module, decoupled from PortAssumption so
    cross-cutting info (synchronicity, affected signals) has a home."""
    name: str
    polarity: Literal["active_high", "active_low", "none"]
    synchronicity: Literal["sync", "async", "none"]
    affects: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Artifact base
# --------------------------------------------------------------------------- #
class Artifact(BaseModel):
    """Immutable node in the design graph."""
    model_config = ConfigDict(frozen=False)  # frozen post-write by store convention

    artifact_id: str                 # stable logical id, shared across versions
    version: int = 1
    kind: ArtifactKind
    design_id: str
    module_id: str | None = None  # None for design-level artifacts (spec, plan)
    content_hash: str = ""           # set by the store on put(); identity of content
    status: ArtifactStatus = ArtifactStatus.DRAFT
    provenance: Provenance
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Fields that are NOT part of content identity (logical id / provenance / lifecycle
    # vary independently of payload). artifact_id is the logical name shared across
    # versions, not data, so two artifacts with the same payload under different ids
    # share a content hash and dedupe on disk; the store overlays the row's id at read.
    _NON_CONTENT_FIELDS = {
        "artifact_id", "content_hash", "provenance", "created_at",
        "status", "version", "metadata",
    }

    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self.artifact_id,
            version=self.version,
            kind=self.kind,
            content_hash=self.content_hash,
        )

    def compute_content_hash(self) -> str:
        """Hash content fields only, so identical content dedupes across runs."""
        payload = self.model_dump(mode="json", exclude=self._NON_CONTENT_FIELDS)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Design-level artifacts
# --------------------------------------------------------------------------- #
class Spec(Artifact):
    kind: ArtifactKind = ArtifactKind.SPEC
    raw_text: str
    normalized: str
    requirements: list[str] = Field(default_factory=list)
    constraints: DesignConstraints = Field(default_factory=DesignConstraints)


class DesignPlan(Artifact):
    kind: ArtifactKind = ArtifactKind.PLAN
    top_module_id: str
    modules: list[ModuleDecl] = Field(default_factory=list)
    rationale: str | None = None


class ContractArtifact(Artifact):
    """F19.1 — structured behavioural contract derived from spec + ModuleDecl.

    Sits between PLAN and RTL in the test-first / oracle-driven workflow
    (M19). Read by F19.4 OracleGenAgent and F19.5 AssertionGenAgent — two
    independent router calls so disagreement between the resulting oracle
    and assertions is real evidence the spec was misread (the F19.6
    triangulation gate). Every field is part of the contract's identity:
    two contracts with the same invariants and assumptions ARE the same
    contract regardless of when they were produced, so no
    ``_NON_CONTENT_FIELDS`` override is needed beyond the parent
    Artifact's lifecycle exclusions.
    """
    kind: ArtifactKind = ArtifactKind.CONTRACT
    behavior_invariants: list[BehaviorInvariant] = Field(default_factory=list)
    port_assumptions: list[PortAssumption] = Field(default_factory=list)
    clock_domains: list[ClockDomain] = Field(default_factory=list)
    reset: ResetSpec | None = None
    # Flexible bag for FSM encodings, signedness, "is_pipelined", etc.
    # Unstructured because the useful keys grow with the design corpus;
    # F19.5 reads it opportunistically.
    encoding: dict[str, str] = Field(default_factory=dict)
    # Explicit "spec didn't say X, I assumed Y" notes from F19.3. F19.6's
    # triangulation gate surfaces these when oracle and assertions
    # disagree — the disagreement usually traces to a specific assumption.
    ambiguity_notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# F19.2 — OracleArtifact + AssertionSpec: the executable / introspectable
# counterparts to ContractArtifact. F19.4 OracleGenAgent and F19.5
# AssertionGenAgent emit them via independent router calls so disagreement
# between the resulting reference model and assertions is real evidence
# of misread spec (the F19.6 triangulation gate). Both carry Python
# source as a ``BlobRef`` (``media_type="text/x-python"``, same idiom as
# TestbenchArtifact); F19.6 writes it to disk + exec's it in a sandboxed
# subprocess.
# --------------------------------------------------------------------------- #
class StructuredInvariant(BaseModel):
    """One assertion in an AssertionSpec.

    Names a callsite within ``AssertionSpec.source``. F19.6's triangulation
    runner exec's that source and dispatches via
    ``getattr(module, callsite)((stim, observed)) -> (passed, message)``.
    ``name`` carries identity — F19.6 names it in the
    ``ORACLE.ASSERTION_DISAGREEMENT`` violation on failure, so it must
    match the corresponding ``BehaviorInvariant.name`` in the source
    contract for the diagnosis to be traceable end-to-end. ``callsite``
    is separate so F19.5's translator stage can use snake_case Python
    identifiers without smuggling them into the contract's NL-shaped names.
    """
    name: str
    callsite: str
    description: str = ""


class OracleArtifact(Artifact):
    """F19.2 — Python reference model derived from a ContractArtifact.

    F19.4 OracleGenAgent emits this as the executable counterpart to F19.5's
    AssertionSpec. F19.6's triangulation gate writes ``source`` to disk,
    imports it, and calls ``getattr(module, reference_fn_name)`` with a
    stimulus sequence to compute the expected output sequence.

    ``rationale_notes`` is excluded from the content hash so two oracles
    with identical source/signature dedupe even if their generators
    labelled the resolution differently. The causal link to the
    originating contract lives in ``provenance.inputs`` (an ArtifactRef
    to the ContractArtifact), not in this field.
    """
    kind: ArtifactKind = ArtifactKind.ORACLE
    source: BlobRef
    module_signature: list[Port] = Field(default_factory=list)
    reference_fn_name: str = "reference"
    rationale_notes: list[str] = Field(default_factory=list)

    # Mirror Artifact._NON_CONTENT_FIELDS verbatim (Pydantic v2 wraps the
    # parent's underscore-prefixed attribute as a ModelPrivateAttr, so
    # ``Artifact._NON_CONTENT_FIELDS | {...}`` doesn't compose) and add
    # ``rationale_notes``. Same idiom as LayoutArtifact / LVSReport.
    _NON_CONTENT_FIELDS = {
        "artifact_id", "content_hash", "provenance", "created_at",
        "status", "version", "metadata", "rationale_notes",
    }


class AssertionSpec(Artifact):
    """F19.2 — set of callable assertions derived from a ContractArtifact.

    F19.5 AssertionGenAgent produces it via three sequential router calls
    (AssertLLM-style: extract -> map -> translate). F19.6's triangulation
    runner exec's ``source``, then for each ``StructuredInvariant`` calls
    ``getattr(module, inv.callsite)((stim, observed)) -> (bool, str)``.

    A useful AssertionSpec carries >=1 invariant (per F19.2 AC), but the
    field default is the empty list so a partial spec is still
    serialisable — that validation lives in F19.5, not the schema.

    ``rationale_notes`` excluded from the hash for the same reason as
    OracleArtifact.
    """
    kind: ArtifactKind = ArtifactKind.ASSERTIONS
    source: BlobRef
    assertions: list[StructuredInvariant] = Field(default_factory=list)
    rationale_notes: list[str] = Field(default_factory=list)

    _NON_CONTENT_FIELDS = {
        "artifact_id", "content_hash", "provenance", "created_at",
        "status", "version", "metadata", "rationale_notes",
    }


# --------------------------------------------------------------------------- #
# Module-level content artifacts (head-bearing)
# --------------------------------------------------------------------------- #
class RTLArtifact(Artifact):
    kind: ArtifactKind = ArtifactKind.RTL
    source: BlobRef
    top_module: str
    language: str = "verilog"        # verilog | systemverilog
    synthesizable: bool = True
    submodule_ids: list[str] = Field(default_factory=list)


class TestbenchArtifact(Artifact):
    __test__ = False  # not a pytest test class despite the leading "Test"
    kind: ArtifactKind = ArtifactKind.TESTBENCH
    source: BlobRef
    framework: str = "cocotb"        # cocotb | sv
    target_module: str


class NetlistArtifact(Artifact):
    kind: ArtifactKind = ArtifactKind.NETLIST
    netlist: BlobRef
    std_cell_lib: str
    cell_count: int = 0
    area_um2: float | None = None


class LayoutArtifact(Artifact):
    kind: ArtifactKind = ArtifactKind.LAYOUT
    def_file: BlobRef
    stage_reached: str = "routed"    # placed | cts | routed
    die_area_um2: float | None = None
    utilization_pct: float | None = None
    cell_count: int = 0                # F12.4: harvested from LibreLane metrics
    congestion_pct: float | None = None  # F12.4: same source
    # F12.1: SDF dumped by LibreLane's STA step during CTS. When present,
    # SIGNOFF re-runs OpenSTA over this SDF instead of synthesising a
    # minimal SDC from the clock period alone, so the gate matches what
    # LibreLane saw post-CTS. ``None`` when the harvest pass didn't find
    # one (older flow versions, pre-routed layouts).
    librelane_sdf: BlobRef | None = None
    # F12.2: Magic DRC report dumped by LibreLane's ``route`` step. When
    # present, SIGNOFF's DRC leg parses it alongside its own re-run and
    # emits a ``LIBRELANE_DRC_DISAGREES`` violation when the two
    # disagree. Disagreement is informational; the re-run is the binding
    # gate.
    librelane_drc_report: BlobRef | None = None
    # F12.3: SPICE netlist extracted by LibreLane's ``extract`` step.
    # SIGNOFF's LVS leg compares the synth netlist against this extracted
    # netlist. When present it's the authoritative ``layout_netlist_bytes``
    # source; SIGNOFF falls back to the explicit ``layout_netlist_bytes``
    # arg only when this field is ``None``. Both missing = SignoffStageError.
    librelane_layout_spice: BlobRef | None = None
    # F13.1: final post-synth Verilog netlist with sky130 standard cells
    # mapped in. SIGNOFF's STA + LVS legs prefer this over the F6.x
    # intermediate Yosys netlist on ``NetlistArtifact`` because OpenSTA's
    # ``read_verilog`` chokes on Yosys's mixed reg/wire/always output, and
    # Netgen LVS can't match Yosys's generic gates against the extracted
    # SPICE (different abstraction levels). When ``None``, both legs fall
    # back to ``NetlistArtifact.netlist`` (the F6.x path; useful for the
    # stub flow where LibreLane never ran).
    librelane_mapped_netlist: BlobRef | None = None
    # F13.4-B: powered netlist (PNL). Same module body as the mapped
    # netlist but ports + cells carry the power pins (VPWR/VGND) + bit-
    # blasted buses so the shape matches LibreLane's extracted SPICE.
    # SIGNOFF feeds this — NOT the unpowered mapped netlist — to LVS so
    # Netgen sees matching port lists on both sides; STA continues to
    # read the unpowered mapped netlist where power pins are noise.
    # When ``None``, LVS falls back to ``librelane_mapped_netlist`` so
    # older harvest passes still produce some verdict.
    librelane_powered_netlist: BlobRef | None = None

    # F21.2: per-corner OpenSTA reports harvested from LibreLane's
    # ``runs/<tag>/final/timing/<corner>/sta.rpt`` (or equivalent) when
    # ``STA_CORNERS`` is set on the flow config. Keys are corner tags
    # (``tt``/``ss``/``ff`` by convention); values are BlobRefs to the
    # raw report bytes. When non-empty, SIGNOFF builds a
    # ``MultiCornerSTAReport`` from the parsed reports in place of
    # today's typical-corner-only ``TimingReport``. When ``None`` (the
    # default), today's single-corner path is byte-identical.
    librelane_per_corner_timing: dict[str, BlobRef] | None = None
    # F21.2: per-corner ``report_power`` outputs, harvested alongside
    # the timing reports when ``STA_REPORT_POWER`` is set. Same key
    # convention as ``librelane_per_corner_timing``. When ``None``,
    # the per-corner ``CornerTiming.power_metrics`` stays empty (no
    # power signal); F21.3's tuner can still reason over slack alone.
    librelane_per_corner_power: dict[str, BlobRef] | None = None

    # F18: raw LibreLane stdout+stderr captured from the sandbox run, so a
    # silent flow failure (e.g. config rejected at parse time, container
    # OOM, missing PDK) leaves a readable diagnostic trail. The driver
    # discards the in-memory PhysicalRun on escalation; without this blob
    # the only signal users got was an empty DEF with stage_reached=
    # 'unknown'. Set whenever the sandbox produced any stdout or stderr,
    # else ``None`` so the audit clearly shows "no output captured". The
    # log is a transcript of *how* the flow ran (wall-clock timestamps,
    # log-level prefixes, etc.) — two reproductions of the same design
    # produce different bytes here. Excluded from the content hash below
    # so identity stays defined by DEF + metrics + the PDK outputs.
    librelane_log: BlobRef | None = None

    # Diagnostic log varies between executions of the same inputs; it
    # is NOT part of the layout's design identity, so don't let it
    # drift the content hash (which would break golden-manifest
    # reproducibility tests and bloat the content-addressed store).
    # Mirror Artifact._NON_CONTENT_FIELDS verbatim (Pydantic v2 wraps the
    # parent's underscore-prefixed attribute as a ModelPrivateAttr, so
    # ``Artifact._NON_CONTENT_FIELDS | {...}`` doesn't compose) and add
    # the log field.
    _NON_CONTENT_FIELDS = {
        "artifact_id", "content_hash", "provenance", "created_at",
        "status", "version", "metadata", "librelane_log",
        # F21.2: per-corner timing/power reports are analysis OUTPUT
        # over the sealed layout, not part of the layout's design
        # identity. The canonical hash for that analysis lives on
        # ``MultiCornerSTAReport`` (which IS in the content-addressed
        # store). Excluding here keeps demo goldens byte-identical
        # across schema bumps (today's path keeps these fields None;
        # F21.2-E populates them when ``sta_corners`` is set).
        "librelane_per_corner_timing", "librelane_per_corner_power",
    }


class GDSIIArtifact(Artifact):
    kind: ArtifactKind = ArtifactKind.GDSII
    gds: BlobRef
    die_area_um2: float | None = None
    cell_count: int = 0


# --------------------------------------------------------------------------- #
# Verification artifacts (gate inputs) — uniform passed/metrics/violations
# --------------------------------------------------------------------------- #
class VerificationArtifact(Artifact):
    """Base for anything the control graph gates on."""
    passed: bool = False
    checker: ToolVersion | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    violations: list[Violation] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gate_ok(self) -> bool:
        """The single predicate the control graph reads."""
        return self.passed and not any(v.severity == "error" for v in self.violations)


class LintResult(VerificationArtifact):
    kind: ArtifactKind = ArtifactKind.LINT


class SimulationResult(VerificationArtifact):
    kind: ArtifactKind = ArtifactKind.SIM
    tests_total: int = 0
    tests_passed: int = 0
    coverage_pct: float | None = None
    waveform: BlobRef | None = None
    failing_assertions: list[str] = Field(default_factory=list)


class SynthesisReport(VerificationArtifact):
    kind: ArtifactKind = ArtifactKind.SYNTH_REPORT
    cell_count: int = 0
    area_um2: float | None = None
    inferred_latches: int = 0
    longest_path_ns: float | None = None


class TimingReport(VerificationArtifact):
    kind: ArtifactKind = ArtifactKind.STA
    wns_ns: float | None = None      # worst negative slack
    tns_ns: float | None = None      # total negative slack
    setup_violations: int = 0
    hold_violations: int = 0


class CornerTiming(BaseModel):
    """F21.2 — per-corner timing + power slice.

    One entry per process corner (typically ``tt``/``ss``/``ff``) inside a
    ``MultiCornerSTAReport``. ``wns_ns``/``tns_ns`` are ``None`` only when
    the parser couldn't extract them — distinct from a non-violating 0.0
    (clean corner). ``power_metrics`` is empty when ``sta_report_power``
    was off; keys are ``leakage``/``switching``/``internal``/``total`` in mW.
    """
    model_config = ConfigDict(frozen=True)

    corner: str
    wns_ns: float | None = None
    tns_ns: float | None = None
    setup_violations: int = 0
    hold_violations: int = 0
    power_metrics: dict[str, float] = Field(default_factory=dict)


class MultiCornerSTAReport(VerificationArtifact):
    """F21.2 — per-corner timing across tt/ss/ff (plus optional power).

    Produced by SIGNOFF in place of ``TimingReport`` when LibreLane ran
    with ``STA_CORNERS`` set and emitted per-corner reports. ``passed``
    is the worst-corner conjunction (all corners' WNS must be ≥0); the
    inherited ``gate_ok`` adds the no-error-violations requirement.
    F21.3 reads ``worst_wns_ns`` / per-corner vectors to pick repair knobs.
    """
    kind: ArtifactKind = ArtifactKind.MULTI_CORNER_STA
    corners: list[CornerTiming] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def worst_wns_ns(self) -> float | None:
        """Min WNS across corners with a parsed value; ``None`` if none parsed."""
        parsed = [c.wns_ns for c in self.corners if c.wns_ns is not None]
        return min(parsed) if parsed else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def worst_tns_ns(self) -> float | None:
        """Min TNS (most negative) across corners with a parsed value."""
        parsed = [c.tns_ns for c in self.corners if c.tns_ns is not None]
        return min(parsed) if parsed else None


class DRCReport(VerificationArtifact):
    kind: ArtifactKind = ArtifactKind.DRC
    violation_count: int = 0


class LVSReport(VerificationArtifact):
    kind: ArtifactKind = ArtifactKind.LVS
    matched: bool = False
    mismatch_count: int = 0

    # F18-mirror: raw Netgen stdout+stderr (+ report when present)
    # captured from the sandbox run. ``LVS.UNKNOWN`` failures are
    # silent by construction — Netgen exits before emitting a verdict,
    # so the typed parse result tells the user nothing about WHY. The
    # sidecar blob lets a post-mortem read what Netgen actually said
    # without re-executing the flow. Set whenever the sandbox produced
    # any output, else ``None``. Excluded from the content hash below
    # because the log varies between executions of the same inputs and
    # must not drift identity (would break golden-manifest tests).
    netgen_log: BlobRef | None = None

    _NON_CONTENT_FIELDS = {
        "artifact_id", "content_hash", "provenance", "created_at",
        "status", "version", "metadata", "netgen_log",
    }


class SecurityReport(VerificationArtifact):
    kind: ArtifactKind = ArtifactKind.SECURITY
    checks_run: list[str] = Field(default_factory=list)
    suspicious_structures: int = 0


class OracleVerificationArtifact(VerificationArtifact):
    """F19.6 — triangulation gate verdict: did the F19.5 assertions
    agree with the F19.4 oracle on the same stimulus?

    Inherits ``passed``, ``violations``, ``metrics``, ``checker``,
    plus the computed ``gate_ok`` property from VerificationArtifact.
    Disagreements are recorded as ``Violation(code=
    "ORACLE.ASSERTION_DISAGREEMENT", severity="error")``, each naming
    the failing assertion in ``location`` and carrying the assertion's
    returned message (or exception traceback) in ``detail``.

    Every field is part of the verification's identity: two runs of
    the same ``(oracle, assertion_spec, stim)`` produce the same
    artifact body, so no ``_NON_CONTENT_FIELDS`` override is needed
    beyond the parent ``Artifact`` lifecycle exclusions.

    In Phase 1 of M19 the gate is informational: ``gate_ok=False`` is
    a warning, not a block. F19.10 promotes it to a hard gate by
    wiring its verdict into the standard escalation chain.
    """
    kind: ArtifactKind = ArtifactKind.ORACLE_VERIFICATION
    oracle_ref: ArtifactRef
    assertion_spec_ref: ArtifactRef
    stim_cycles: int = 0
    assertions_checked: int = 0
    assertions_passed: int = 0


# --------------------------------------------------------------------------- #
# Diagnosis (semantic-failure understanding) — see control_graph.md / agents.md
# --------------------------------------------------------------------------- #
class TraceFacts(BaseModel):
    """Deterministic facts extracted from a waveform/AST by the model-free `trace` tool."""
    failing_signal: str | None = None
    cycle: int | None = None
    expected: str | None = None
    actual: str | None = None


class FailureDiagnosis(Artifact):
    """TraceFacts + a model-produced cause hypothesis. The outer loop re-prompts on THIS,
    never on raw simulator stdout. Also used to attribute cross-stage feedback."""
    kind: ArtifactKind = ArtifactKind.DIAGNOSIS
    failing_signal: str | None = None
    cycle: int | None = None
    expected: str | None = None
    actual: str | None = None
    suspected_cause: str | None = None       # model-produced
    nl_summary: str = ""                         # what the model reasons over
    target_stage: Stage | None = None         # set when used for cross-stage feedback
    target_module: str | None = None
    # F20.6 — diagnosis enrichment. Each is a deterministic derivation
    # from (sim.failing_assertions, sim.waveform, testbench.source);
    # two diagnoses for the same triplet hash equal so they stay IN
    # the content hash (no _NON_CONTENT_FIELDS override). Empty / empty
    # dict when the enrichment inputs were not available (graceful
    # fallback — the outer-loop prompt simply omits the corresponding
    # section).
    test_source: str = ""
    window_vcd_summary: str = ""
    active_signals_at_failure_cycle: dict[str, str] = Field(default_factory=dict)


class RepairAttempt(Artifact):
    """F20.1: persisted per-outer-loop-attempt rationale.

    Each RTL outer-loop attempt produces one of these. ``rationale``
    is the model's one-paragraph reflection on the hypothesis it
    acted on for THIS attempt, and what it would try next if the
    repair also fails. The next outer-loop iteration reads the last
    1-2 ``rationale`` strings out of the local list the driver
    maintains and inlines them into the next prompt so the model can
    avoid re-trying its own failed hypotheses.

    ``rationale`` IS part of the content hash (no
    ``_NON_CONTENT_FIELDS`` override) — two attempts with identical
    diagnoses + RTL refs but different rationale text are genuinely
    different artifacts (the rationale carries the model's reasoning
    that the next attempt consumes).
    """
    kind: ArtifactKind = ArtifactKind.REPAIR_ATTEMPT
    attempt_index: int                # 1, 2, 3, ...
    previous_rtl_ref: ArtifactRef
    new_rtl_ref: ArtifactRef
    diagnosis_ref: ArtifactRef
    rationale: str


# --------------------------------------------------------------------------- #
# F19.9 — Adaptive reflection routing (Spec2RTL-Agent-style)
#
# When the RTL outer loop exhausts its repair budget, F19.9's
# ``ReflectionRoutingAgent`` runs one frontier-model classification step
# over the FailureDiagnosis + the per-module M19 lineage and picks one
# of four targeted recovery routes. The state-graph dispatcher reads
# the returned ``ReflectionRoute`` and either re-enters CONTRACT
# (regenerate upstream artifacts), re-runs a sibling module's RTL,
# regenerates the current module's RTL one more time, or escalates to
# the human gate.
# --------------------------------------------------------------------------- #
class ReflectionRouteKind(StrEnum):
    """Discrete recovery routes the F19.9 dispatcher can pick.

    The router model picks exactly one. Each route is implemented by
    the RTL graph node clearing the relevant per-module heads and
    re-entering the appropriate upstream node (or human gate).
    """
    RE_EXTRACT_CONTRACT = "re_extract_contract"
    REVISIT_SIBLING_RTL = "revisit_sibling_rtl"
    REGEN_CURRENT_RTL = "regen_current_rtl"
    ESCALATE_HUMAN = "escalate_human"


class ReflectionRoute(BaseModel):
    """Typed verdict produced by :class:`ReflectionRoutingAgent`.

    ``target_module`` is only meaningful for
    :attr:`ReflectionRouteKind.REVISIT_SIBLING_RTL`; the dispatcher
    falls back to ESCALATE_HUMAN when it is unset or names a non-sibling.
    ``reason`` is informational — logged to provenance / trace, never
    affecting dispatch.
    """
    kind: ReflectionRouteKind
    target_module: str | None = None
    reason: str = ""


# --------------------------------------------------------------------------- #
# F23 — Interactive human-in-the-loop repair (the "unstuck" turn)
#
# When a stage escalates to ``EscalationLevel.HUMAN`` (F19.9's
# ``ReflectionRouteKind.ESCALATE_HUMAN`` or a spent budget), instead of
# dead-ending the operator converses; F23.2's ``HumanHintDistillAgent``
# distills that chat into a typed ``HumanHint`` that re-seeds a *bounded*
# retry. The gate stays binding — a hint can never set ``gate_ok`` — so
# this is guidance for the next attempt, not an approval override.
# --------------------------------------------------------------------------- #
class HumanHintKind(StrEnum):
    """What kind of steer the operator gave, so the dispatcher can pick a
    sensible default recovery route when the hint carries no explicit one."""
    POINT_AT_BUG = "point_at_bug"          # names a concrete defect to fix
    ADD_CONSTRAINT = "add_constraint"      # a stimulus/assumption to hold
    SUGGEST_APPROACH = "suggest_approach"  # a different implementation tack
    REDIRECT_STAGE = "redirect_stage"      # the real bug is upstream
    EXTEND_STIMULUS = "extend_stimulus"    # window too short / drive to done


class HumanHint(Artifact):
    """F23.1 — distilled, typed operator guidance for one interactive turn.

    Preserves *"never pass freeform text between agents as the carrier of
    state"*: the model-facing carrier is ``summary`` (+ ``hint_kind`` /
    ``suggested_route`` / ``references``), all IN the content hash. The raw
    chat ``raw_transcript`` is excluded from the hash (provenance/trace
    only), exactly like reasoning tapes — two hints distilled to the same
    structured guidance from differently-worded chats dedupe.

    ``suggested_route`` is an optional steer the dispatcher VALIDATES (a
    hallucinated sibling / unknown route falls back to ESCALATE_HUMAN, per
    ``graph.human_repair``); it never bypasses the gate.
    """
    kind: ArtifactKind = ArtifactKind.HUMAN_HINT
    hint_kind: HumanHintKind
    target_stage: Stage
    summary: str                                    # one-paragraph, model-facing
    suggested_route: ReflectionRouteKind | None = None
    references: list[ArtifactRef] = Field(default_factory=list)
    raw_transcript: str = ""                         # NOT in the content hash

    # Mirror Artifact._NON_CONTENT_FIELDS verbatim (Pydantic v2 wraps the
    # parent's underscore attr as a ModelPrivateAttr so `| {...}` doesn't
    # compose) and add ``raw_transcript``. Same idiom as OracleArtifact.
    _NON_CONTENT_FIELDS = {
        "artifact_id", "content_hash", "provenance", "created_at",
        "status", "version", "metadata", "raw_transcript",
    }


class PhysicalRepairRouteKind(StrEnum):
    """F21.3 — discrete recovery routes the SIGNOFF-failure dispatcher
    can pick when timing closes negative.

    The router model picks exactly one. Each route maps to a single-knob
    delta against the current ``PhysicalConfig``; the PHYSICAL node
    reapplies the accumulated history at entry to produce the config
    for the next attempt.

    * :attr:`LOWER_DENSITY`: reduce ``pl_target_density`` by 0.05
      (floored at 0.30). The standard congestion-driven setup-violation
      fix — looser placement → shorter routes → less wire delay.
    * :attr:`INCREASE_DELAY_OPTIMIZATION`: set ``synth_strategy``
      to ``"DELAY 3"``. Yosys spends more passes optimising the
      critical path at the cost of area; effective when the timing
      failure is synthesis-bound rather than placement-bound.
    * :attr:`RELAX_CLOCK_PERIOD`: multiply ``clock_period_ns`` by 1.10.
      The "admit timing can't close at the target frequency" fallback
      that still produces a working chip; F21.3b's tuner can revisit.
    * :attr:`ESCALATE_HUMAN`: out of ideas; route the spine to the
      human gate.
    """
    LOWER_DENSITY = "lower_density"
    INCREASE_DELAY_OPTIMIZATION = "increase_delay_optimization"
    RELAX_CLOCK_PERIOD = "relax_clock_period"
    ESCALATE_HUMAN = "escalate_human"


class PhysicalRepairRoute(BaseModel):
    """F21.3 — typed verdict produced by :class:`PhysicalRepairRoutingAgent`.

    Frozen so the history list on :class:`DesignState` stays hashable-
    by-comparison. ``reason`` is informational (one-sentence rationale
    from the model, logged via the ``PHYSICAL_REPAIR_ROUTED`` audit event
    and the trace span) and does NOT affect dispatch.
    """
    model_config = ConfigDict(frozen=True)

    kind: PhysicalRepairRouteKind
    reason: str = ""


# --------------------------------------------------------------------------- #
# The blackboard: head pointers + loop/escalation state
# --------------------------------------------------------------------------- #
class StageState(BaseModel):
    """Per-stage control state. The seam the control graph reads and writes."""
    stage: Stage
    status: StageStatus = StageStatus.PENDING
    head: ArtifactRef | None = None              # primary produced artifact
    results: list[ArtifactRef] = Field(default_factory=list)  # verification artifacts
    attempts: int = 0
    max_attempts: int = 3
    max_react_steps: int = 6                        # bound on the agent's intra-attempt ReAct loop
    escalation: EscalationLevel = EscalationLevel.INNER
    last_failure: ArtifactRef | None = None      # a VerificationArtifact ref
    # F23.3: interactive HUMAN-turn accounting. Each interactive "unstuck"
    # turn that dispatches a retry increments this; ``max_human_turns`` caps
    # how many times an operator can re-seed the loop before a hard stop,
    # keeping the "never loop unbounded" invariant even with a human in it.
    human_turns_used: int = 0
    max_human_turns: int = 2

    def budget_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    def human_turns_left(self) -> int:
        return max(0, self.max_human_turns - self.human_turns_used)


class ModuleState(BaseModel):
    module_id: str
    name: str
    decl: ArtifactRef | None = None              # ModuleDecl inside the plan
    stages: dict[Stage, StageState] = Field(default_factory=dict)
    status: StageStatus = StageStatus.PENDING

    def head(self, stage: Stage) -> ArtifactRef | None:
        st = self.stages.get(stage)
        return st.head if st else None


class PendingHumanRepair(BaseModel):
    """F23.5 (Option B) — an interactive HUMAN-repair request parked across a
    checkpoint interrupt.

    Set when a stage escalates in Option-B mode; carries what the operator
    needs to see (``diagnosis_ref``) and the slot their guidance fills
    (``transcript``, injected out-of-band via ``resume --hint`` / the TUI
    before the graph resumes). ``transcript is None`` at pause time; a
    non-empty value on resume drives the repair, an explicit empty/declined
    resume ends the run blocked (an RTL escalation is a failure pause, not a
    tapeout-ready one).
    """
    module_id: str
    stage: Stage
    diagnosis_ref: ArtifactRef | None = None
    transcript: str | None = None


class DesignState(BaseModel):
    """Root of the blackboard. This is the object that flows through LangGraph."""
    design_id: str
    name: str
    constraints: DesignConstraints = Field(default_factory=DesignConstraints)
    spec: ArtifactRef | None = None
    plan: ArtifactRef | None = None
    modules: dict[str, ModuleState] = Field(default_factory=dict)
    # Per-module stages live in ModuleState.stages (RTL). Whole-design stages live here:
    stages: dict[Stage, StageState] = Field(default_factory=dict)  # SYNTH, PHYSICAL, SIGNOFF, GDSII
    top_module_id: str | None = None
    current_stage: Stage = Stage.SPEC
    status: DesignStatus = DesignStatus.PLANNING
    global_feedback_budget: int = 2     # bounded cross-stage (PPA -> RTL) escalations
    # F23.5 (Option B): an interactive HUMAN-repair request parked across a
    # checkpoint interrupt. None on the happy path; set when RTL escalates in
    # Option-B mode so the pause node + resume-with-hint can round-trip the
    # operator's guidance. Cleared once the retry is dispatched.
    pending_human_repair: PendingHumanRepair | None = None
    # F21.3: accumulated SIGNOFF-failure recovery routes. The PHYSICAL
    # node walks this list at entry and applies each delta to
    # ``StageContext.physical_config`` to produce the current attempt's
    # config. Empty by default → byte-identical to today's path.
    # Capped indirectly via ``global_feedback_budget``: every dispatcher
    # entry decrements the budget, so the list cannot grow past it.
    physical_repair_routes: list[PhysicalRepairRoute] = Field(default_factory=list)
    # F19.11: snapshot of the routing flag at run creation. ``cmd_resume``
    # reads this from the checkpoint and rejects the resume when the
    # current ``settings.routing.use_test_first_workflow`` value differs
    # — switching the workflow mid-session would mix two different
    # graph topologies on the same checkpoint. NOT in any content hash
    # (DesignState itself isn't content-addressed); it rides on the
    # LangGraph checkpoint.
    use_test_first_workflow: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --------------------------------------------------------------------------- #
# Store interface (implemented in a later pass: SQLite index + content-addressed dir)
# --------------------------------------------------------------------------- #
@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, artifact: Artifact) -> ArtifactRef:
        """Compute content_hash, assign version, persist immutably, return a ref."""
        ...

    def get(self, ref: ArtifactRef) -> Artifact: ...

    def get_by_id(self, artifact_id: str, version: int | None = None) -> Artifact:
        """Latest version if `version` is None."""
        ...

    def history(self, artifact_id: str) -> list[Artifact]:
        """All versions of one logical artifact, oldest first."""
        ...

    def lineage(self, ref: ArtifactRef) -> list[Artifact]:
        """Walk provenance.inputs transitively — the DAG rooted at the spec."""
        ...

    def list(self, design_id: str, kind: ArtifactKind | None = None) -> list[ArtifactRef]:
        ...


# --------------------------------------------------------------------------- #
# Router interface (implemented in routing/ — see model_routing.md)
# --------------------------------------------------------------------------- #
class GenerationResult(BaseModel):
    candidates: list[str] = Field(default_factory=list)
    chosen: str
    invocation: ModelInvocation        # recorded into provenance
    cached: bool = False


@runtime_checkable
class ModelRouter(Protocol):
    def generate(self, task: TaskType, *, context: dict[str, Any],
                 failure: FailureDiagnosis | None = None,
                 escalation: EscalationLevel = EscalationLevel.INNER,
                 n: int | None = None) -> GenerationResult:
        """Resolve a model from the policy (registry + per-loop bindings) and generate."""
        ...

    def stream(self, task: TaskType, *, context: dict[str, Any],
               failure: FailureDiagnosis | None = None,
               escalation: EscalationLevel = EscalationLevel.INNER,
               ) -> Iterator[StreamChunk]:
        """Stream tokens for ``task`` through the resolved model (F11.2).

        Multi-candidate sampling has no meaning for an interactive stream —
        a single stream is always produced.
        """
        ...


# --------------------------------------------------------------------------- #
# Tool-service interface (implemented in tools/ — see tool_contracts.md)
# --------------------------------------------------------------------------- #
class ToolRun(BaseModel):
    """Raw result of one sandboxed execution; only a parser consumes stdout/stderr."""
    returncode: int
    stdout: str = ""
    stderr: str = ""
    artifacts_dir: str
    duration_s: float = 0.0


@runtime_checkable
class ToolService(Protocol):
    name: str
    def version(self) -> ToolVersion:
        """name, version, and the pinned IIC-OSIC-TOOLS image digest as container_digest."""
        ...


# Stages that may attribute a failure upstream and trigger bounded cross-stage feedback.
BACKEND_STAGES = {Stage.SYNTH, Stage.PHYSICAL, Stage.SIGNOFF}
