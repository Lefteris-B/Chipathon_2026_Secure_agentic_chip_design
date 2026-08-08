"""F8.1 — `chip-agent run` / `chip-agent resume` CLI integration tests.

Covers:

* spec markdown -> initial DesignState + persisted Spec artifact via the
  real :class:`SpecIntakeAgent` (F10.1),
* a real :class:`PlannerAgent` minting a typed :class:`DesignPlan`,
* `cmd_run` halting at the F5.3 ``await_human`` interrupt,
* `cmd_resume` advancing past the gate, minting a real GDSII artifact
  through the F6.5 driver against a stub Magic sandbox, and emitting
  a run manifest + audit log that survives a verify pass.

Every CLI test routes through a real :class:`LiteLLMRouter` over a stub
:class:`CompletionBackend` (see :mod:`tests._routing_stub`). F10.1 made
``--config`` with a non-empty ``routing.registry`` mandatory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chip_agent.cli import (
    CLIError,
    PreflightError,
    RunArgs,
    build_arg_parser,
    cmd_resume,
    cmd_run,
    main,
    preflight_local_models,
)
from chip_agent.design_state import (
    ArtifactKind,
    ArtifactRef,
    DesignConstraints,
    DesignPlan,
    DesignStatus,
    ModuleDecl,
    Provenance,
    Spec,
    Stage,
)
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.obs.replay import compare_to_manifest
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import (
    COUNTER_RTL,
    StubBackend,
    make_routing_config,
    make_test_router,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
COUNTER_SPEC = """\
# 8-bit counter

A small synchronous up-counter.

## Module

* Name: counter
* Top-level module ID: `counter`

## Ports

* `clk` — input, 1 bit, primary clock.
* `rst_n` — input, 1 bit, asynchronous active-low reset.
* `en` — input, 1 bit, count enable.
* `q` — output, 8 bits, current count.

## Constraints

