"""F10.3 — LibreLane closure on the counter spec under sky130.

Opt-in real-flow smoke. Runs the full ``chip-agent run`` + ``chip-agent
resume`` against the pinned IIC-OSIC-TOOLS container with
``configs/demo-counter.yaml`` and asserts the spine reaches
``DesignStatus.COMPLETED`` with a non-empty GDS that has a valid GDSII
HEADER record.

Enabled only when ``CHIP_AGENT_LIBRELANE_CLOSURE=1`` AND the docker
daemon + pinned image are available — the marker registration in
``tests/conftest.py`` handles both gates.

Closure config (the F10.3 tuning):

* ``target_utilization=0.3`` — sky130 + the counter's 245 instances
  legalise at this density; 0.5+ trips OpenROAD's DPL-0036 in
  Resizer Timing Optimizations (post-CTS).
* ``time_limit_s=1800`` (config-level) — gives the full flow
  ~30 minutes to close; the counter actually finishes in ~60s on a
  modern host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chip_agent.cli import RunArgs, cmd_resume, cmd_run
from chip_agent.design_state import DesignStatus, Stage
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import StubBackend, make_routing_config, make_test_router

REPO_ROOT = Path(__file__).resolve().parent.parent
COUNTER_SPEC = REPO_ROOT / "specs" / "counter.md"
DEMO_CFG = REPO_ROOT / "configs" / "demo-counter.yaml"
HMAC_KEY = b"f10-3-closure-hmac-key"


@pytest.mark.docker
@pytest.mark.librelane_closure
def test_counter_closes_through_librelane_to_real_gds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives ``specs/counter.md`` end-to-end against the real container.

    Asserts: cmd_run reaches the human gate at SIGNOFF (RTL + SYNTH +
    PHYSICAL closed); cmd_resume completes through GDSII; the persisted
    GDS body is non-empty and starts with the standard GDS HEADER record
    (``\\x00\\x06\\x00\\x02``).
    """
    from chip_agent.settings import Settings
    from chip_agent.tools.image import image_locally_available
    if not DEMO_CFG.exists():
        pytest.skip(f"{DEMO_CFG} missing")
    settings = Settings.from_yaml(DEMO_CFG)
    if not image_locally_available(settings.sandbox):
        pytest.skip(
            f"{settings.sandbox.image}:{settings.sandbox.image_tag} not pulled locally",
        )

    routing_cfg = make_routing_config(tmp_path, filename="routing-stub.yaml")
    backend = StubBackend()
    router, _ = make_test_router(config_path=routing_cfg, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router", lambda _args, *, settings: router,
    )

    run_dir = tmp_path / "run"
    paused = cmd_run(RunArgs(
        cmd="run", spec_path=COUNTER_SPEC, name="counter",
        run_dir=run_dir, design_id="closure",
        hmac_key=HMAC_KEY, sandbox_kind="docker", config_path=DEMO_CFG,
    ))
    # RTL + SYNTH + PHYSICAL must all have closed before the human gate.
    assert paused.paused_state.status is DesignStatus.AWAITING_HUMAN
    assert paused.paused_state.current_stage is Stage.SIGNOFF
    phys_ss = paused.paused_state.stages[Stage.PHYSICAL]
    assert phys_ss.head is not None, (
        f"PHYSICAL did not promote a head — closure failed: status={phys_ss.status!r}"
    )

    out = cmd_resume(RunArgs(
        cmd="resume", spec_path=None, name=None,
        run_dir=run_dir, design_id="closure",
        hmac_key=HMAC_KEY, sandbox_kind="docker", config_path=DEMO_CFG,
    ))
    assert out.final_state.status is DesignStatus.COMPLETED
    assert out.final_state.current_stage is Stage.GDSII

    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        gds_art = store.get_by_id("closure.counter.gds")
        gds_bytes = store.get_blob(gds_art.gds)
    assert len(gds_bytes) > 1024, (
        f"GDS body too small to be a real layout: {len(gds_bytes)} bytes"
    )
    # GDSII HEADER record: 2-byte length (0x0006) + 2-byte record (HEADER=0x0002).
    assert gds_bytes[:4] == b"\x00\x06\x00\x02", (
        f"GDS body lacks the HEADER record: first 8 bytes = {gds_bytes[:8]!r}"
    )
