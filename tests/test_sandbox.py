"""F0.4 acceptance: sandboxed run produces a typed ToolRun with the right invariants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.design_state import ToolRun
from chip_agent.settings import SandboxSettings, Settings
from chip_agent.tools.sandbox import (
    ProcessResult,
    SandboxError,
    SandboxRunner,
)

DIGEST = "sha256:" + "a" * 64


def _pinned_settings(**overrides: object) -> SandboxSettings:
    base: dict[str, object] = {
        "image": "hpretl/iic-osic-tools",
        "image_tag": "2026.04",
        "image_digest": DIGEST,
        "cpu_limit": 2.0,
        "memory_limit_gb": 4.0,
        "time_limit_s": 120,
    }
    base.update(overrides)
    return Settings(sandbox=base).sandbox


@dataclass
class StubProcessRunner:
    """Records every argv it was called with; returns canned results."""

    result: ProcessResult = field(default_factory=lambda: ProcessResult(0, "ok", "", False))
    cleanup_result: ProcessResult = field(default_factory=lambda: ProcessResult(0, "", "", False))
    calls: list[tuple[list[str], int | None]] = field(default_factory=list)

    def run(self, argv: list[str], *, timeout: int | None = None) -> ProcessResult:
        self.calls.append((list(argv), timeout))
        if argv[:2] == ["docker", "kill"]:
            return self.cleanup_result
        return self.result


def test_run_builds_expected_docker_argv(tmp_path: Path) -> None:
    stub = StubProcessRunner()
    runner = SandboxRunner(_pinned_settings(), process_runner=stub)
    tr = runner.run(["yosys", "-V"], mount=tmp_path)

    assert isinstance(tr, ToolRun)
    argv, timeout = stub.calls[0]
    assert argv[0] == "docker" and argv[1] == "run"
    assert "--rm" in argv
    assert "--network=none" in argv
    assert "--cpus=2.0" in argv
    assert "--memory=4096m" in argv               # 4.0 GB -> 4096 MB
    # Unique container name so we can kill on timeout.
    name_flag = next(a for a in argv if a.startswith("--name="))
    assert name_flag.startswith("--name=chip-agent-")
    # Mount: only the design dir, mapped to /work, rw by default.
    v_idx = argv.index("-v")
    assert argv[v_idx + 1] == f"{tmp_path.resolve()}:/work"
    # Workdir.
    w_idx = argv.index("-w")
    assert argv[w_idx + 1] == "/work"
    # Image ref comes from sandbox.image_ref (digest-pinned here).
    img = f"hpretl/iic-osic-tools@{DIGEST}"
    img_idx = argv.index(img)
    # The cmd follows the image ref.
    assert argv[img_idx + 1 :] == ["yosys", "-V"]
    # Default timeout = sandbox.time_limit_s.
    assert timeout == 120


def test_run_uses_explicit_time_limit(tmp_path: Path) -> None:
    stub = StubProcessRunner()
    runner = SandboxRunner(_pinned_settings(), process_runner=stub)
    runner.run(["true"], mount=tmp_path, time_limit_s=7)
    _, timeout = stub.calls[0]
    assert timeout == 7


def test_run_captures_typed_tool_run(tmp_path: Path) -> None:
    stub = StubProcessRunner(
        result=ProcessResult(returncode=3, stdout="out", stderr="err", timed_out=False)
    )
    runner = SandboxRunner(_pinned_settings(), process_runner=stub)
    tr = runner.run(["sh", "-c", "exit 3"], mount=tmp_path)
    assert tr.returncode == 3
    assert tr.stdout == "out"
    assert tr.stderr == "err"
    assert tr.artifacts_dir == str(tmp_path.resolve())
    assert tr.duration_s >= 0.0


def test_timeout_returns_rc_124_and_issues_docker_kill(tmp_path: Path) -> None:
    stub = StubProcessRunner(
        result=ProcessResult(124, "", "[sandbox] timed out after 1s", True)
    )
    runner = SandboxRunner(_pinned_settings(), process_runner=stub)
    tr = runner.run(["sleep", "60"], mount=tmp_path, time_limit_s=1)
    assert tr.returncode == 124
    assert "timed out" in tr.stderr
    # The runner must have followed up with `docker kill <name>` using the same
    # name it passed to `docker run`.
    run_name = next(
        a for a in stub.calls[0][0] if a.startswith("--name=")
    ).removeprefix("--name=")
    kill_call = stub.calls[1][0]
    assert kill_call == ["docker", "kill", run_name]


def test_read_only_mount_adds_ro_suffix(tmp_path: Path) -> None:
    stub = StubProcessRunner()
    runner = SandboxRunner(_pinned_settings(), process_runner=stub)
    runner.run(["true"], mount=tmp_path, read_only_mount=True)
    argv = stub.calls[0][0]
    v_idx = argv.index("-v")
    assert argv[v_idx + 1].endswith(":/work:ro")


def test_extra_env_passes_through(tmp_path: Path) -> None:
    stub = StubProcessRunner()
    runner = SandboxRunner(_pinned_settings(), process_runner=stub)
    runner.run(["true"], mount=tmp_path, extra_env={"PDK_ROOT": "/pdk"})
    argv = stub.calls[0][0]
    assert "-e" in argv
    assert "PDK_ROOT=/pdk" in argv


def test_mount_must_exist(tmp_path: Path) -> None:
    stub = StubProcessRunner()
    runner = SandboxRunner(_pinned_settings(), process_runner=stub)
    with pytest.raises(SandboxError):
        runner.run(["true"], mount=tmp_path / "does-not-exist")


def test_mount_must_be_directory(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    stub = StubProcessRunner()
    runner = SandboxRunner(_pinned_settings(), process_runner=stub)
    with pytest.raises(SandboxError):
        runner.run(["true"], mount=f)


def test_empty_cmd_rejected(tmp_path: Path) -> None:
    runner = SandboxRunner(_pinned_settings(), process_runner=StubProcessRunner())
    with pytest.raises(SandboxError):
        runner.run([], mount=tmp_path)


def test_unpinned_image_still_runs_with_tag_ref(tmp_path: Path) -> None:
    # An operator may run before pinning; the runner uses image:tag in that case.
    sb = SandboxSettings(image="hpretl/iic-osic-tools", image_tag="2026.04")
    stub = StubProcessRunner()
    runner = SandboxRunner(sb, process_runner=stub)
    runner.run(["true"], mount=tmp_path)
    argv = stub.calls[0][0]
    assert "hpretl/iic-osic-tools:2026.04" in argv


# --------------------------------------------------------------------------- #
# Integration: the three AC properties against a real docker daemon + alpine.
# Auto-skipped (see tests/conftest.py) when no daemon is reachable.
# --------------------------------------------------------------------------- #

ALPINE = SandboxSettings(
    image="alpine",
    image_tag="3.20",
    cpu_limit=1.0,
    memory_limit_gb=0.25,
    time_limit_s=30,
)


@pytest.mark.docker
def test_integration_network_is_blocked(tmp_path: Path) -> None:
    runner = SandboxRunner(ALPINE)
    tr = runner.run(
        ["wget", "-q", "-T", "2", "http://example.com", "-O", "-"],
        mount=tmp_path,
    )
    # With --network=none, wget cannot resolve or reach anything.
    assert tr.returncode != 0


@pytest.mark.docker
def test_integration_time_limit_fires(tmp_path: Path) -> None:
    runner = SandboxRunner(ALPINE)
    tr = runner.run(["sleep", "10"], mount=tmp_path, time_limit_s=2)
    assert tr.returncode == 124
    assert "timed out" in tr.stderr


@pytest.mark.docker
def test_integration_mount_round_trips_file(tmp_path: Path) -> None:
    runner = SandboxRunner(ALPINE)
    tr = runner.run(
        ["sh", "-c", "echo hello > /work/output.txt"],
        mount=tmp_path,
    )
    assert tr.returncode == 0, tr.stderr
    assert (tmp_path / "output.txt").read_text().strip() == "hello"
    assert tr.artifacts_dir == str(tmp_path.resolve())
