"""Unit tests for the IIC-OSIC-TOOLS entrypoint banner stripper."""

from __future__ import annotations

from chip_agent.tools._runner_utils import strip_entrypoint_banner


def test_strips_known_log_levels() -> None:
    """INFO / WARN / WARNING / ERROR / DEBUG prefixes are all removed."""
    text = (
        "[INFO] USER_ID: 1000\n"
        "[WARN] tcsh missing\n"
        "[WARNING] using fallback\n"
        "[ERROR] image not ready\n"
        "[DEBUG] startup_us=14ms\n"
        "real tool output\n"
    )
    assert strip_entrypoint_banner(text) == "real tool output\n"


def test_preserves_lines_with_level_token_mid_line() -> None:
    """A verible finding that happens to mention "[INFO]" inside the
    message must NOT be stripped — only lines that *start* with the
    bracketed level qualify."""
    text = (
        "counter.v:12:5: case statement missing default "
        "(see [INFO]: rule reference)\n"
    )
    assert strip_entrypoint_banner(text) == text


def test_empty_input_returns_empty() -> None:
    assert strip_entrypoint_banner("") == ""


def test_no_banner_unchanged() -> None:
    text = "line one\nline two\n"
    assert strip_entrypoint_banner(text) == text


def test_trailing_newline_preserved_when_content_survives() -> None:
    """Filtered output with surviving content keeps the trailing newline."""
    text = "[INFO] banner\nreal\n"
    assert strip_entrypoint_banner(text) == "real\n"


def test_trailing_newline_dropped_when_all_lines_stripped() -> None:
    """Banner-only input collapses to empty — don't manufacture a
    spurious newline that downstream str-equality tests would trip on."""
    text = "[INFO] line one\n[WARN] line two\n"
    assert strip_entrypoint_banner(text) == ""


def test_unknown_bracketed_token_passes_through() -> None:
    """Tools that emit ``[NOTE]`` or ``[VERBOSE]`` etc. shouldn't be
    swallowed — the stripper is intentionally conservative."""
    text = "[NOTE] custom level\nreal output\n"
    assert strip_entrypoint_banner(text) == text
