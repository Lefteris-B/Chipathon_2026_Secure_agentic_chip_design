"""Typed application settings.

Loads from defaults < YAML file < environment variables (env wins where set).

Env vars use the prefix ``CHIP_AGENT_`` and ``__`` as a nested-section delimiter,
e.g. ``CHIP_AGENT_BUDGETS__COST_BUDGET_USD=10.0`` sets ``settings.budgets.cost_budget_usd``.

Per ``@CLAUDE.md``, model/loop bindings and the sandbox image digest are *config*,
not code — this module is the typed seam every other module reads from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "BudgetSettings",
    "ConstraintDefaults",
    "LoopBinding",
    "ModelEntry",
    "PathsSettings",
    "RoutingSettings",
    "SandboxSettings",
    "Settings",
    "SettingsError",
    "TaskBinding",
]


class SettingsError(ValueError):
    """Raised for non-pydantic settings problems (e.g. YAML structure)."""


class _Strict(BaseModel):
    """Nested config sections forbid unknown keys so typos raise immediately."""

    model_config = ConfigDict(extra="forbid")


class PathsSettings(_Strict):
    runs_dir: Path = Path("runs")
    store_db: Path = Path("runs/store.sqlite")


class ConstraintDefaults(_Strict):
    """Default `DesignConstraints` fields applied when a spec omits them."""

    pdk: str = "gf180mcuD"
    std_cell_lib: str = "gf180mcu_fd_sc_mcu7t5v0"
    target_clock_ns: float | None = None
    max_die_area_um2: float | None = None
    target_utilization: float | None = None


class BudgetSettings(_Strict):
    cost_budget_usd: float = Field(default=5.0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    max_react_steps: int = Field(default=6, ge=1)
    global_feedback_budget: int = Field(default=2, ge=0)


class ModelEntry(_Strict):
    provider: str
    model: str
    endpoint: str | None = None


class LoopBinding(_Strict):
    model: str
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    n: int = Field(default=1, ge=1)


class TaskBinding(_Strict):
    model: str
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    n: int = Field(default=1, ge=1)


class RoutingSettings(_Strict):
    """The single locus of model choice (see ``docs/model_routing.md``)."""

    registry: dict[str, ModelEntry] = Field(default_factory=dict)
    loops: dict[str, LoopBinding] = Field(default_factory=dict)  # "inner" / "outer"
    tasks: dict[str, TaskBinding] = Field(default_factory=dict)
    # F11.5: per-primary fallback map. ``{"<primary_name>": "<fallback_name>"}``
    # — both keys and values must name registry entries. When a primary
    # backend is unreachable (connect timeout / DNS / refused), the router
    # retries against the configured fallback and records a
    # ``BACKEND_FALLBACK`` audit event.
    fallback: dict[str, str] = Field(default_factory=dict)
    # F19.11: opt into the M19 test-first workflow (CONTRACT / ORACLE /
    # ASSERTIONS / ORACLE_VERIFICATION + differential TB + reflection
    # routing). Default ``False`` rolls back to the pre-F19.7 path so a
    # config that omits the key (or an operator who wants to disable
    # M19) gets the original behaviour. Shipped configs in
    # ``configs/*.yaml`` set this to ``true`` so live users get M19 by
    # default.
    use_test_first_workflow: bool = False
    # F19.13: max port count for the "trivial module" heuristic that
    # skips the M19 stages on a per-module basis (CONTRACT / ORACLE /
    # ASSERTIONS / ORACLE_VERIFICATION). The full heuristic is "≤N
    # ports AND no clk/reset input port" (the second half is
    # hard-coded via the shared name sets in ``stim_ramp.py``). Default
    # ``2`` matches the F19.13 AC's literal "≤2 ports and no state"
    # reading; operators can widen up to 8 if they want single-flop
    # registers (3-4 port modules) to also fast-path.
    m19_trivial_max_ports: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def _bindings_reference_registry(self) -> RoutingSettings:
        if not self.registry:
            return self  # routing not configured; defer to router instantiation
        names = set(self.registry)
        for slot, loop_binding in self.loops.items():
            if loop_binding.model not in names:
                raise ValueError(
                    f"routing.loops.{slot}.model={loop_binding.model!r} not in registry; "
                    f"available: {sorted(names)}"
                )
        for task, task_binding in self.tasks.items():
            if task_binding.model not in names:
                raise ValueError(
                    f"routing.tasks.{task}.model={task_binding.model!r} not in registry; "
                    f"available: {sorted(names)}"
                )
        allowed_slots = {"inner", "outer"}
        bad = set(self.loops) - allowed_slots
        if bad:
            raise ValueError(
                f"routing.loops keys must be a subset of {sorted(allowed_slots)}; "
                f"got unexpected: {sorted(bad)}"
            )
        for primary, fb in self.fallback.items():
            if primary not in names:
                raise ValueError(
                    f"routing.fallback key {primary!r} is not in registry; "
                    f"available: {sorted(names)}"
                )
            if fb not in names:
                raise ValueError(
                    f"routing.fallback[{primary!r}]={fb!r} is not in registry; "
                    f"available: {sorted(names)}"
                )
            if primary == fb:
                raise ValueError(
                    f"routing.fallback[{primary!r}] must not map to itself"
                )
        return self


class SignoffSettings(_Strict):
    """F21.2 — per-corner SIGNOFF knobs.

    ``sta_corners`` is the operator-facing seam that opens multi-corner
    timing analysis: when set (e.g. ``["tt", "ss", "ff"]``) the run-time
    plumbing tells LibreLane to run per-corner STA + harvests the
    resulting reports onto ``LayoutArtifact.librelane_per_corner_timing``;
    SIGNOFF then builds a ``MultiCornerSTAReport`` instead of today's
    single-corner ``TimingReport``. When ``None`` (the default), today's
    single-corner path is byte-identical. F21.3 will read this signal
    to drive a knob-tuner; for now it's pure measurement scaffold.
    """

    sta_corners: list[str] | None = None
    sta_report_power: bool = False


class SandboxSettings(_Strict):
    """Pinned EDA toolchain image + per-invocation resource caps."""

    image: str = "hpretl/iic-osic-tools"
    image_tag: str | None = None
    image_digest: str | None = None
    cpu_limit: float = Field(default=4.0, gt=0)
    memory_limit_gb: float = Field(default=8.0, gt=0)
    time_limit_s: int = Field(default=600, ge=1)
    # Tokens prepended to every command before the actual user cmd. The
    # IIC-OSIC-TOOLS image's entrypoint expects ``--skip`` to short-circuit
    # the VNC bootstrap and exec the rest of the args directly.
    command_prefix: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _digest_shape(self) -> SandboxSettings:
        d = self.image_digest
        if d is None:
            return self
        if not d.startswith("sha256:") or len(d) != len("sha256:") + 64:
            raise ValueError(
                f"sandbox.image_digest must be 'sha256:<64 hex>', got {d!r}"
            )
        hex_part = d.removeprefix("sha256:")
        if any(c not in "0123456789abcdef" for c in hex_part):
            raise ValueError(
                f"sandbox.image_digest has non-hex chars after 'sha256:': {hex_part!r}"
            )
        return self

    @property
    def image_ref(self) -> str:
        """What to hand the sandbox runner: digest > tag > bare image."""
        if self.image_digest:
            return f"{self.image}@{self.image_digest}"
        if self.image_tag:
            return f"{self.image}:{self.image_tag}"
        return self.image

    @property
    def is_pinned(self) -> bool:
        """True iff the image is pinned to a content-addressed digest."""
        return self.image_digest is not None


class Settings(BaseSettings):
    """The whole-app config. Pass a YAML path to :meth:`from_yaml`, or rely on env."""

    model_config = SettingsConfigDict(
        env_prefix="CHIP_AGENT_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )

    paths: PathsSettings = Field(default_factory=PathsSettings)
    constraints: ConstraintDefaults = Field(default_factory=ConstraintDefaults)
    budgets: BudgetSettings = Field(default_factory=BudgetSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    signoff: SignoffSettings = Field(default_factory=SignoffSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)

    @classmethod
    def from_yaml(cls, path: Path | str) -> Settings:
        """Load defaults < YAML, then let env vars override unset/set fields.

        Raises :class:`SettingsError` for YAML-shape problems and
        :class:`pydantic.ValidationError` for typed-field problems — both carry
        the offending field path so the message is actionable.
        """
        p = Path(path)
        try:
            raw = p.read_text()
        except OSError as e:
            raise SettingsError(f"could not read config file {p}: {e}") from e
        try:
            data: Any = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            raise SettingsError(f"invalid YAML in {p}: {e}") from e
        if not isinstance(data, dict):
            raise SettingsError(
                f"YAML config at {p} must be a mapping at the top level, got {type(data).__name__}"
            )
        return cls(**data)


__all__ += ["ValidationError"]
