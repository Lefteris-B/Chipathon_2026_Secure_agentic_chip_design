"""F10.2 — RTL outer loop uses real cocotb on ``--sandbox docker``.

Pins the simulator swap inside :func:`build_demo_stage_context`:

* ``--sandbox stub`` keeps the in-memory :class:`_DeterministicSimulator`.
  The persisted ``SimulationResult.checker`` carries the
  ``deterministic-sim`` marker so tests can tell which path ran.
* ``--sandbox docker`` builds a real
  :class:`chip_agent.tools.cocotb_sim.SimulationService` over the
  IIC-OSIC-TOOLS container; the persisted ``SimulationResult.checker``
  names ``cocotb+<simulator>`` and carries the container digest.

The docker test is opt-in via the existing ``pytest.mark.docker``
marker (auto-skipped when the daemon or pinned image isn't local).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chip_agent.cli import RunArgs, cmd_run
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import (
    StubBackend,
    make_routing_config,
    make_test_router,
)

HMAC_KEY = b"f10-2-cocotb-hmac-key"


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    spec = tmp_path / "counter.md"
    spec.write_text((Path(__file__).parent.parent / "specs" / "counter.md").read_text())
    return spec


@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


@pytest.fixture
def patch_router(
    monkeypatch: pytest.MonkeyPatch,
    routing_config: Path,
) -> StubBackend:
    backend = StubBackend()
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr("chip_agent.cli._resolve_router", lambda _args, *, settings: router)
    return backend


def _args(*, spec_file: Path, run_dir: Path, config: Path, design_id: str,
          sandbox: str = "stub") -> RunArgs:
    return RunArgs(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id=design_id, hmac_key=HMAC_KEY,
        config_path=config, sandbox_kind=sandbox,
    )


# --------------------------------------------------------------------------- #
# AC strand 1 — ``--sandbox stub`` uses the deterministic simulator.
# --------------------------------------------------------------------------- #
def test_stub_sandbox_uses_deterministic_simulator(
    spec_file: Path, tmp_path: Path, routing_config: Path,
    patch_router: StubBackend,
) -> None:
    """Stub sandbox runs the in-memory ``_DeterministicSimulator`` (always passes)."""
    run_dir = tmp_path / "run"
    cmd_run(_args(
        spec_file=spec_file, run_dir=run_dir,
        config=routing_config, design_id="d-stub-sim", sandbox="stub",
    ))

    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        sim = store.get_by_id("d-stub-sim.counter.sim")

    assert sim.passed is True
    assert sim.checker is not None
    assert sim.checker.name == "deterministic-sim"
    assert sim.checker.version == "stub"
    # Stub path never hits a container, so no container_digest is recorded.
    assert sim.checker.container_digest is None


# --------------------------------------------------------------------------- #
# AC strand 2 — ``--sandbox docker`` runs real cocotb in the container.
# --------------------------------------------------------------------------- #
@pytest.mark.docker
def test_docker_sandbox_uses_real_cocotb_sim(
    spec_file: Path, tmp_path: Path, patch_router: StubBackend,
) -> None:
    """F10.2 AC: docker sandbox runs real cocotb inside the IIC container.

    The persisted ``SimulationResult.checker`` names ``cocotb+verilator``
    (or whichever sim the image bundles by default) AND records the
    container digest. The deterministic-sim marker MUST NOT appear.
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

    run_dir = tmp_path / "run"
    args = RunArgs(
        cmd="run", spec_path=spec_file, name="counter", run_dir=run_dir,
        design_id="d-docker-sim", hmac_key=HMAC_KEY,
        sandbox_kind="docker", config_path=cfg,
    )
    cmd_run(args)

    with SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    ) as store:
        sim = store.get_by_id("d-docker-sim.counter.sim")

    assert sim.checker is not None, "expected SimulationService to stamp a checker"
    # The deterministic-sim marker is the stub path; under docker we expect
    # the real cocotb service's name.
    assert sim.checker.name != "deterministic-sim"
    assert sim.checker.name.startswith("cocotb"), (
        f"unexpected sim checker name {sim.checker.name!r}; "
        f"expected the cocotb service name"
    )
    assert sim.checker.container_digest == settings.sandbox.image_digest
