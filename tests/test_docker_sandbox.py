"""F8.2 — DockerSandbox + image-pin verify integration tests.

These tests exercise the real docker daemon against a small test image
(``alpine:3.20``), then run the same wiring against the pinned IIC-OSIC-TOOLS
image **only if it's locally available**. CI typically runs the alpine
portion (the conftest's ``docker`` marker auto-skips when no daemon is
reachable); the IIC-OSIC-TOOLS portion auto-skips unless the multi-GB
image was pre-pulled.

What's verified:

* :func:`resolve_digest` + :func:`verify_pinned_image` round-trip
  against the real docker daemon.
* :func:`image_locally_available` reports True for an available image
  and False for an obviously-missing one.
* :class:`DockerSandbox` (alias for :class:`SandboxRunner`) executes a
  trivial command inside the pinned image and surfaces the recorded
  ``image_digest`` on the runner.
* For the IIC-OSIC-TOOLS image (when present): ``yosys -V`` returns
  cleanly inside ``--network=none``, demonstrating the F8.2 AC that
  the real tool unit tests work against the real container.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from chip_agent.settings import SandboxSettings
from chip_agent.tools.image import (
    ImageProvisioningError,
    image_locally_available,
    resolve_digest,
    verify_pinned_image,
)
from chip_agent.tools.sandbox import DockerSandbox, SandboxRunner

ALPINE_IMAGE = "alpine"
ALPINE_TAG = "3.20"


IIC_IMAGE = os.environ.get("CHIP_AGENT_TEST_IIC_IMAGE", "hpretl/iic-osic-tools")
IIC_TAG = os.environ.get("CHIP_AGENT_TEST_IIC_TAG", "chipathon26")
# The IIC-OSIC-TOOLS image's script entrypoint needs ``--skip`` to
# short-circuit the VNC bootstrap and exec the supplied command.
IIC_COMMAND_PREFIX: list[str] = ["--skip"]


def _skip_unless_image_present(image: str, tag: str) -> SandboxSettings:
    """Return pinned settings; skip the test if the image isn't local."""
    settings = SandboxSettings(image=image, image_tag=tag)
    if not image_locally_available(settings):
        pytest.skip(
            f"{image}:{tag} not pulled locally — "
            f"run `docker pull {image}:{tag}` to enable",
        )
    return settings


# --------------------------------------------------------------------------- #
# DockerSandbox alias surface
# --------------------------------------------------------------------------- #
def test_docker_sandbox_is_sandbox_runner_alias() -> None:
    """F8.2 names ``DockerSandbox`` for the named class; verify the alias."""
    assert DockerSandbox is SandboxRunner


def test_docker_sandbox_exposes_image_digest_property() -> None:
    DIGEST = "sha256:" + "a" * 64
    settings = SandboxSettings(image="img", image_tag="v1", image_digest=DIGEST)
    sandbox = DockerSandbox(settings)
    assert sandbox.image_digest == DIGEST


def test_docker_sandbox_image_digest_none_when_unpinned() -> None:
    settings = SandboxSettings(image="img", image_tag="v1")
    sandbox = DockerSandbox(settings)
    assert sandbox.image_digest is None


# --------------------------------------------------------------------------- #
# Real docker — alpine smoke (auto-skipped when no daemon)
# --------------------------------------------------------------------------- #
@pytest.mark.docker
def test_resolve_then_verify_matches_against_real_docker() -> None:
    """End-to-end: resolve a digest, pin it, then verify it round-trips."""
    digest = resolve_digest(ALPINE_IMAGE, ALPINE_TAG)
    pinned = SandboxSettings(
        image=ALPINE_IMAGE, image_tag=ALPINE_TAG, image_digest=digest,
    )
    info = verify_pinned_image(pinned)
    assert info.matches
    assert info.actual_digest == digest


@pytest.mark.docker
def test_image_locally_available_against_real_docker_for_alpine() -> None:
    digest = resolve_digest(ALPINE_IMAGE, ALPINE_TAG)
    pinned = SandboxSettings(
        image=ALPINE_IMAGE, image_tag=ALPINE_TAG, image_digest=digest,
    )
    assert image_locally_available(pinned)


@pytest.mark.docker
def test_image_locally_available_false_for_known_missing_image() -> None:
    """An obviously-absent image returns False rather than raising."""
    fake = SandboxSettings(
        image=f"chip-agent-nonexistent-{uuid.uuid4().hex[:8]}",
        image_tag="never-pushed",
    )
    assert not image_locally_available(fake)


@pytest.mark.docker
def test_verify_pinned_image_detects_mismatched_digest() -> None:
    """The verifier should reject a digest that doesn't match docker's view."""
    wrong = "sha256:" + "b" * 64
    settings = SandboxSettings(
        image=ALPINE_IMAGE, image_tag=ALPINE_TAG, image_digest=wrong,
    )
    with pytest.raises(ImageProvisioningError, match="pinned digest mismatch"):
        verify_pinned_image(settings)


@pytest.mark.docker
def test_docker_sandbox_runs_command_in_real_image(tmp_path: Path) -> None:
    """Smoke test: DockerSandbox can run a trivial command end-to-end."""
    digest = resolve_digest(ALPINE_IMAGE, ALPINE_TAG)
    settings = SandboxSettings(
        image=ALPINE_IMAGE, image_tag=ALPINE_TAG, image_digest=digest,
        cpu_limit=1.0, memory_limit_gb=0.25, time_limit_s=20,
    )
    sandbox = DockerSandbox(settings)
    assert sandbox.image_digest == digest

    tr = sandbox.run(
        ["sh", "-c", "echo $CHIP_AGENT_F8_2_PROBE > /work/out.txt"],
        mount=tmp_path,
        extra_env={"CHIP_AGENT_F8_2_PROBE": "ok"},
    )
    assert tr.returncode == 0, tr.stderr
    assert (tmp_path / "out.txt").read_text().strip() == "ok"


# --------------------------------------------------------------------------- #
# Real IIC-OSIC-TOOLS image (auto-skipped unless pre-pulled)
# --------------------------------------------------------------------------- #
@pytest.mark.docker
def test_iic_image_digest_matches_docker_inspect() -> None:
    """F8.2 AC: recorded ``container_digest`` matches ``docker inspect``."""
    settings = _skip_unless_image_present(IIC_IMAGE, IIC_TAG)
    digest = resolve_digest(IIC_IMAGE, IIC_TAG)
    pinned = settings.model_copy(update={"image_digest": digest})
    info = verify_pinned_image(pinned)
    assert info.matches


@pytest.mark.docker
def test_iic_image_yosys_runs_inside_pinned_container(tmp_path: Path) -> None:
    """F8.2 AC: a real tool (Yosys) runs cleanly in the pinned IIC-OSIC-TOOLS."""
    base = _skip_unless_image_present(IIC_IMAGE, IIC_TAG)
    digest = resolve_digest(IIC_IMAGE, IIC_TAG)
    settings = base.model_copy(update={
        "image_digest": digest,
        "time_limit_s": 60,
        "command_prefix": IIC_COMMAND_PREFIX,
    })
    sandbox = DockerSandbox(settings)
    tr = sandbox.run(["yosys", "-V"], mount=tmp_path)
    assert tr.returncode == 0, tr.stderr
    assert "yosys" in (tr.stdout + tr.stderr).lower()
