"""F12.6 — FSM traffic-light demo proves the spine handles a state-machine shape.

Drives ``specs/fsm_traffic_light.md`` through the CLI (``cmd_run`` +
``cmd_resume``) with the F12.6 stub responses and asserts the same spine
that closes counter + shift_register also closes a three-state Moore FSM
with output decode.

The committed
``tests/fixtures/fsm_traffic_light_golden_manifest.json`` mirrors the
counter golden's role: the F8.3-style reproducibility AC for the FSM
spec shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chip_agent.cli import RunArgs, cmd_resume, cmd_run
from chip_agent.design_state import ArtifactKind, DesignStatus, Stage
from chip_agent.obs.replay import RunManifest, compare_to_manifest
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import (
    FSM_TRAFFIC_LIGHT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FSM_SPEC = REPO_ROOT / "specs" / "fsm_traffic_light.md"
GOLDEN_MANIFEST_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "fsm_traffic_light_golden_manifest.json"
)
GOLDEN_DESIGN_ID = "fsm-traffic-light-demo-golden"
HMAC_KEY = b"f12-6-fsm-traffic-light-demo-hmac-key"


@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


@pytest.fixture
def patch_router(
    monkeypatch: pytest.MonkeyPatch, routing_config: Path,
) -> StubBackend:
    backend = StubBackend(matchers=FSM_TRAFFIC_LIGHT_RESPONSES)
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router", lambda _args, *, settings: router,
    )
    return backend


def _args(
    *,
    cmd: str,
    run_dir: Path,
    design_id: str,
    config_path: Path,
    spec_path: Path | None = None,
    name: str | None = None,
) -> RunArgs:
    return RunArgs(
        cmd=cmd, spec_path=spec_path, name=name, run_dir=run_dir,
        design_id=design_id, hmac_key=HMAC_KEY, config_path=config_path,
    )


def _drive_demo(
    run_dir: Path, *, config_path: Path, design_id: str = GOLDEN_DESIGN_ID,
) -> RunManifest:
    cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id=design_id,
        spec_path=FSM_SPEC, name="fsm_traffic_light",
        config_path=config_path,
    ))
    out = cmd_resume(_args(
        cmd="resume", run_dir=run_dir, design_id=design_id,
        config_path=config_path,
    ))
    return out.manifest


# --------------------------------------------------------------------------- #
# AC strand 1 — fresh run reaches COMPLETED through every stage.
# --------------------------------------------------------------------------- #
def test_fsm_traffic_light_demo_produces_completed_design(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    run_dir = tmp_path / "run"
    cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id="fsm-d0",
        spec_path=FSM_SPEC, name="fsm_traffic_light",
        config_path=routing_config,
    ))
    out = cmd_resume(_args(
        cmd="resume", run_dir=run_dir, design_id="fsm-d0",
        config_path=routing_config,
    ))
    assert out.final_state.status is DesignStatus.COMPLETED
    assert out.final_state.current_stage is Stage.GDSII
    assert out.gds_ref.artifact_id == "fsm-d0.fsm_traffic_light.gds"


# --------------------------------------------------------------------------- #
# AC strand 2 — DesignPlan carries the four expected ports on one module.
# --------------------------------------------------------------------------- #
def test_fsm_traffic_light_plan_has_correct_ports(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    run_dir = tmp_path / "run"
    out = cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id="fsm-plan",
        spec_path=FSM_SPEC, name="fsm_traffic_light",
        config_path=routing_config,
    ))
    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        plan = store.get(out.plan_ref)
    assert len(plan.modules) == 1
    module = plan.modules[0]
    assert module.module_id == "fsm_traffic_light"
    port_names = {p.name for p in module.ports}
    assert port_names == {"clk", "rst_n", "tick", "state"}, (
        f"unexpected port set: {port_names!r}"
    )
    by_name = {p.name: p for p in module.ports}
    assert by_name["clk"].direction == "in" and by_name["clk"].width == 1
    assert by_name["rst_n"].direction == "in" and by_name["rst_n"].width == 1
    assert by_name["tick"].direction == "in" and by_name["tick"].width == 1
    assert by_name["state"].direction == "out" and by_name["state"].width == 2


# --------------------------------------------------------------------------- #
# AC strand 3 — committed golden manifest reproduces against a fresh run.
# --------------------------------------------------------------------------- #
def test_committed_golden_manifest_exists_and_is_valid_json() -> None:
    assert GOLDEN_MANIFEST_PATH.exists(), (
        f"committed golden manifest missing at {GOLDEN_MANIFEST_PATH}"
    )
    payload = json.loads(GOLDEN_MANIFEST_PATH.read_text())
    assert payload["design_id"] == GOLDEN_DESIGN_ID
    assert payload["root_ref"]["kind"] == "gdsii"


def test_fsm_traffic_light_golden_manifest_reproduces(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    fresh = _drive_demo(tmp_path / "run", config_path=routing_config)
    golden = RunManifest.model_validate_json(GOLDEN_MANIFEST_PATH.read_text())
    assert fresh.design_id == golden.design_id
    assert fresh.root_ref.content_hash == golden.root_ref.content_hash, (
        "GDS content_hash drifted from golden — stub responses or templates changed?"
    )
    fresh_by_id = {f"{e.artifact_id}@v{e.version}": e for e in fresh.entries}
    golden_by_id = {f"{e.artifact_id}@v{e.version}": e for e in golden.entries}
    assert set(fresh_by_id) == set(golden_by_id)
    for key, golden_entry in golden_by_id.items():
        fresh_entry = fresh_by_id[key]
        assert fresh_entry.content_hash == golden_entry.content_hash, (
            f"{key}: content_hash diverged from golden"
        )


def test_fsm_traffic_light_golden_verifies_against_fresh_store(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    _drive_demo(tmp_path / "run", config_path=routing_config)
    golden = RunManifest.model_validate_json(GOLDEN_MANIFEST_PATH.read_text())
    with SqliteArtifactStore(
        db_path=tmp_path / "run" / "store.sqlite",
        content_dir=tmp_path / "run" / "content",
    ) as store:
        diff = compare_to_manifest(store, golden)
    assert diff.reproducible


# --------------------------------------------------------------------------- #
# AC strand 4 — every artifact kind from the counter demo also lands here.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", [
    ArtifactKind.SPEC, ArtifactKind.PLAN, ArtifactKind.RTL,
    ArtifactKind.NETLIST, ArtifactKind.LAYOUT, ArtifactKind.GDSII,
])
def test_each_demo_artifact_kind_is_in_golden_manifest(kind: ArtifactKind) -> None:
    golden = RunManifest.model_validate_json(GOLDEN_MANIFEST_PATH.read_text())
    kinds = {e.kind for e in golden.entries}
    assert kind in kinds
