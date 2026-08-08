"""F10.5 — shift-register demo proves the spine isn't counter-shape-dependent.

Drives ``specs/shift_register.md`` through the CLI (``cmd_run`` +
``cmd_resume``) with the F10.5 stub responses (a different normalised
spec, different plan JSON, different RTL) and asserts the same spine
that closes the counter also closes a 4-bit shift register.

The committed
``tests/fixtures/shift_register_golden_manifest.json`` mirrors the
counter golden's role: the F8.3-style reproducibility AC for a
non-counter design.
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
    SHIFT_REGISTER_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIFT_REG_SPEC = REPO_ROOT / "specs" / "shift_register.md"
GOLDEN_MANIFEST_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "shift_register_golden_manifest.json"
)
GOLDEN_DESIGN_ID = "shift-register-demo-golden"
HMAC_KEY = b"f10-5-shift-register-demo-hmac-key"


@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


@pytest.fixture
def patch_router(
    monkeypatch: pytest.MonkeyPatch,
    routing_config: Path,
) -> StubBackend:
    backend = StubBackend(matchers=SHIFT_REGISTER_RESPONSES)
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr("chip_agent.cli._resolve_router", lambda _args, *, settings: router)
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
        design_id=design_id, hmac_key=HMAC_KEY,
        config_path=config_path,
    )


def _drive_demo(
    run_dir: Path,
    *,
    config_path: Path,
    design_id: str = GOLDEN_DESIGN_ID,
) -> RunManifest:
    cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id=design_id,
        spec_path=SHIFT_REG_SPEC, name="shift_register",
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
def test_shift_register_demo_produces_completed_design(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """The spine drives ``shift_register.md`` to ``DesignStatus.COMPLETED``."""
    run_dir = tmp_path / "run"
    cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id="sr-d0",
        spec_path=SHIFT_REG_SPEC, name="shift_register",
        config_path=routing_config,
    ))
    out = cmd_resume(_args(
        cmd="resume", run_dir=run_dir, design_id="sr-d0",
        config_path=routing_config,
    ))
    assert out.final_state.status is DesignStatus.COMPLETED
    assert out.final_state.current_stage is Stage.GDSII
    assert out.gds_ref.artifact_id == "sr-d0.shift_register.gds"


# --------------------------------------------------------------------------- #
# AC strand 2 — DesignPlan carries the four expected ports on one module.
# --------------------------------------------------------------------------- #
def test_shift_register_plan_has_correct_ports(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """PlannerAgent emits a one-module plan with clk/rst_n/serial_in/q."""
    run_dir = tmp_path / "run"
    out = cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id="sr-plan",
        spec_path=SHIFT_REG_SPEC, name="shift_register",
        config_path=routing_config,
    ))
    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        plan = store.get(out.plan_ref)
    assert len(plan.modules) == 1, (
        f"expected one-module plan, got {len(plan.modules)} modules"
    )
    module = plan.modules[0]
    assert module.module_id == "shift_register"
    port_names = {p.name for p in module.ports}
    assert port_names == {"clk", "rst_n", "serial_in", "q"}, (
        f"unexpected port set: {port_names!r}"
    )
    # Per-port direction + width sanity.
    by_name = {p.name: p for p in module.ports}
    assert by_name["clk"].direction == "in" and by_name["clk"].width == 1
    assert by_name["rst_n"].direction == "in" and by_name["rst_n"].width == 1
    assert by_name["serial_in"].direction == "in" and by_name["serial_in"].width == 1
    assert by_name["q"].direction == "out" and by_name["q"].width == 4


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


def test_shift_register_golden_manifest_reproduces(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """A fresh run produces byte-identical content hashes to the golden."""
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
            f"{key}: content_hash diverged from golden "
            f"(fresh={fresh_entry.content_hash}, golden={golden_entry.content_hash})"
        )
        assert fresh_entry.kind is golden_entry.kind


def test_shift_register_committed_golden_verifies_against_fresh_store(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """``compare_to_manifest`` verifies the committed golden clean."""
    _drive_demo(tmp_path / "run", config_path=routing_config)
    golden = RunManifest.model_validate_json(GOLDEN_MANIFEST_PATH.read_text())
    with SqliteArtifactStore(
        db_path=tmp_path / "run" / "store.sqlite",
        content_dir=tmp_path / "run" / "content",
    ) as store:
        diff = compare_to_manifest(store, golden)
    assert diff.reproducible, (
        f"committed golden does not verify against a fresh run: "
        f"missing={[e.artifact_id for e in diff.missing]!r}, "
        f"mismatched={[m.artifact_id for m in diff.mismatched]!r}"
    )


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


# --------------------------------------------------------------------------- #
# AC strand 5 — RTL body matches the canned shift-register text.
# --------------------------------------------------------------------------- #
def test_shift_register_rtl_body_matches_canned_response(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    """The persisted RTL is the SHIFT_REGISTER_RTL canned text."""
    from tests._routing_stub import SHIFT_REGISTER_RTL
    run_dir = tmp_path / "run"
    cmd_run(_args(
        cmd="run", run_dir=run_dir, design_id="sr-rtl",
        spec_path=SHIFT_REG_SPEC, name="shift_register",
        config_path=routing_config,
    ))
    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        rtl = store.get_by_id("sr-rtl.shift_register.rtl")
        body = store.get_blob(rtl.source).decode("utf-8")
    assert body == SHIFT_REGISTER_RTL.rstrip("\n") + "\n"
