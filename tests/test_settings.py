"""F0.2 acceptance: config loads from YAML + env; invalid raises a clear error."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chip_agent.settings import Settings, SettingsError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_defaults_are_sane(monkeypatch: pytest.MonkeyPatch) -> None:
    # No env vars leaking from the host environment into the defaults check.
    for key in list(__import__("os").environ):
        if key.startswith("CHIP_AGENT_"):
            monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.constraints.pdk == "gf180mcuD"
    assert s.constraints.std_cell_lib == "gf180mcu_fd_sc_mcu7t5v0"
    assert s.budgets.max_attempts == 3
    assert s.budgets.max_react_steps == 6
    assert s.routing.registry == {}
    assert s.sandbox.image == "hpretl/iic-osic-tools"


def test_loads_from_yaml(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
        constraints:
          pdk: sky130A
          target_clock_ns: 10.0
        budgets:
          cost_budget_usd: 12.5
          max_attempts: 5
        routing:
          registry:
            local: { provider: ollama, model: x }
          loops:
            inner: { model: local, temperature: 0.4 }
        """,
    )
    s = Settings.from_yaml(cfg)
    assert s.constraints.target_clock_ns == 10.0
    assert s.budgets.cost_budget_usd == 12.5
    assert s.budgets.max_attempts == 5
    assert s.routing.loops["inner"].model == "local"


def test_shipped_default_yaml_is_valid() -> None:
    s = Settings.from_yaml(REPO_ROOT / "chip_agent" / "configs" / "default.yaml")
    assert "local-coder" in s.routing.registry
    assert s.routing.loops["inner"].model == "local-coder"
    assert s.routing.loops["outer"].model == "frontier"


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHIP_AGENT_BUDGETS__COST_BUDGET_USD", "99.0")
    monkeypatch.setenv("CHIP_AGENT_BUDGETS__MAX_ATTEMPTS", "7")
    monkeypatch.setenv("CHIP_AGENT_CONSTRAINTS__PDK", "sky130B-test")
    s = Settings()
    assert s.budgets.cost_budget_usd == 99.0
    assert s.budgets.max_attempts == 7
    assert s.constraints.pdk == "sky130B-test"


def test_invalid_type_raises_clear_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path, "budgets:\n  max_attempts: not-an-int\n")
    with pytest.raises(ValidationError) as ei:
        Settings.from_yaml(cfg)
    msg = str(ei.value)
    assert "budgets" in msg and "max_attempts" in msg


def test_negative_budget_raises_clear_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path, "budgets:\n  cost_budget_usd: -1.0\n")
    with pytest.raises(ValidationError) as ei:
        Settings.from_yaml(cfg)
    assert "cost_budget_usd" in str(ei.value)


def test_unknown_field_raises_clear_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path, "budgets:\n  not_a_real_field: 1\n")
    with pytest.raises(ValidationError) as ei:
        Settings.from_yaml(cfg)
    assert "not_a_real_field" in str(ei.value)


def test_binding_to_missing_registry_raises_clear_error(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
        routing:
          registry:
            foo: { provider: ollama, model: m }
          loops:
            inner: { model: bar }
        """,
    )
    with pytest.raises(ValidationError) as ei:
        Settings.from_yaml(cfg)
    msg = str(ei.value)
    assert "bar" in msg and "foo" in msg


def test_unknown_loop_slot_raises_clear_error(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
        routing:
          registry:
            foo: { provider: ollama, model: m }
          loops:
            sideways: { model: foo }
        """,
    )
    with pytest.raises(ValidationError) as ei:
        Settings.from_yaml(cfg)
    assert "sideways" in str(ei.value)


def test_malformed_yaml_raises_settings_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path, ": : not yaml :::\n")
    with pytest.raises((SettingsError, ValidationError)):
        Settings.from_yaml(cfg)


def test_missing_file_raises_settings_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        Settings.from_yaml(tmp_path / "does-not-exist.yaml")


# --------------------------------------------------------------------------- #
# F21.2-D — SignoffSettings.
#
# The signoff block defaults to "no multi-corner" (sta_corners=None) so
# existing configs that don't mention it keep today's single-corner path
# byte-identical. When set in YAML, the field round-trips into a list[str]
# that cli.py converts to a tuple at the PhysicalConfig seam.
# --------------------------------------------------------------------------- #
def test_signoff_defaults_are_single_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("CHIP_AGENT_"):
            monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.signoff.sta_corners is None
    assert s.signoff.sta_report_power is False


def test_signoff_sta_corners_round_trip_from_yaml(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        """
        signoff:
          sta_corners: [tt, ss, ff]
          sta_report_power: true
        """,
    )
    s = Settings.from_yaml(cfg)
    assert s.signoff.sta_corners == ["tt", "ss", "ff"]
    assert s.signoff.sta_report_power is True


def test_signoff_unknown_field_rejected(tmp_path: Path) -> None:
    """SignoffSettings uses ``extra='forbid'`` via _Strict — a typo
    fails loudly so operators don't silently lose a knob."""
    cfg = _write(tmp_path, "signoff:\n  sta_corner: [tt]\n")  # typo: corner vs corners
    with pytest.raises(ValidationError) as ei:
        Settings.from_yaml(cfg)
    assert "sta_corner" in str(ei.value)
