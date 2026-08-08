"""Run a command in the pinned EDA image, isolated and bounded (F0.4).

Every tool service ultimately ends up here. The runner enforces the sandbox
properties that ``docs/tool_contracts.md`` makes invariant:

* ``--network=none``           — no egress; the PDK is baked into the image.
* ``--cpus`` / ``--memory``    — CPU + RAM caps from :class:`SandboxSettings`.
* ``--rm`` + a unique name     — container is single-use and killable on timeout.
* ``-v <mount>:/work``         — only the supplied design dir is visible.
* ``-w /work``                 — commands run with /work as cwd.
* outer timeout                — wall-clock cap; on expiry the container is killed
                                 and the result returns with ``returncode=124``.

The docker invocation is funnelled through a :class:`ProcessRunner` Protocol so
unit tests inject a stub and only the integration tests touch a real daemon.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from chip_agent.design_state import ToolRun
from chip_agent.settings import SandboxSettings

__all__ = [
    "DockerSandbox",
    "ProcessResult",
    "ProcessRunner",
    "SandboxError",
    "SandboxRunner",
    "SubprocessProcessRunner",
    "docker_daemon_available",
]


_TIMEOUT_RC = 124  # matches GNU coreutils `timeout` exit status


class SandboxError(RuntimeError):
    """Docker missing, mount invalid, or other pre-flight failure."""


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of one external-process invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class ProcessRunner(Protocol):
    """The thin subprocess seam the sandbox uses; tests inject a fake."""

    def run(self, argv: list[str], *, timeout: int | None = None) -> ProcessResult: ...


class SubprocessProcessRunner:
    """Default runner: real subprocess, captures stdout/stderr, honours timeout."""

    def run(self, argv: list[str], *, timeout: int | None = None) -> ProcessResult:
        try:
            cp = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as e:
            return ProcessResult(
                returncode=_TIMEOUT_RC,
                stdout=(e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")),
                stderr=(e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or ""))
                + f"\n[sandbox] timed out after {timeout}s",
                timed_out=True,
            )
        except FileNotFoundError as e:
            raise SandboxError(
                f"binary not found ({argv[0]!r}); is docker installed?"
            ) from e
        return ProcessResult(
            returncode=cp.returncode,
            stdout=cp.stdout,
            stderr=cp.stderr,
            timed_out=False,
        )


def docker_daemon_available(docker_bin: str = "docker") -> bool:
    """True if ``docker`` is on PATH and the daemon answers ``docker info``."""
    if shutil.which(docker_bin) is None:
        return False
    try:
        cp = subprocess.run(
            [docker_bin, "info"], capture_output=True, text=True, timeout=5, check=False
        )
        return cp.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


class SandboxRunner:
    """Runs commands inside the pinned EDA image with network/resource caps."""

    def __init__(
        self,
        sandbox: SandboxSettings,
        *,
        docker_bin: str = "docker",
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.docker_bin = docker_bin
        self._proc: ProcessRunner = process_runner or SubprocessProcessRunner()

    def run(
        self,
        cmd: list[str],
        mount: Path | str,
        *,
        time_limit_s: int | None = None,
        workdir: str = "/work",
        read_only_mount: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> ToolRun:
        """Execute ``cmd`` inside the sandboxed image.

        ``mount`` is the only host directory exposed; it becomes ``/work``
        inside the container. ``time_limit_s`` defaults to
        ``sandbox.time_limit_s`` from settings; on expiry the container is
        killed and the result returns ``returncode=124``.
        """
        if not cmd:
            raise SandboxError("cmd must be non-empty")
        mount_path = Path(mount).resolve()
        if not mount_path.exists() or not mount_path.is_dir():
            raise SandboxError(
                f"mount must be an existing directory; got {mount_path}"
            )

        timeout = time_limit_s if time_limit_s is not None else self.sandbox.time_limit_s
        container_name = f"chip-agent-{uuid.uuid4().hex[:12]}"
        argv = self._build_argv(
            container_name=container_name,
            cmd=cmd,
            mount=mount_path,
            workdir=workdir,
            read_only_mount=read_only_mount,
            extra_env=extra_env or {},
        )

        start = time.monotonic()
        result = self._proc.run(argv, timeout=timeout)
        duration = time.monotonic() - start

        if result.timed_out:
            # Best-effort container cleanup; --rm removes it once stopped.
            with contextlib.suppress(SandboxError):
                self._proc.run([self.docker_bin, "kill", container_name], timeout=5)

        return ToolRun(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            artifacts_dir=str(mount_path),
            duration_s=duration,
        )

    @property
    def image_digest(self) -> str | None:
        """F8.2 surface: the pinned image digest, if any.

        Tool services that record a :class:`~chip_agent.design_state.ToolVersion`
        already pull this via ``getattr(getattr(sandbox, "sandbox", None),
        "image_digest", None)``; exposing it directly here keeps the
        ``DockerSandbox`` name self-documenting.
        """
        return self.sandbox.image_digest

    def _build_argv(
        self,
        *,
        container_name: str,
        cmd: list[str],
        mount: Path,
        workdir: str,
        read_only_mount: bool,
        extra_env: dict[str, str],
    ) -> list[str]:
        mount_spec = f"{mount}:/work" + (":ro" if read_only_mount else "")
        # Docker requires --memory as an integer; convert GB -> MB.
        memory_mb = max(1, int(self.sandbox.memory_limit_gb * 1024))
        argv: list[str] = [
            self.docker_bin, "run", "--rm",
            "--network=none",
            f"--cpus={self.sandbox.cpu_limit}",
            f"--memory={memory_mb}m",
            f"--name={container_name}",
            "-v", mount_spec,
            "-w", workdir,
        ]
        for k, v in extra_env.items():
            argv.extend(["-e", f"{k}={v}"])
        argv.append(self.sandbox.image_ref)
        argv.extend(self.sandbox.command_prefix)
        argv.extend(cmd)
        return argv


# F8.2 alias. :class:`SandboxRunner` is the docker-backed
# :class:`~chip_agent.tools._protocols.SandboxLike` impl; ``DockerSandbox``
# is the name the M8 plan references. Kept as an alias rather than a
# subclass so type-narrowing and ``isinstance`` checks stay symmetric.
DockerSandbox = SandboxRunner