* Target clock period: 10 ns.
* Target utilization: 50%.
"""

HMAC_KEY = b"f8-1-test-hmac-key"


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "counter.md"
    path.write_text(COUNTER_SPEC)
    return path


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "demo"


@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    """A Settings YAML with one ``stub-model`` covering every TaskType binding."""
    return make_routing_config(tmp_path)


@pytest.fixture
def stub_backend() -> StubBackend:
    """Backend that returns canned counter responses for every agent."""
    return StubBackend()


@pytest.fixture
def patch_router(
    monkeypatch: pytest.MonkeyPatch,
    routing_config: Path,
    stub_backend: StubBackend,
) -> StubBackend:
    """Stub ``cli._resolve_router`` so cmd_run uses the deterministic backend."""
    router, _ = make_test_router(config_path=routing_config, backend=stub_backend)
    monkeypatch.setattr("chip_agent.cli._resolve_router", lambda _args, *, settings: router)
    return stub_backend


def _run_args(
    *,
    cmd: str,
    run_dir: Path,
    spec_path: Path | None = None,
    name: str | None = None,
    design_id: str | None = None,
    config_path: Path | None = None,
) -> RunArgs:
    return RunArgs(
        cmd=cmd,
        spec_path=spec_path,
        name=name,
        run_dir=run_dir,
        design_id=design_id,
        hmac_key=HMAC_KEY,
        config_path=config_path,
    )


# --------------------------------------------------------------------------- #
# argparse shape
# --------------------------------------------------------------------------- #
def test_arg_parser_run_subcommand_requires_name_and_run_dir() -> None:
    parser = build_arg_parser()
    # F11.3 made ``--spec`` optional (the chat-handoff path uses
    # ``--design-id`` to look up an existing Spec in the store);
    # ``--name`` + ``--run-dir`` stay required at the argparse layer.
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--spec", "x.md", "--run-dir", "/tmp/x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--spec", "x.md", "--name", "counter"])
    # ``--name`` + ``--run-dir`` alone parses (the spec-vs-design-id
    # check runs inside ``cmd_run``).
    parser.parse_args(["run", "--name", "counter", "--run-dir", "/tmp/x"])


def test_arg_parser_resume_subcommand_requires_design_id_and_run_dir() -> None:
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "--run-dir", "/tmp/x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "--design-id", "d0"])


def test_arg_parser_unknown_subcommand_errors() -> None:
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["frobnicate"])


# --------------------------------------------------------------------------- #
# cmd_run — spec loader + halt at human gate
# --------------------------------------------------------------------------- #
def test_cmd_run_persists_spec_and_halts_at_human_gate(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    out = cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-d0", config_path=routing_config,
    ))

    assert out.design_id == "counter-d0"
    assert out.spec_ref.artifact_id == "counter-d0.spec"
    assert out.spec_ref.kind is ArtifactKind.SPEC
    assert out.paused_state.status is DesignStatus.AWAITING_HUMAN
    # F5.3: the spine halts in the await_human node *before* GDSII.
    assert out.paused_state.current_stage is Stage.SIGNOFF


def test_cmd_run_extracts_constraints_from_markdown(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    out = cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-d1", config_path=routing_config,
    ))
    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        spec = store.get(out.spec_ref)
    assert isinstance(spec, Spec)
    # Constraints flow from the LLM-normalised text (the stub backend
    # returns "target clock period 10 ns" + "Target utilisation: 30%").
    assert spec.constraints.target_clock_ns == 10.0
    assert spec.constraints.target_utilization == 0.3
    assert spec.constraints.pdk == "gf180mcuD"


def test_cmd_run_mints_design_id_when_none_given(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    out = cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="Counter Demo", run_dir=run_dir,
        config_path=routing_config,
    ))
    assert out.design_id.startswith("counter-demo-")
    # name slugged + timestamp + random suffix.
    parts = out.design_id.split("-")
    assert len(parts) >= 4


def test_cmd_run_missing_spec_file_raises(
    run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    with pytest.raises(FileNotFoundError):
        cmd_run(_run_args(
            cmd="run", spec_path=Path("/nonexistent/spec.md"),
            name="counter", run_dir=run_dir, design_id="d0",
            config_path=routing_config,
        ))


def test_cmd_run_empty_spec_file_raises(
    tmp_path: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    spec = tmp_path / "empty.md"
    spec.write_text("")
    with pytest.raises(CLIError, match="empty"):
        cmd_run(_run_args(
            cmd="run", spec_path=spec, name="counter", run_dir=run_dir,
            design_id="d0", config_path=routing_config,
        ))


def test_cmd_run_writes_audit_event_for_spec_promotion(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-d2", config_path=routing_config,
    ))
    audit = SqliteAuditLog(db_path=run_dir / "audit.sqlite", hmac_key=HMAC_KEY)
    try:
        events = list(audit.events(design_id="counter-d2"))
    finally:
        audit.close()
    promoted = [e for e in events if e.event_type is EventType.ARTIFACT_PROMOTED]
    assert promoted, "expected an ARTIFACT_PROMOTED event for the spec"
    assert promoted[0].payload["stage"] == Stage.SPEC.value
    gate = [e for e in events if e.event_type is EventType.GATE_DECISION]
    assert gate and gate[-1].payload["verdict"] == "await_human"


# --------------------------------------------------------------------------- #
# cmd_resume — pass the human gate, mint a GDSII, emit the manifest
# --------------------------------------------------------------------------- #
def test_cmd_resume_produces_gdsii_ref(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-r0", config_path=routing_config,
    ))
    out = cmd_resume(_run_args(
        cmd="resume", run_dir=run_dir, design_id="counter-r0",
        config_path=routing_config,
    ))
    assert out.gds_ref.kind is ArtifactKind.GDSII
    # The synthetic top module id from the counter spec.
    assert out.gds_ref.artifact_id == "counter-r0.counter.gds"
    assert out.final_state.status is DesignStatus.COMPLETED
    assert out.final_state.current_stage is Stage.GDSII


def test_cmd_resume_emits_manifest_with_full_stub_dag(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """F8.3 enriched DAG: Spec → Plan → RTL → Netlist → Layout → GDS."""
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-r1", config_path=routing_config,
    ))
    out = cmd_resume(_run_args(
        cmd="resume", run_dir=run_dir, design_id="counter-r1",
        config_path=routing_config,
    ))
    kinds = {e.kind for e in out.manifest.entries}
    assert kinds == {
        ArtifactKind.GDSII, ArtifactKind.LAYOUT, ArtifactKind.NETLIST,
        ArtifactKind.RTL, ArtifactKind.PLAN, ArtifactKind.SPEC,
    }
    assert out.manifest.root_ref == out.gds_ref
    assert out.manifest_path.exists()
    payload = json.loads(out.manifest_path.read_text())
    assert payload["design_id"] == "counter-r1"
    assert len(payload["entries"]) == 6


def test_cmd_resume_manifest_is_reproducible_against_same_store(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """The committed manifest must verify clean against the store it came from."""
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-r2", config_path=routing_config,
    ))
    out = cmd_resume(_run_args(
        cmd="resume", run_dir=run_dir, design_id="counter-r2",
        config_path=routing_config,
    ))
    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        diff = compare_to_manifest(store, out.manifest)
    assert diff.reproducible, (
        f"expected manifest to verify clean against its store; "
        f"missing={diff.missing!r}, mismatched={diff.mismatched!r}"
    )


def test_cmd_resume_audit_log_chain_verifies_clean(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-r3", config_path=routing_config,
    ))
    cmd_resume(_run_args(
        cmd="resume", run_dir=run_dir, design_id="counter-r3",
        config_path=routing_config,
    ))
    audit = SqliteAuditLog(db_path=run_dir / "audit.sqlite", hmac_key=HMAC_KEY)
    try:
        verdict = audit.verify(design_id="counter-r3")
    finally:
        audit.close()
    assert verdict.valid
    assert not verdict.findings


def test_cmd_resume_records_human_decision_and_gds_promotion(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-r4", config_path=routing_config,
    ))
    cmd_resume(_run_args(
        cmd="resume", run_dir=run_dir, design_id="counter-r4",
        config_path=routing_config,
    ))
    audit = SqliteAuditLog(db_path=run_dir / "audit.sqlite", hmac_key=HMAC_KEY)
    try:
        events = list(audit.events(design_id="counter-r4"))
    finally:
        audit.close()

    decisions = [e for e in events if e.event_type is EventType.HUMAN_DECISION]
    assert decisions and decisions[-1].payload["decision"] == "approve"
    promoted = [
        e for e in events
        if e.event_type is EventType.ARTIFACT_PROMOTED
        and e.payload.get("stage") == Stage.GDSII.value
    ]
    assert promoted, "expected GDS promotion to land in the audit log"


def test_cmd_resume_without_run_errors(
    run_dir: Path, routing_config: Path,
) -> None:
    """Resume against an empty run dir surfaces a clear error."""
    with pytest.raises(FileNotFoundError):
        cmd_resume(_run_args(
            cmd="resume", run_dir=run_dir, design_id="nonexistent",
            config_path=routing_config,
        ))


# --------------------------------------------------------------------------- #
# main(argv) wiring
# --------------------------------------------------------------------------- #
def test_main_run_then_resume(
    spec_file: Path, run_dir: Path, monkeypatch: pytest.MonkeyPatch,
    routing_config: Path, patch_router: StubBackend,
) -> None:
    monkeypatch.setenv("CHIP_AGENT_HMAC_KEY", HMAC_KEY.decode("utf-8"))
    rc = main([
        "run", "--spec", str(spec_file), "--name", "counter",
        "--run-dir", str(run_dir), "--design-id", "main-r0",
        "--config", str(routing_config),
    ])
    assert rc == 0

    rc = main([
        "resume", "--design-id", "main-r0", "--run-dir", str(run_dir),
        "--config", str(routing_config),
    ])
    assert rc == 0

    # The manifest landed in the conventional location.
    manifest_path = run_dir / "manifests" / "main-r0.json"
    assert manifest_path.exists()


def test_main_run_halts_without_resume(
    spec_file: Path, run_dir: Path, monkeypatch: pytest.MonkeyPatch,
    routing_config: Path, patch_router: StubBackend,
) -> None:
    """A standalone `run` should leave the manifest absent — the gate held."""
    monkeypatch.setenv("CHIP_AGENT_HMAC_KEY", HMAC_KEY.decode("utf-8"))
    main([
        "run", "--spec", str(spec_file), "--name", "counter",
        "--run-dir", str(run_dir), "--design-id", "halted",
        "--config", str(routing_config),
    ])
    assert not (run_dir / "manifests" / "halted.json").exists()


# --------------------------------------------------------------------------- #
# F9.2 — --sandbox / --config wiring
# --------------------------------------------------------------------------- #
def test_arg_parser_default_sandbox_is_stub() -> None:
    parser = build_arg_parser()
    ns = parser.parse_args([
        "run", "--spec", "x.md", "--name", "x", "--run-dir", "/tmp/x",
    ])
    assert ns.sandbox == "stub"
    assert ns.config is None


def test_arg_parser_resume_sandbox_defaults_to_none() -> None:
    """F14.4: resume's --sandbox defaults to None ("inherit the run's backend"),
    while run keeps the stub default so its behaviour is unchanged."""
    parser = build_arg_parser()
    resume_ns = parser.parse_args([
        "resume", "--design-id", "d0", "--run-dir", "/tmp/x",
    ])
    assert resume_ns.sandbox is None
    run_ns = parser.parse_args([
        "run", "--spec", "x.md", "--name", "x", "--run-dir", "/tmp/x",
    ])
    assert run_ns.sandbox == "stub"


def test_cmd_run_writes_run_meta_with_sandbox_kind(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """F14.4: a run records the sandbox backend it used so resume can reuse it."""
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-meta0", config_path=routing_config,
    ))
    meta = json.loads((run_dir / "run_meta.json").read_text())
    assert meta["sandbox_kind"] == "stub"


def test_cmd_resume_inherits_recorded_sandbox_backend(
    spec_file: Path, run_dir: Path, routing_config: Path,
    patch_router: StubBackend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F14.4: with no explicit --sandbox, resume adopts the backend the run
    recorded — so a docker run streams out a real GDS instead of the stub."""
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-inh0", config_path=routing_config,
    ))
    # Simulate the original run having used docker.
    (run_dir / "run_meta.json").write_text(json.dumps({"sandbox_kind": "docker"}))

    import chip_agent.cli as climod
    seen: dict[str, str | None] = {}

    def _spy(args: RunArgs, *, settings: object) -> None:
        seen["kind"] = args.sandbox_kind
        return None  # keep the rest of resume on stub services

    monkeypatch.setattr(climod, "_resolve_sandbox", _spy)
    cmd_resume(RunArgs(
        cmd="resume", spec_path=None, name=None, run_dir=run_dir,
        design_id="counter-inh0", hmac_key=HMAC_KEY,
        config_path=routing_config, sandbox_kind=None,
    ))
    assert seen["kind"] == "docker"


