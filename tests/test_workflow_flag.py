"""F19.11 acceptance: ``routing.use_test_first_workflow`` flag.

Covers the AC:

* Both paths produce the same ``demo-counter`` GDSII ``content_hash``.
* Under the new path the audit log records the four M19
  ``ARTIFACT_PROMOTED`` events; under the old path it does not.
* Switching the flag mid-session is rejected with a ``CLIError`` whose
  message names the field.

Also pins the plumbing invariants: ``RoutingSettings`` default is
``False``, the YAML round-trips both values, and
``build_demo_stage_context(use_test_first_workflow=False)`` leaves the
four M19 agents + the F19.9 reflection router unset.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chip_agent.cli import CLIError, RunArgs, cmd_resume, cmd_run
from chip_agent.design_state import Stage
from chip_agent.obs.audit_log import EventType, SqliteAuditLog
from chip_agent.obs.tracing import InMemoryTracer
from chip_agent.settings import Settings
from tests._routing_stub import (
    StubBackend,
    make_routing_config,
    make_test_router,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COUNTER_SPEC = REPO_ROOT / "specs" / "counter.md"
HMAC_KEY = b"f19-11-workflow-flag-hmac"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _args(
    *,
    cmd: str,
    run_dir: Path,
    design_id: str,
    config_path: Path,
    spec_path: Path | None = None,
    name: str | None = None,
    tracer: InMemoryTracer | None = None,
) -> RunArgs:
    return RunArgs(
        cmd=cmd, spec_path=spec_path, name=name, run_dir=run_dir,
        design_id=design_id, hmac_key=HMAC_KEY, tracer=tracer,
        config_path=config_path,
    )


def _write_routing_config(
    tmp_path: Path, *, use_test_first_workflow: bool,
) -> Path:
    """Write a stub routing YAML with the flag set to the requested value."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = make_routing_config(tmp_path)
    text = cfg.read_text()
    flag_value = "true" if use_test_first_workflow else "false"
    new_text = re.sub(
        r"use_test_first_workflow:\s*(true|false)",
        f"use_test_first_workflow: {flag_value}",
        text,
        count=1,
    )
    cfg.write_text(new_text)
    return cfg


@pytest.fixture
def patch_router(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StubBackend:
    """A single :class:`StubBackend` that the CLI uses regardless of flag."""
    backend = StubBackend()
    router, _ = make_test_router(
        config_path=make_routing_config(tmp_path),
        backend=backend,
    )
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router", lambda _args, *, settings: router,
    )
    return backend


def _drive_run_then_resume(
    run_dir: Path, *, config_path: Path, design_id: str,
) -> Path:
    """Run the counter demo end-to-end; return the manifest path."""
    cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id=design_id,
        spec_path=COUNTER_SPEC, name="counter",
        config_path=config_path,
    ))
    out = cmd_resume(_args(
        cmd="resume", run_dir=run_dir, design_id=design_id,
        config_path=config_path,
    ))
    return out.manifest_path


def _gds_content_hash(manifest_path: Path) -> str:
    """Pull the GDSII root_ref content_hash from a written manifest."""
    payload = manifest_path.read_text()
    import json
    return json.loads(payload)["root_ref"]["content_hash"]


# --------------------------------------------------------------------------- #
# AC #1 — both paths produce the same GDSII content_hash
# --------------------------------------------------------------------------- #
def test_gds_content_hash_equal_across_workflow_flag_values(
    tmp_path: Path, patch_router: StubBackend,
) -> None:
    """The counter demo's GDSII content_hash is identical under both
    workflow flag values — the M19 stages must not perturb the GDS
    lineage. Both runs use the same ``design_id`` so artifact_id
    differences don't account for the hash split."""
    new_path_dir = tmp_path / "new_path"
    old_path_dir = tmp_path / "old_path"

    new_cfg = _write_routing_config(
        new_path_dir, use_test_first_workflow=True,
    )
    old_cfg = _write_routing_config(
        old_path_dir, use_test_first_workflow=False,
    )

    design_id = "f19-11-compare"
    new_manifest = _drive_run_then_resume(
        new_path_dir, config_path=new_cfg, design_id=design_id,
    )
    old_manifest = _drive_run_then_resume(
        old_path_dir, config_path=old_cfg, design_id=design_id,
    )

    assert _gds_content_hash(new_manifest) == _gds_content_hash(old_manifest)


