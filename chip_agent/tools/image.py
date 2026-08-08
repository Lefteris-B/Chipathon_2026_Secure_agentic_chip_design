"""IIC-OSIC-TOOLS image pinning & per-tool version reporting (F0.3).

The toolchain image is the deterministic anchor for every sandboxed tool run
(see ``docs/tool_contracts.md``). This module resolves a *release tag* to a
content-addressed *digest* and records both into a YAML config, so every
downstream invocation can reference ``image@sha256:...`` instead of a
mutable tag.

Docker interactions are funnelled through the :class:`DockerRunner` Protocol so
tests inject a fake — pulling the real (multi-GB) image is not required.

CLI:
    python -m chip_agent.tools.image pin --config <yaml> [--image X --tag Y]
    python -m chip_agent.tools.image metadata --image-ref <image@digest>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from chip_agent.settings import SandboxSettings, Settings

__all__ = [
    "DEFAULT_TOOL_METADATA_PATH",
    "DockerRunner",
    "ImageProvisioningError",
    "PinnedImageInfo",
    "SubprocessDockerRunner",
    "image_locally_available",
    "pin_image_in_config",
    "read_tool_metadata",
    "resolve_digest",
    "verify_pinned_image",
]


DEFAULT_TOOL_METADATA_PATH = "/tool_metadata.yml"
_DIGEST_PREFIX = "sha256:"


class ImageProvisioningError(RuntimeError):
    """Raised when docker is unavailable, returns non-zero, or output is malformed."""


class DockerRunner(Protocol):
    """Minimal docker CLI surface this module needs."""

    def run(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessDockerRunner:
    """Default :class:`DockerRunner` backed by the local ``docker`` binary."""

    def __init__(self, docker_bin: str = "docker") -> None:
        self.docker_bin = docker_bin

    def run(
        self, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.docker_bin, *args]
        try:
            return subprocess.run(
                cmd, check=check, capture_output=True, text=True
            )
        except FileNotFoundError as e:
            raise ImageProvisioningError(
                f"docker binary not found ({self.docker_bin!r}); install Docker"
            ) from e
        except subprocess.CalledProcessError as e:
            raise ImageProvisioningError(
                f"docker {' '.join(args)} failed (rc={e.returncode}):\n{e.stderr or e.stdout}"
            ) from e


def _default_runner(runner: DockerRunner | None) -> DockerRunner:
    return runner if runner is not None else SubprocessDockerRunner()


def resolve_digest(
    image: str, tag: str, *, runner: DockerRunner | None = None
) -> str:
    """Pull ``image:tag`` and return its content-addressed ``sha256:...`` digest.

    Implementation: ``docker pull`` then ``docker image inspect --format``
    over ``.RepoDigests``. Raises :class:`ImageProvisioningError` if docker
    is missing, the pull fails, or the digest output is unrecognized.
    """
    r = _default_runner(runner)
    ref = f"{image}:{tag}"
    r.run(["pull", ref])
    cp = r.run(
        ["image", "inspect", "--format={{index .RepoDigests 0}}", ref]
    )
    out = cp.stdout.strip()
    if "@" not in out:
        raise ImageProvisioningError(
            f"unexpected `docker image inspect` output for {ref}: {out!r}"
        )
    digest = out.split("@", 1)[1]
    if not digest.startswith(_DIGEST_PREFIX):
        raise ImageProvisioningError(
            f"digest for {ref} does not start with 'sha256:': {digest!r}"
        )
    return digest


@dataclass(frozen=True)
class PinnedImageInfo:
    """Result of :func:`verify_pinned_image`.

    ``expected_digest`` is what :class:`SandboxSettings` recorded;
    ``actual_digest`` is what ``docker image inspect`` reports right
    now. ``matches`` is the F8.2 invariant the CLI's preflight enforces.
    """

    image: str
    tag: str | None
    expected_digest: str
    actual_digest: str

    @property
    def matches(self) -> bool:
        return self.expected_digest == self.actual_digest


def image_locally_available(
    settings: SandboxSettings, *, runner: DockerRunner | None = None,
) -> bool:
    """True iff ``docker image inspect <image_ref>`` succeeds on the host.

    Used as a preflight: pulling a multi-GB IIC-OSIC-TOOLS image just to
    detect availability would block tests + CI for minutes. This call
    returns immediately and falls back to ``False`` on any docker error.
    """
    r = _default_runner(runner)
    try:
        r.run(["image", "inspect", settings.image_ref])
    except ImageProvisioningError:
        return False
    return True


def verify_pinned_image(
    settings: SandboxSettings, *, runner: DockerRunner | None = None,
) -> PinnedImageInfo:
    """Confirm the locally-resolved digest matches ``settings.image_digest``.

    Reads ``.RepoDigests[0]`` via ``docker image inspect`` on the pinned
    image and returns a :class:`PinnedImageInfo`. Raises
    :class:`ImageProvisioningError` if no digest is pinned in settings,
    if docker is unavailable, or if the recorded and live digests
    disagree — the F8.2 AC.

    Does NOT pull the image. Pair with :func:`image_locally_available`
    if the caller wants a soft preflight first.
    """
    if settings.image_digest is None:
        raise ImageProvisioningError(
            "sandbox.image_digest is unpinned; run `python -m chip_agent.tools.image pin` first",
        )
    r = _default_runner(runner)
    ref = f"{settings.image}:{settings.image_tag}" if settings.image_tag else settings.image
    cp = r.run(["image", "inspect", "--format={{index .RepoDigests 0}}", ref])
    out = cp.stdout.strip()
    if "@" not in out:
        raise ImageProvisioningError(
            f"unexpected `docker image inspect` output for {ref}: {out!r}",
        )
    actual = out.split("@", 1)[1]
    if not actual.startswith(_DIGEST_PREFIX):
        raise ImageProvisioningError(
            f"digest for {ref} does not start with 'sha256:': {actual!r}",
        )
    info = PinnedImageInfo(
        image=settings.image,
        tag=settings.image_tag,
        expected_digest=settings.image_digest,
        actual_digest=actual,
    )
    if not info.matches:
        raise ImageProvisioningError(
            f"pinned digest mismatch for {ref}: "
            f"settings={info.expected_digest}, docker={info.actual_digest}; "
            f"re-pin or restore the recorded tag",
        )
    return info


def read_tool_metadata(
    image_ref: str,
    *,
    runner: DockerRunner | None = None,
    path: str = DEFAULT_TOOL_METADATA_PATH,
) -> dict[str, Any]:
    """Return parsed ``tool_metadata.yml`` from inside the image.

    Runs ``docker run --rm --entrypoint=cat <image_ref> <path>``. The image
    reference should already be content-addressed (``image@sha256:...``) so the
    read is reproducible.
    """
    r = _default_runner(runner)
    cp = r.run(["run", "--rm", "--entrypoint=cat", image_ref, path])
    try:
        data = yaml.safe_load(cp.stdout) or {}
    except yaml.YAMLError as e:
        raise ImageProvisioningError(
            f"could not parse {path} from {image_ref}: {e}"
        ) from e
    if not isinstance(data, dict):
        raise ImageProvisioningError(
            f"{path} from {image_ref} is not a YAML mapping (got {type(data).__name__})"
        )
    return data


def pin_image_in_config(
    config_path: Path | str,
    *,
    image: str | None = None,
    tag: str | None = None,
    runner: DockerRunner | None = None,
) -> SandboxSettings:
    """Resolve the digest for ``image:tag`` and write it back into ``config_path``.

    If ``image`` / ``tag`` are omitted, the values already present under
    ``sandbox`` in the YAML file are used. Returns the updated
    :class:`SandboxSettings` block; the file on disk is rewritten with the
    new ``image_digest`` (note: comments are not preserved by ``yaml.safe_dump``;
    pin a user-local config rather than the shipped default).
    """
    p = Path(config_path)
    if not p.exists():
        raise ImageProvisioningError(f"config file does not exist: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ImageProvisioningError(
            f"config file {p} must be a YAML mapping at the top level"
        )
    sb = raw.setdefault("sandbox", {})
    if not isinstance(sb, dict):
        raise ImageProvisioningError(
            f"config file {p}: 'sandbox' section must be a mapping, got {type(sb).__name__}"
        )
    if image is not None:
        sb["image"] = image
    if tag is not None:
        sb["image_tag"] = tag
    if not sb.get("image") or not sb.get("image_tag"):
        raise ImageProvisioningError(
            "pin_image_in_config requires sandbox.image and sandbox.image_tag "
            "(pass --image/--tag or set them in the YAML)"
        )
    sb["image_digest"] = resolve_digest(sb["image"], sb["image_tag"], runner=runner)
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    # Round-trip through Settings to apply the digest-shape validator.
    return Settings.from_yaml(p).sandbox


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m chip_agent.tools.image",
        description="Pin the IIC-OSIC-TOOLS image and inspect its tool metadata.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pin = sub.add_parser("pin", help="resolve image:tag -> digest, write to config")
    pin.add_argument("--config", required=True, type=Path)
    pin.add_argument("--image")
    pin.add_argument("--tag")

    meta = sub.add_parser("metadata", help="read tool_metadata.yml from the image")
    meta.add_argument("--image-ref", required=True)
    meta.add_argument("--path", default=DEFAULT_TOOL_METADATA_PATH)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "pin":
            sb = pin_image_in_config(args.config, image=args.image, tag=args.tag)
            print(f"pinned {sb.image}:{sb.image_tag} -> {sb.image_digest}")
            return 0
        if args.cmd == "metadata":
            meta = read_tool_metadata(args.image_ref, path=args.path)
            print(yaml.safe_dump(meta, sort_keys=False).rstrip())
            return 0
    except ImageProvisioningError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