def test_cmd_resume_explicit_sandbox_overrides_recorded(
    spec_file: Path, run_dir: Path, routing_config: Path,
    patch_router: StubBackend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F14.4: an explicit --sandbox on resume wins over the recorded backend."""
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="counter-inh1", config_path=routing_config,
    ))
    (run_dir / "run_meta.json").write_text(json.dumps({"sandbox_kind": "docker"}))

    import chip_agent.cli as climod
    seen: dict[str, str | None] = {}

    def _spy(args: RunArgs, *, settings: object) -> None:
        seen["kind"] = args.sandbox_kind
        return None

    monkeypatch.setattr(climod, "_resolve_sandbox", _spy)
    cmd_resume(RunArgs(
        cmd="resume", spec_path=None, name=None, run_dir=run_dir,
        design_id="counter-inh1", hmac_key=HMAC_KEY,
        config_path=routing_config, sandbox_kind="stub",
    ))
    assert seen["kind"] == "stub"


def test_arg_parser_accepts_sandbox_docker_with_config() -> None:
    parser = build_arg_parser()
    ns = parser.parse_args([
        "run", "--spec", "x.md", "--name", "x", "--run-dir", "/tmp/x",
        "--sandbox", "docker", "--config", "configs/demo-counter.yaml",
    ])
    assert ns.sandbox == "docker"
    assert ns.config == Path("configs/demo-counter.yaml")


def test_arg_parser_rejects_unknown_sandbox() -> None:
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "run", "--spec", "x.md", "--name", "x", "--run-dir", "/tmp/x",
            "--sandbox", "kvm",
        ])


def test_cmd_run_docker_sandbox_rejects_unpinned_config(
    spec_file: Path, run_dir: Path, tmp_path: Path, patch_router: StubBackend,
) -> None:
    cfg = tmp_path / "unpinned.yaml"
    cfg.write_text(
        "sandbox:\n  image: alpine\n  image_tag: '3.20'\n"
        "routing:\n"
        "  registry:\n"
        "    stub-model: {provider: stub, model: deterministic}\n"
        "  tasks:\n"
        "    spec_intake: {model: stub-model}\n"
        "    plan:        {model: stub-model}\n"
        "    rtl_gen:     {model: stub-model}\n"
        "    rtl_repair:  {model: stub-model}\n"
        "    tb_gen:      {model: stub-model}\n"
        "    diagnose:    {model: stub-model}\n",
    )
    args = RunArgs(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="d", hmac_key=HMAC_KEY, sandbox_kind="docker",
        config_path=cfg,
    )
    from chip_agent.tools.image import ImageProvisioningError
    with pytest.raises(ImageProvisioningError, match="not digest-pinned"):
        cmd_run(args)


def test_cmd_run_stub_sandbox_pinned_by_default(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """The default --sandbox stub path keeps the counter spec running."""
    out = cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="stub-default", config_path=routing_config,
    ))
    assert out.paused_state.status is DesignStatus.AWAITING_HUMAN


@pytest.mark.docker
def test_cmd_run_docker_sandbox_end_to_end(
    spec_file: Path, run_dir: Path, patch_router: StubBackend,
) -> None:
    """F9.2 AC: --sandbox docker drives the spine against the real container.

    Skipped unless the pinned IIC-OSIC-TOOLS image is locally available.
    The signoff stage still uses stub runners (F9.x scope), but Yosys +
    LibreLane + Magic-GDS run inside the real container.
    """
    from chip_agent.settings import Settings
    from chip_agent.tools.image import image_locally_available
    cfg = Path("configs/demo-counter.yaml")
    if not cfg.exists():
        pytest.skip("configs/demo-counter.yaml missing")
    settings = Settings.from_yaml(cfg)
    if not image_locally_available(settings.sandbox):
        pytest.skip(
            f"{settings.sandbox.image}:{settings.sandbox.image_tag} not pulled locally",
        )

    args = RunArgs(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="docker-smoke", hmac_key=HMAC_KEY,
        sandbox_kind="docker", config_path=cfg,
    )
    out = cmd_run(args)
    assert out.paused_state.status is DesignStatus.AWAITING_HUMAN


# --------------------------------------------------------------------------- #
# F9.3 — RTL inner loop wired through real services (when sandbox provided)
# --------------------------------------------------------------------------- #
def test_cmd_run_stub_sandbox_promotes_rtl_head_in_audit_log(
    spec_file: Path, run_dir: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """The RTL inner loop produces one ``ARTIFACT_PROMOTED`` event per module.

    Even on the stub-sandbox path, the spine drives a real RTL stage and
    emits the F9.1 audit event when the RTL head promotes.
    """
    cmd_run(_run_args(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="rtl-d0", config_path=routing_config,
    ))
    audit = SqliteAuditLog(db_path=run_dir / "audit.sqlite", hmac_key=HMAC_KEY)
    try:
        events = list(audit.events(design_id="rtl-d0"))
    finally:
        audit.close()
    rtl_promoted = [
        e for e in events
        if e.event_type is EventType.ARTIFACT_PROMOTED
        and e.payload.get("stage") == Stage.RTL.value
    ]
    assert len(rtl_promoted) == 1
    assert rtl_promoted[0].payload["ref"]["kind"] == ArtifactKind.RTL.value


@pytest.mark.docker
def test_cmd_run_docker_sandbox_rtl_uses_real_verible(
    spec_file: Path, run_dir: Path, patch_router: StubBackend,
) -> None:
    """F9.3 AC: --sandbox docker runs Verible + Verilator inside the container.

    The persisted lint result carries the verible tool version + the
    container digest in its ``checker`` field — proof the F9.3 service
    swap fired (the F9.1 stub linter has no checker tool).
    """
    from chip_agent.settings import Settings
    from chip_agent.store.sqlite_store import SqliteArtifactStore
    from chip_agent.tools.image import image_locally_available
    cfg = Path("configs/demo-counter.yaml")
    if not cfg.exists():
        pytest.skip("configs/demo-counter.yaml missing")
    settings = Settings.from_yaml(cfg)
    if not image_locally_available(settings.sandbox):
        pytest.skip(
            f"{settings.sandbox.image}:{settings.sandbox.image_tag} not pulled locally",
        )

    args = RunArgs(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="rtl-verible", hmac_key=HMAC_KEY,
        sandbox_kind="docker", config_path=cfg,
    )
    cmd_run(args)

    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        lint = store.get_by_id("rtl-verible.counter.lint")
    assert lint.passed, lint.violations
    assert lint.checker is not None
    assert lint.checker.name == "verible"
    assert lint.checker.container_digest == settings.sandbox.image_digest


# --------------------------------------------------------------------------- #
# F10.1 — real router records a ModelInvocation in provenance
# --------------------------------------------------------------------------- #
def test_cmd_run_with_real_router_records_model_invocation(
    spec_file: Path, run_dir: Path, routing_config: Path,
) -> None:
    """A live ``LiteLLMRouter`` records its ``ModelInvocation`` in provenance.

    Builds a real router over the stub backend (no monkeypatching) so the
    real ``_resolve_router`` path is exercised end-to-end.
    """
    backend = StubBackend()
    backend_calls_before = len(backend.calls)
    router, _ = make_test_router(config_path=routing_config, backend=backend)

    # Inject the prebuilt router via the existing monkeypatch contract.
    import chip_agent.cli as cli_mod
    original = cli_mod._resolve_router
    cli_mod._resolve_router = lambda _args, *, settings: router  # type: ignore[assignment]
    try:
        out = cmd_run(_run_args(
            cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
            design_id="router-d0", config_path=routing_config,
        ))
    finally:
        cli_mod._resolve_router = original  # type: ignore[assignment]

    assert len(backend.calls) > backend_calls_before, (
        "expected the stub backend to be called by the agents"
    )
    spec_call = backend.calls[0]
    assert spec_call["model"] == "stub/deterministic"

    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        rtl = store.get_by_id("router-d0.counter.rtl")
    assert rtl.provenance.model is not None
    assert rtl.provenance.model.provider == "stub"
    assert rtl.provenance.model.model == "deterministic"
    # Token counts flow from the backend's CompletionResult.
    assert rtl.provenance.model.prompt_tokens == 100
    # The minted RTL body matches the stub backend's canned Verilog.
    body = store.get_blob(rtl.source).decode("utf-8")
    assert body.rstrip("\n") == COUNTER_RTL.rstrip("\n")
    assert out.paused_state.status is DesignStatus.AWAITING_HUMAN


def test_cmd_run_unknown_model_reference_errors_clearly(
    tmp_path: Path,
) -> None:
    """``RoutingSettings`` validator catches a binding referencing a missing model."""
    cfg = tmp_path / "broken.yaml"
    cfg.write_text(
        "routing:\n"
        "  registry:\n"
        "    sonnet: {provider: anthropic, model: claude-sonnet-4-6}\n"
        "  tasks:\n"
        "    rtl_gen: {model: nonexistent, temperature: 0.2, n: 1}\n",
    )
    from pydantic import ValidationError

    from chip_agent.settings import Settings
    with pytest.raises(ValidationError, match="not in registry"):
        Settings.from_yaml(cfg)


# --------------------------------------------------------------------------- #
# preflight_local_models — verifies Ollama daemons + pulled models before
# the TUI hides the terminal under its alt-screen.
# --------------------------------------------------------------------------- #
def _ollama_only_settings(tmp_path: Path, *, model: str = "qwen3-coder:latest"):
    """Build a Settings with one ollama-only registry entry."""
    from chip_agent.settings import Settings
    cfg = tmp_path / "ollama-only.yaml"
    cfg.write_text(
        "routing:\n"
        "  registry:\n"
        f"    local: {{provider: ollama, model: {model}, endpoint: http://localhost:11434}}\n"
        "  tasks:\n"
        "    spec_intake: {model: local, temperature: 0.0, n: 1}\n",
    )
    return Settings.from_yaml(cfg)


class _FakeResp:
    """Minimal urlopen() response context manager."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_preflight_passes_when_model_pulled(tmp_path: Path) -> None:
    settings = _ollama_only_settings(tmp_path)
    payload = json.dumps(
        {"models": [{"name": "qwen3-coder:latest"}]},
    ).encode("utf-8")
    seen: list[str] = []

    def fake_open(req, timeout):  # type: ignore[no-untyped-def]
        seen.append(req.full_url)
        return _FakeResp(payload)

    preflight_local_models(settings, opener=fake_open)
    assert seen == ["http://localhost:11434/api/tags"]


def test_preflight_accepts_tagless_model_via_latest_fallback(tmp_path: Path) -> None:
    """User wrote ``qwen3-coder`` (no tag); Ollama returns ``qwen3-coder:latest``."""
    settings = _ollama_only_settings(tmp_path, model="qwen3-coder")
    payload = json.dumps(
        {"models": [{"name": "qwen3-coder:latest"}]},
    ).encode("utf-8")
    preflight_local_models(
        settings, opener=lambda _req, _t: _FakeResp(payload),
    )


def test_preflight_raises_when_daemon_unreachable(tmp_path: Path) -> None:
    import urllib.error
    settings = _ollama_only_settings(tmp_path)

    def fake_open(_req, _timeout):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("connection refused")

    with pytest.raises(PreflightError, match="unreachable"):
        preflight_local_models(settings, opener=fake_open)


def test_preflight_raises_when_model_missing(tmp_path: Path) -> None:
    settings = _ollama_only_settings(tmp_path)
    payload = json.dumps({"models": [{"name": "llama3:latest"}]}).encode("utf-8")
    with pytest.raises(PreflightError, match="not pulled"):
        preflight_local_models(
            settings, opener=lambda _req, _t: _FakeResp(payload),
        )


# --------------------------------------------------------------------------- #
# _seed_initial_state — regression for the multi-module BlackboardError.
# A real frontier run on a UART receiver produced a 4-module plan
# (uart_rx_top + rx_sync + baud_gen + rx_fsm); the spine crashed at the
# first submodule with ``BlackboardError("unknown module_id 'rx_sync'")``
# because the seed only populated ``modules[top]``. The graph already
# iterates every module in plan.modules — the seed has to keep pace.
# --------------------------------------------------------------------------- #
def test_seed_initial_state_populates_every_module_in_plan() -> None:
    from chip_agent.cli import _seed_initial_state

    spec = Spec(
        artifact_id="uart.spec", design_id="uart",
        raw_text="raw", normalized="norm",
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC),
    )
    plan = DesignPlan(
        artifact_id="uart.plan", design_id="uart",
        top_module_id="uart_rx_top",
        modules=[
            ModuleDecl(
                module_id="uart_rx_top", name="uart_rx",
                description="top-level UART receiver",
                depends_on=["rx_sync", "baud_gen", "rx_fsm"],
            ),
            ModuleDecl(
                module_id="rx_sync", name="uart_rx_sync",
                description="2-flop metastability synchroniser",
            ),
            ModuleDecl(
                module_id="baud_gen", name="uart_baud_gen",
                description="baud-rate tick generator",
            ),
            ModuleDecl(
                module_id="rx_fsm", name="uart_rx_fsm",
                description="byte-receive state machine",
            ),
        ],
        provenance=Provenance(produced_by=Stage.PLAN),
    )
    spec_ref = ArtifactRef(
        artifact_id=spec.artifact_id, version=1,
        kind=ArtifactKind.SPEC, content_hash="sha256:" + "0" * 64,
    )
    plan_ref = ArtifactRef(
        artifact_id=plan.artifact_id, version=1,
        kind=ArtifactKind.PLAN, content_hash="sha256:" + "1" * 64,
    )

    state = _seed_initial_state(
        design_id="uart", name="uart",
        spec=spec, plan=plan, spec_ref=spec_ref, plan_ref=plan_ref,
    )

    # Every plan module — top AND submodules — must have a state entry,
    # or the RTL graph's per-module iteration trips BlackboardError.
    assert set(state.modules) == {
        "uart_rx_top", "rx_sync", "baud_gen", "rx_fsm",
    }
    # Names should be propagated from the ModuleDecl, not collapsed to
    # the module_id.
    assert state.modules["rx_sync"].name == "uart_rx_sync"
    assert state.top_module_id == "uart_rx_top"


