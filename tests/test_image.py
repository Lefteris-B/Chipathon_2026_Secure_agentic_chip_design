"""F0.3 acceptance: pinned digest is recorded in config; tool_metadata.yml is readable."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.settings import SandboxSettings, Settings
from chip_agent.tools.image import (
    DEFAULT_TOOL_METADATA_PATH,
    ImageProvisioningError,
    image_locally_available,
    pin_image_in_config,
    read_tool_metadata,
    resolve_digest,
    verify_pinned_image,
)

DIGEST = "sha256:" + "a" * 64
BAD_DIGEST = "sha256:" + "z" * 64        # non-hex
SHORT_DIGEST = "sha256:abc"              # too short


@dataclass
class StubRunner:
    """Canned-response DockerRunner. Records the args it was called with."""

    pull_stdout: str = ""
    inspect_stdout: str = ""
    run_stdout: str = ""
    calls: list[list[str]] = field(default_factory=list)
    fail_on: str | None = None   # raise CalledProcessError on the first arg matching

    def run(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if self.fail_on is not None and args and args[0] == self.fail_on:
            raise ImageProvisioningError(f"stub: docker {self.fail_on} failed")
        if args[0] == "pull":
            return subprocess.CompletedProcess(args, 0, self.pull_stdout, "")
        if args[0] == "image" and args[1] == "inspect":
            return subprocess.CompletedProcess(args, 0, self.inspect_stdout, "")
        if args[0] == "run":
            return subprocess.CompletedProcess(args, 0, self.run_stdout, "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_resolve_digest_parses_repo_digests() -> None:
    runner = StubRunner(inspect_stdout=f"hpretl/iic-osic-tools@{DIGEST}\n")
    got = resolve_digest("hpretl/iic-osic-tools", "2026.04", runner=runner)
    assert got == DIGEST
    # Verify it actually pulled before inspecting.
    assert runner.calls[0][:2] == ["pull", "hpretl/iic-osic-tools:2026.04"]
    assert runner.calls[1][0] == "image" and runner.calls[1][1] == "inspect"


def test_resolve_digest_rejects_malformed_inspect_output() -> None:
    runner = StubRunner(inspect_stdout="not-a-repo-digest\n")
    with pytest.raises(ImageProvisioningError) as ei:
        resolve_digest("img", "tag", runner=runner)
    assert "unexpected" in str(ei.value).lower()


def test_resolve_digest_rejects_non_sha256() -> None:
    runner = StubRunner(inspect_stdout="img@md5:abc\n")
    with pytest.raises(ImageProvisioningError) as ei:
        resolve_digest("img", "tag", runner=runner)
    assert "sha256" in str(ei.value)


def test_resolve_digest_propagates_pull_failure() -> None:
    runner = StubRunner(fail_on="pull")
    with pytest.raises(ImageProvisioningError):
        resolve_digest("img", "tag", runner=runner)


def test_read_tool_metadata_parses_yaml() -> None:
    runner = StubRunner(
        run_stdout=(
            "yosys: '0.45'\n"
            "openroad: '2.0-15960'\n"
            "magic: '8.3.503'\n"
        )
    )
    meta = read_tool_metadata("img@" + DIGEST, runner=runner)
    assert meta == {"yosys": "0.45", "openroad": "2.0-15960", "magic": "8.3.503"}
    # Confirm the docker invocation shape: `run --rm --entrypoint=cat <ref> <path>`.
    call = runner.calls[0]
    assert call[:4] == ["run", "--rm", "--entrypoint=cat", "img@" + DIGEST]
    assert call[4] == DEFAULT_TOOL_METADATA_PATH


def test_read_tool_metadata_rejects_non_mapping() -> None:
    runner = StubRunner(run_stdout="- yosys\n- openroad\n")
    with pytest.raises(ImageProvisioningError) as ei:
        read_tool_metadata("img@" + DIGEST, runner=runner)
    assert "mapping" in str(ei.value).lower()


def test_read_tool_metadata_rejects_malformed_yaml() -> None:
    runner = StubRunner(run_stdout=": : not yaml :::\n")
    with pytest.raises(ImageProvisioningError):
        read_tool_metadata("img@" + DIGEST, runner=runner)


def _seed_config(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        "sandbox:\n"
        "  image: hpretl/iic-osic-tools\n"
        "  image_tag: '2026.04'\n"
    )
    return p


def test_pin_image_in_config_records_digest(tmp_path: Path) -> None:
    cfg = _seed_config(tmp_path)
    runner = StubRunner(inspect_stdout=f"hpretl/iic-osic-tools@{DIGEST}\n")
    sb = pin_image_in_config(cfg, runner=runner)
    assert sb.image_digest == DIGEST
    assert sb.image_tag == "2026.04"
    # Round-trip the file through Settings (the AC: digest is recorded in config).
    s = Settings.from_yaml(cfg)
    assert s.sandbox.image_digest == DIGEST
    assert s.sandbox.is_pinned
    assert s.sandbox.image_ref == f"hpretl/iic-osic-tools@{DIGEST}"


def test_pin_image_in_config_respects_overrides(tmp_path: Path) -> None:
    cfg = _seed_config(tmp_path)
    runner = StubRunner(inspect_stdout=f"other/img@{DIGEST}\n")
    pin_image_in_config(cfg, image="other/img", tag="v1", runner=runner)
    # Overrides reach the docker pull call.
    assert runner.calls[0] == ["pull", "other/img:v1"]
    s = Settings.from_yaml(cfg)
    assert s.sandbox.image == "other/img"
    assert s.sandbox.image_tag == "v1"


def test_pin_image_in_config_requires_image_and_tag(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("paths: {runs_dir: runs}\n")
    runner = StubRunner()
    with pytest.raises(ImageProvisioningError) as ei:
        pin_image_in_config(cfg, runner=runner)
    assert "image" in str(ei.value) and "tag" in str(ei.value)


def test_pin_image_in_config_missing_file(tmp_path: Path) -> None:
    runner = StubRunner()
    with pytest.raises(ImageProvisioningError):
        pin_image_in_config(tmp_path / "nope.yaml", runner=runner)


def test_image_ref_prefers_digest_over_tag() -> None:
    s = Settings(
        sandbox={"image": "img", "image_tag": "v1", "image_digest": DIGEST},
    )
    assert s.sandbox.image_ref == f"img@{DIGEST}"
    assert s.sandbox.is_pinned


def test_image_ref_falls_back_to_tag_then_bare() -> None:
    s_tag = Settings(sandbox={"image": "img", "image_tag": "v1"})
    assert s_tag.sandbox.image_ref == "img:v1"
    assert not s_tag.sandbox.is_pinned

    s_bare = Settings(sandbox={"image": "img"})
    assert s_bare.sandbox.image_ref == "img"
    assert not s_bare.sandbox.is_pinned


def test_bad_digest_shape_rejected_by_settings() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as ei:
        Settings(sandbox={"image": "img", "image_digest": SHORT_DIGEST})
    assert "image_digest" in str(ei.value)
    with pytest.raises(ValidationError) as ei:
        Settings(sandbox={"image": "img", "image_digest": BAD_DIGEST})
    assert "image_digest" in str(ei.value)


# --------------------------------------------------------------------------- #
# F8.2 — verify_pinned_image + image_locally_available
# --------------------------------------------------------------------------- #
def _pinned_settings(digest: str = DIGEST) -> SandboxSettings:
    return SandboxSettings(
        image="hpretl/iic-osic-tools", image_tag="2026.04", image_digest=digest,
    )


def test_verify_pinned_image_matches() -> None:
    settings = _pinned_settings()
    runner = StubRunner(
        inspect_stdout=f"hpretl/iic-osic-tools@{DIGEST}\n",
    )
    info = verify_pinned_image(settings, runner=runner)
    assert info.matches
    assert info.expected_digest == DIGEST
    assert info.actual_digest == DIGEST
    assert info.image == "hpretl/iic-osic-tools"
    assert info.tag == "2026.04"
    # Single call: `docker image inspect` (no pull).
    assert runner.calls == [
        ["image", "inspect", "--format={{index .RepoDigests 0}}",
         "hpretl/iic-osic-tools:2026.04"],
    ]


def test_verify_pinned_image_mismatch_raises() -> None:
    settings = _pinned_settings()
    other = "sha256:" + "b" * 64
    runner = StubRunner(inspect_stdout=f"hpretl/iic-osic-tools@{other}\n")
    with pytest.raises(ImageProvisioningError, match="pinned digest mismatch"):
        verify_pinned_image(settings, runner=runner)


def test_verify_pinned_image_requires_pinned_digest() -> None:
    settings = SandboxSettings(image="hpretl/iic-osic-tools", image_tag="2026.04")
    with pytest.raises(ImageProvisioningError, match="image_digest is unpinned"):
        verify_pinned_image(settings, runner=StubRunner())


def test_verify_pinned_image_rejects_malformed_inspect_output() -> None:
    settings = _pinned_settings()
    runner = StubRunner(inspect_stdout="garbage-no-at-sign\n")
    with pytest.raises(ImageProvisioningError, match="unexpected"):
        verify_pinned_image(settings, runner=runner)


def test_verify_pinned_image_rejects_non_sha256_actual() -> None:
    settings = _pinned_settings()
    runner = StubRunner(inspect_stdout="hpretl/iic-osic-tools@md5:deadbeef\n")
    with pytest.raises(ImageProvisioningError, match="sha256"):
        verify_pinned_image(settings, runner=runner)


def test_image_locally_available_true() -> None:
    settings = _pinned_settings()
    runner = StubRunner(inspect_stdout="ok\n")
    assert image_locally_available(settings, runner=runner)
    # Single call to `image inspect` against the pinned digest ref.
    assert runner.calls == [
        ["image", "inspect", f"hpretl/iic-osic-tools@{DIGEST}"],
    ]


def test_image_locally_available_false_when_docker_errors() -> None:
    settings = _pinned_settings()
    runner = StubRunner(fail_on="image")
    assert not image_locally_available(settings, runner=runner)