# --------------------------------------------------------------------------- #
# AC #2 — audit log records M19 events only when the flag is True
# --------------------------------------------------------------------------- #
def _audit_promoted_stages(audit_path: Path) -> set[str]:
    """Return the set of stage values that appear in ARTIFACT_PROMOTED events."""
    log = SqliteAuditLog(db_path=audit_path, hmac_key=HMAC_KEY)
    try:
        events = list(log.events(design_id="f19-11-audit"))
    finally:
        log.close()
    return {
        e.payload["stage"]
        for e in events
        if e.event_type is EventType.ARTIFACT_PROMOTED and "stage" in e.payload
    }


def test_audit_log_records_m19_events_only_when_flag_true(
    tmp_path: Path, patch_router: StubBackend,
) -> None:
    new_path_dir = tmp_path / "new_path"
    old_path_dir = tmp_path / "old_path"

    new_cfg = _write_routing_config(
        new_path_dir, use_test_first_workflow=True,
    )
    old_cfg = _write_routing_config(
        old_path_dir, use_test_first_workflow=False,
    )

    _drive_run_then_resume(
        new_path_dir, config_path=new_cfg, design_id="f19-11-audit",
    )
    new_stages = _audit_promoted_stages(new_path_dir / "audit.sqlite")

    _drive_run_then_resume(
        old_path_dir, config_path=old_cfg, design_id="f19-11-audit",
    )
    old_stages = _audit_promoted_stages(old_path_dir / "audit.sqlite")

    m19_values = {
        Stage.CONTRACT.value, Stage.ORACLE.value,
        Stage.ASSERTIONS.value, Stage.ORACLE_VERIFICATION.value,
    }
    assert m19_values <= new_stages, (
        f"M19 stages missing under flag True: {m19_values - new_stages!r}"
    )
    assert m19_values.isdisjoint(old_stages), (
        f"M19 stages leaked under flag False: {m19_values & old_stages!r}"
    )


def test_spine_still_completes_under_flag_false(
    tmp_path: Path, patch_router: StubBackend,
) -> None:
    """Flag-off path still reaches GDSII (the pre-F19.7 behaviour)."""
    cfg = _write_routing_config(tmp_path, use_test_first_workflow=False)
    manifest_path = _drive_run_then_resume(
        tmp_path, config_path=cfg, design_id="f19-11-off",
    )
    # Manifest exists + has a GDS root ref → spine reached COMPLETED.
    assert manifest_path.exists()
    assert _gds_content_hash(manifest_path).startswith("sha256:")


# --------------------------------------------------------------------------- #
# AC #3 — switching the flag mid-session raises CLIError
# --------------------------------------------------------------------------- #
def test_cmd_resume_rejects_mid_session_flag_flip(
    tmp_path: Path, patch_router: StubBackend,
) -> None:
    """Run with the flag True, then attempt resume with it False → CLIError."""
    run_dir = tmp_path / "run"
    on_cfg = _write_routing_config(run_dir, use_test_first_workflow=True)
    design_id = "f19-11-flip"

    cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id=design_id,
        spec_path=COUNTER_SPEC, name="counter",
        config_path=on_cfg,
    ))

    # Flip the flag in the config file.
    off_text = on_cfg.read_text().replace(
        "use_test_first_workflow: true",
        "use_test_first_workflow: false",
    )
    on_cfg.write_text(off_text)

    with pytest.raises(CLIError, match=r"use_test_first_workflow"):
        cmd_resume(_args(
            cmd="resume", run_dir=run_dir, design_id=design_id,
            config_path=on_cfg,
        ))