def test_seed_initial_state_tolerates_top_missing_from_modules_list() -> None:
    """Defensive: a malformed plan that lists no modules but names a top
    still produces a usable DesignState with that top seeded."""
    from chip_agent.cli import _seed_initial_state

    spec = Spec(
        artifact_id="x.spec", design_id="x",
        raw_text="r", normalized="n",
        constraints=DesignConstraints(),
        provenance=Provenance(produced_by=Stage.SPEC),
    )
    plan = DesignPlan(
        artifact_id="x.plan", design_id="x",
        top_module_id="orphan_top",
        modules=[],  # empty — should still seed the top
        provenance=Provenance(produced_by=Stage.PLAN),
    )
    refs = [
        ArtifactRef(
            artifact_id=a, version=1, kind=k,
            content_hash="sha256:" + "0" * 64,
        )
        for a, k in [("x.spec", ArtifactKind.SPEC), ("x.plan", ArtifactKind.PLAN)]
    ]
    state = _seed_initial_state(
        design_id="x", name="x",
        spec=spec, plan=plan, spec_ref=refs[0], plan_ref=refs[1],
    )
    assert "orphan_top" in state.modules


# --------------------------------------------------------------------------- #
# _resolve_verilog_top_name — the planner tags every module with a logical
# ``module_id`` handle (typically ``mod_<name>``) used for stage bookkeeping,
# while the actual Verilog ``module <name>`` token lives on ``ModuleDecl.name``.
# Tools that consume RTL (Verilator, Yosys, LibreLane) MUST see the Verilog
# name. The live "counter" run died at Verilator stage 2/80 with
# ``--top-module 'mod_up_counter_8bit' was not found in design`` because
# ``cli.py`` passed the planner handle into ``PhysicalConfig.design_name``
# instead of resolving it to the matching module's ``name``.
# --------------------------------------------------------------------------- #
def test_resolve_verilog_top_name_returns_module_name_not_id() -> None:
    """The Verilog declaration name (``up_counter_8bit``) lives on
    ``ModuleDecl.name``; the planner handle (``mod_up_counter_8bit``) lives
    on ``module_id``. The resolver returns the former."""
    from chip_agent.cli import _resolve_verilog_top_name

    plan = DesignPlan(
        artifact_id="d.plan", design_id="d",
        top_module_id="mod_up_counter_8bit",
        modules=[
            ModuleDecl(
                module_id="mod_up_counter_8bit",
                name="up_counter_8bit",       # ← the Verilog identifier
                description="8-bit synchronous up counter",
            ),
        ],
        provenance=Provenance(produced_by=Stage.PLAN),
    )
    assert _resolve_verilog_top_name(plan) == "up_counter_8bit"


