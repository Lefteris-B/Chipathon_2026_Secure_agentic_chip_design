"""F10.1 — ``cmd_run`` requires ``--config`` with a non-empty routing registry.

The fallback ``_StubRouter`` baked into ``cli_stubs.py`` was deleted; the
CLI must surface a clear ``CLIError`` when:

* no ``--config`` was passed at all;
* the supplied config has an empty ``routing.registry``.

Neither path may silently fall through to a no-op or run against an
in-memory stub — the only way to drive the demo is via a configured
:class:`LiteLLMRouter`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chip_agent.cli import CLIError, RunArgs, cmd_run

HMAC_KEY = b"f10-1-no-router-hmac-key"

_COUNTER_SPEC_MD = """\
# 8-bit counter

* Top-level module ID: `counter`
* Target clock period: 10 ns.
"""


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    p = tmp_path / "counter.md"
    p.write_text(_COUNTER_SPEC_MD)
    return p


def _args(*, spec_path: Path, run_dir: Path, config_path: Path | None) -> RunArgs:
    return RunArgs(
        cmd="run", spec_path=spec_path, name="counter", run_dir=run_dir,
        design_id="no-router", hmac_key=HMAC_KEY, config_path=config_path,
    )


def test_cmd_run_without_config_path_errors_clearly(
    spec_file: Path, tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    with pytest.raises(CLIError, match="--config <yaml> is required"):
        cmd_run(_args(spec_path=spec_file, run_dir=run_dir, config_path=None))


def test_cmd_run_with_empty_registry_errors_clearly(
    spec_file: Path, tmp_path: Path,
) -> None:
    """A config that omits ``routing.registry`` cannot drive the agents."""
    run_dir = tmp_path / "run"
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("routing:\n  registry: {}\n")
    with pytest.raises(CLIError, match=r"routing\.registry is empty"):
        cmd_run(_args(spec_path=spec_file, run_dir=run_dir, config_path=cfg))


def test_cmd_run_with_omitted_routing_block_errors_clearly(
    spec_file: Path, tmp_path: Path,
) -> None:
    """A config that omits the routing block entirely is also rejected."""
    run_dir = tmp_path / "run"
    cfg = tmp_path / "no-routing.yaml"
    cfg.write_text("paths:\n  runs_dir: runs\n")
    with pytest.raises(CLIError, match=r"routing\.registry is empty"):
        cmd_run(_args(spec_path=spec_file, run_dir=run_dir, config_path=cfg))