# --------------------------------------------------------------------------- #
# Plumbing — settings + factory wiring
# --------------------------------------------------------------------------- #
def test_routing_settings_default_is_false(tmp_path: Path) -> None:
    """A YAML that omits the flag defaults the field to ``False``."""
    cfg = tmp_path / "minimal.yaml"
    cfg.write_text(
        "constraints:\n"
        "  pdk: sky130A\n"
        "routing:\n"
        "  registry:\n"
        "    m:\n"
        "      provider: stub\n"
        "      model: m\n"
        "  loops:\n"
        "    inner: {model: m, temperature: 0.0, n: 1}\n"
        "    outer: {model: m, temperature: 0.0, n: 1}\n"
        "  tasks: {}\n",
    )
    s = Settings.from_yaml(cfg)
    assert s.routing.use_test_first_workflow is False


def test_routing_settings_yaml_roundtrips_true_and_false(
    tmp_path: Path,
) -> None:
    """Both YAML boolean values round-trip via Settings.from_yaml."""
    base = (
        "constraints:\n  pdk: sky130A\n"
        "routing:\n"
        "  use_test_first_workflow: {flag}\n"
        "  registry:\n    m: {{provider: stub, model: m}}\n"
        "  loops:\n    inner: {{model: m, temperature: 0.0, n: 1}}\n"
        "    outer: {{model: m, temperature: 0.0, n: 1}}\n"
        "  tasks: {{}}\n"
    )
    for value, expected in (("true", True), ("false", False)):
        cfg = tmp_path / f"flag_{value}.yaml"
        cfg.write_text(base.format(flag=value))
        s = Settings.from_yaml(cfg)
        assert s.routing.use_test_first_workflow is expected


def test_build_demo_stage_context_unsets_m19_agents_when_flag_false(
    tmp_path: Path,
) -> None:
    """``build_demo_stage_context(use_test_first_workflow=False)`` leaves
    the four M19 agent fields + ``reflection_router`` as ``None``."""
    from chip_agent.cli_stubs import build_demo_stage_context
    from chip_agent.design_state import (
        DesignConstraints,
        DesignPlan,
        ModuleDecl,
        Port,
        Provenance,
        Spec,
    )
    from chip_agent.obs.audit_log import SqliteAuditLog
    from chip_agent.obs.tracing import NoopTracer
    from chip_agent.store.sqlite_store import SqliteArtifactStore

    cfg = make_routing_config(tmp_path)
    router, _ = make_test_router(config_path=cfg)
    store = SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    )
    audit = SqliteAuditLog(
        db_path=tmp_path / "audit.sqlite", hmac_key=HMAC_KEY,
    )

    constraints = DesignConstraints(pdk="sky130A")
    spec = Spec(
        artifact_id="d.spec", design_id="d",
        raw_text="counter", normalized="counter",
        requirements=[], constraints=constraints,
        provenance=Provenance(produced_by=Stage.SPEC, agent="test"),
    )
    store.put(spec)
    module = ModuleDecl(
        module_id="counter", name="counter", description="d",
        ports=[Port(name="clk", direction="in", width=1)],
    )
    plan = DesignPlan(
        artifact_id="d.plan", design_id="d",
        top_module_id="counter", modules=[module],
        provenance=Provenance(produced_by=Stage.PLAN, agent="test"),
    )
    store.put(plan)

    ctx = build_demo_stage_context(
        store=store, audit_log=audit, tracer=NoopTracer(),
        design_id="d", top_module="counter",
        constraints=constraints, router=router, spec=spec, plan=plan,
        use_test_first_workflow=False,
    )
    assert ctx.contract_extractor is None
    assert ctx.oracle_gen is None
    assert ctx.assertion_gen is None
    assert ctx.oracle_verifier is None
    assert ctx.reflection_router is None

    audit.close()
    store.close()