def test_resolve_verilog_top_name_picks_correct_module_among_many() -> None:
    """A multi-module plan: the resolver must find the *top* module's name,
    not any other module's name."""
    from chip_agent.cli import _resolve_verilog_top_name

    plan = DesignPlan(
        artifact_id="u.plan", design_id="u",
        top_module_id="mod_uart_rx_top",
        modules=[
            ModuleDecl(
                module_id="mod_baud_gen", name="uart_baud_gen",
                description="baud-rate tick generator",
            ),
            ModuleDecl(
                module_id="mod_uart_rx_top", name="uart_rx_top",
                description="top-level UART receiver",
                depends_on=["mod_baud_gen", "mod_rx_fsm"],
            ),
            ModuleDecl(
                module_id="mod_rx_fsm", name="uart_rx_fsm",
                description="byte-receive state machine",
            ),
        ],
        provenance=Provenance(produced_by=Stage.PLAN),
    )
    assert _resolve_verilog_top_name(plan) == "uart_rx_top"


def test_resolve_verilog_top_name_raises_when_top_id_missing() -> None:
    """Defensive: a malformed plan whose ``top_module_id`` doesn't appear in
    its ``modules`` list is a planner bug. Raise a typed ``CLIError`` with
    the available handles so the failure is triagable."""
    from chip_agent.cli import CLIError, _resolve_verilog_top_name

    plan = DesignPlan(
        artifact_id="b.plan", design_id="b",
        top_module_id="mod_ghost_top",
        modules=[
            ModuleDecl(
                module_id="mod_real_module", name="real_module",
                description="the only real module here",
            ),
        ],
        provenance=Provenance(produced_by=Stage.PLAN),
    )
    with pytest.raises(CLIError) as exc_info:
        _resolve_verilog_top_name(plan)
    msg = str(exc_info.value)
    assert "mod_ghost_top" in msg
    assert "mod_real_module" in msg          # surfaces what *is* available
    assert "inconsistent DesignPlan" in msg  # names the planner as the source


