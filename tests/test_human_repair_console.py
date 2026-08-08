"""F23.5 (Option A): console transcript provider + --interactive-repair flag.

Pins the blocking chat/run REPL seam that feeds
``StageContext.human_transcript_for``:

* the provider prints the diagnosis and reads a multi-line block,
  returning the operator's text (or ``None`` to decline);
* ``run --interactive-repair`` flows through the arg parser into
  ``RunArgs.interactive_repair`` (default off).
"""

from __future__ import annotations

import io

from chip_agent.cli import (
    _hint_update_for_state,
    _resolve_args,
    build_arg_parser,
)
from chip_agent.cli_human_repair import console_transcript_provider
from chip_agent.design_state import (
    DesignState,
    FailureDiagnosis,
    PendingHumanRepair,
    Provenance,
    Stage,
)


def _diagnosis() -> FailureDiagnosis:
    return FailureDiagnosis(
        artifact_id="d0.m.diagnosis", design_id="d0", module_id="m",
        nl_summary="ciphertext wrong for the all-zero vector",
        failing_signal="ciphertext", cycle=240,
        expected="5579c1387b228445", actual="38d2f04c34635345",
        provenance=Provenance(produced_by=Stage.RTL, agent="rtl_stage"),
    )


def _state() -> DesignState:
    return DesignState(design_id="d0", name="present80")


def test_provider_reads_multiline_transcript_and_shows_diagnosis() -> None:
    stdin = io.StringIO("the final addRoundKey is missing\nadd it after round 31\n\n")
    stdout = io.StringIO()
    provider = console_transcript_provider(stdin=stdin, stdout=stdout)

    transcript = provider(_state(), "m", _diagnosis())

    assert transcript == "the final addRoundKey is missing\nadd it after round 31"
    shown = stdout.getvalue()
    assert "ciphertext wrong for the all-zero vector" in shown   # diagnosis surfaced
    assert "38d2f04c34635345" in shown                            # actual value shown


def test_provider_empty_input_declines() -> None:
    provider = console_transcript_provider(
        stdin=io.StringIO("\n"), stdout=io.StringIO(),
    )
    assert provider(_state(), "m", _diagnosis()) is None


def test_provider_eof_declines() -> None:
    provider = console_transcript_provider(
        stdin=io.StringIO(""), stdout=io.StringIO(),
    )
    assert provider(_state(), "m", _diagnosis()) is None


def test_run_interactive_repair_flag_parses() -> None:
    parser = build_arg_parser()
    ns = parser.parse_args([
        "run", "--spec", "x.md", "--name", "present80",
        "--run-dir", "/tmp/x", "--interactive-repair",
    ])
    assert _resolve_args(ns).interactive_repair is True


def test_run_interactive_repair_defaults_off() -> None:
    parser = build_arg_parser()
    ns = parser.parse_args([
        "run", "--spec", "x.md", "--name", "present80", "--run-dir", "/tmp/x",
    ])
    assert _resolve_args(ns).interactive_repair is False


# --------------------------------------------------------------------------- #
# F23.5 Option B — resume --hint parsing + injection helper
# --------------------------------------------------------------------------- #
def test_resume_hint_flag_parses() -> None:
    parser = build_arg_parser()
    ns = parser.parse_args([
        "resume", "--design-id", "d0", "--run-dir", "/tmp/x",
        "--hint", "add the final addRoundKey",
    ])
    assert _resolve_args(ns).hint == "add the final addRoundKey"
    # Default: no hint.
    ns2 = parser.parse_args(["resume", "--design-id", "d0", "--run-dir", "/tmp/x"])
    assert _resolve_args(ns2).hint is None


def test_hint_update_fills_parked_request() -> None:
    state = DesignState(design_id="d0", name="present80")
    state.pending_human_repair = PendingHumanRepair(module_id="m", stage=Stage.RTL)
    update = _hint_update_for_state(state, "the final XOR is missing")
    assert update is not None
    assert update["pending_human_repair"].transcript == "the final XOR is missing"
    # Original object not mutated (model_copy).
    assert state.pending_human_repair.transcript is None


def test_hint_update_none_when_no_pause() -> None:
    state = DesignState(design_id="d0", name="present80")  # no pending request
    assert _hint_update_for_state(state, "anything") is None
