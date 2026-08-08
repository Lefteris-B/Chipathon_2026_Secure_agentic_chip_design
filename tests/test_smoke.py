"""Smoke test: the package and schema import cleanly."""

from __future__ import annotations


def test_package_imports() -> None:
    import chip_agent  # noqa: F401


def test_schema_imports() -> None:
    from chip_agent.design_state import DesignState, Stage

    state = DesignState(design_id="d0", name="smoke")
    assert state.current_stage is Stage.SPEC