def test_resolve_verilog_top_name_handles_id_equal_to_name() -> None:
    """The planner doesn't always prefix the handle. If a plan uses the
    same string for both ``module_id`` and ``name`` (legitimate when the
    planner picks the Verilog name as its handle), the resolver still
    works — no special case needed."""
    from chip_agent.cli import _resolve_verilog_top_name

    plan = DesignPlan(
        artifact_id="c.plan", design_id="c",
        top_module_id="counter",
        modules=[
            ModuleDecl(
                module_id="counter", name="counter",
                description="trivial counter",
            ),
        ],
        provenance=Provenance(produced_by=Stage.PLAN),
    )
    assert _resolve_verilog_top_name(plan) == "counter"


# --------------------------------------------------------------------------- #
# _preflight_docker_image — verifies the pinned IIC-OSIC-TOOLS image is
# locally available before the TUI hides the terminal. Default `tui` runs
# with --sandbox docker now, so this preflight is what keeps the failure
# mode visible.
# --------------------------------------------------------------------------- #
def _docker_settings(tmp_path: Path, *, pinned: bool):
    """Build a Settings whose sandbox is (or isn't) digest-pinned."""
    from chip_agent.settings import Settings
    cfg = tmp_path / "sandbox-settings.yaml"
    yaml = (
        "sandbox:\n"
        "  image: hpretl/iic-osic-tools\n"
        "  image_tag: chipathon26\n"
    )
    if pinned:
        yaml += "  image_digest: sha256:" + "a" * 64 + "\n"
    cfg.write_text(yaml)
    return cfg, Settings.from_yaml(cfg)


def test_docker_preflight_raises_when_image_not_pulled(tmp_path: Path) -> None:
    from chip_agent.cli import _preflight_docker_image
    from chip_agent.tools.image import ImageProvisioningError

    cfg, settings = _docker_settings(tmp_path, pinned=True)

    def fake_verify(_sandbox_settings):  # type: ignore[no-untyped-def]
        raise ImageProvisioningError("docker: not found")

    with pytest.raises(PreflightError, match="not available locally"):
        _preflight_docker_image(settings, config_path=cfg, verify=fake_verify)


def test_docker_preflight_raises_when_unpinned(tmp_path: Path) -> None:
    from chip_agent.cli import _preflight_docker_image
    cfg, settings = _docker_settings(tmp_path, pinned=False)
    with pytest.raises(PreflightError, match="not digest-pinned"):
        _preflight_docker_image(
            settings, config_path=cfg, verify=lambda _s: None,
        )


def test_docker_preflight_passes_when_verify_succeeds(tmp_path: Path) -> None:
    from chip_agent.cli import _preflight_docker_image
    cfg, settings = _docker_settings(tmp_path, pinned=True)
    seen: list[bool] = []
    _preflight_docker_image(
        settings, config_path=cfg,
        verify=lambda _s: seen.append(True),
    )
    assert seen == [True]


def test_preflight_is_a_noop_for_non_ollama_registry(tmp_path: Path) -> None:
    """A frontier-only / stub-only registry should skip the network entirely."""
    from chip_agent.settings import Settings
    cfg = tmp_path / "frontier-only.yaml"
    cfg.write_text(
        "routing:\n"
        "  registry:\n"
        "    frontier: {provider: anthropic, model: claude-sonnet-4-6}\n"
        "  tasks:\n"
        "    spec_intake: {model: frontier, temperature: 0.0, n: 1}\n",
    )
    settings = Settings.from_yaml(cfg)

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("network must not be called for non-ollama entries")

    preflight_local_models(settings, opener=boom)
