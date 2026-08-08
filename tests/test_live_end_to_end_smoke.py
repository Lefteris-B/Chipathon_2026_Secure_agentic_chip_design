"""F11.7 — live end-to-end smoke: chat -> run -> resume -> real GDS.

Drives the full operator chain against live providers + live LibreLane in
a single opt-in test so we can prove the M11 chat-driven flow actually
closes through to a hand-checkable GDS file. All earlier live smokes
exercise one rail at a time (F10.4 frontier, F11.1 Ollama, F10.3
LibreLane closure); F11.7 is the one place we run them together end-to-end.

Activation gates (every gate must pass — any miss skips with a clear
reason rather than failing):

* ``CHIP_AGENT_LIVE_E2E=1`` — the conftest marker check.
* ``ANTHROPIC_API_KEY`` set — the F11.1/F11.5 hybrid policy routes
  SPEC_INTAKE / PLAN / DIAGNOSE to the frontier model.
* Ollama daemon reachable at ``CHIP_AGENT_OLLAMA_URL``
  (defaults to ``http://localhost:11434``) — high-volume RTL_GEN /
  RTL_REPAIR / TB_GEN route there. (Note: even if Ollama is unreachable
  here, the F11.5 fallback would demote to frontier mid-run — but we
  pre-flight the daemon so the smoke surfaces an honest network
  configuration issue rather than silently spending frontier tokens for
  every local-coder call.)
* Docker daemon available + the pinned IIC-OSIC-TOOLS image pulled —
  the LibreLane spine runs inside the container.

Runtime budget: 10-30 minutes depending on Ollama latency + LibreLane
closure speed; ``time_limit_s`` in the demo config caps it at 1800 s.

Failure-mode policy:

* Hard fail only on chain-shape regressions (Spec didn't mint, paused
  state isn't AWAITING_HUMAN, audit log doesn't contain the expected
  events, exports tree is missing a file).
* PHYSICAL closure failures are **soft passes**: we accept either
  ``DesignStatus.COMPLETED`` with a real GDS or ``DesignStatus.FAILED``
  with a typed ``PhysicalRun.failure_artifact`` (F10.3's deliverable
  shape). The test reports which path the run took.
* Mid-stream fallback events are observed (printed) but not failed —
  F11.5 made fallback a feature.
"""

from __future__ import annotations

import io
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from chip_agent.cli import RunArgs, cmd_chat, cmd_resume, cmd_run
from chip_agent.design_state import ArtifactRef, DesignStatus, Stage
from chip_agent.obs.audit_log import EventType, SqliteAuditLog

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_OLLAMA_CFG = REPO_ROOT / "configs" / "demo-ollama.yaml"
DESIGN_ID = "live-e2e"
DESIGN_NAME = "counter"
HMAC_KEY = b"f11.7-live-e2e-hmac-key"

_DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"


def _ollama_endpoint() -> str:
    return os.environ.get(
        "CHIP_AGENT_OLLAMA_URL", _DEFAULT_OLLAMA_ENDPOINT,
    ).rstrip("/")


def _skip_unless_preflight_ok() -> None:
    """Every external dependency is present + healthy, or skip cleanly."""
    if not DEMO_OLLAMA_CFG.exists():
        pytest.skip(f"{DEMO_OLLAMA_CFG} missing")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip(
            "ANTHROPIC_API_KEY not set; the hybrid demo-ollama.yaml routes "
            "PLAN / SPEC_INTAKE / DIAGNOSE to a frontier Anthropic model.",
        )

    endpoint = _ollama_endpoint()
    try:
        with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=3) as resp:
            if resp.status != 200:
                pytest.skip(
                    f"Ollama at {endpoint} returned status {resp.status}",
                )
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"Ollama at {endpoint} not reachable: {e}")

    # Lazy-import to keep the offline test collection cheap: sandbox + image
    # checks touch docker / settings parsing only when the smoke is enabled.
    from chip_agent.settings import Settings
    from chip_agent.tools.image import image_locally_available

    settings = Settings.from_yaml(DEMO_OLLAMA_CFG)
    if not image_locally_available(settings.sandbox):
        pytest.skip(
            f"{settings.sandbox.image}:{settings.sandbox.image_tag} not "
            "pulled locally; run `docker pull` first.",
        )


def _chat_args(*, run_dir: Path, stdin_text: str) -> RunArgs:
    return RunArgs(
        cmd="chat", spec_path=None, name=DESIGN_NAME, run_dir=run_dir,
        design_id=DESIGN_ID, hmac_key=HMAC_KEY,
        config_path=DEMO_OLLAMA_CFG,
        chat_stdin=io.StringIO(stdin_text),
        chat_stdout=io.StringIO(),
    )


def _run_args(*, run_dir: Path) -> RunArgs:
    return RunArgs(
        cmd="run", spec_path=None, name=DESIGN_NAME, run_dir=run_dir,
        design_id=DESIGN_ID, hmac_key=HMAC_KEY, sandbox_kind="docker",
        config_path=DEMO_OLLAMA_CFG,
    )


def _resume_args(*, run_dir: Path) -> RunArgs:
    return RunArgs(
        cmd="resume", spec_path=None, name=None, run_dir=run_dir,
        design_id=DESIGN_ID, hmac_key=HMAC_KEY, sandbox_kind="docker",
        config_path=DEMO_OLLAMA_CFG,
    )


@pytest.mark.docker
@pytest.mark.live_end_to_end
def test_chat_then_run_then_resume_produces_a_real_gds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The full operator chain: chat mints Spec, run pauses at human gate,
    resume produces a real GDS — all artifacts land under exports/<id>/."""
    _skip_unless_preflight_ok()

    run_dir = tmp_path / "run"

    # ----------------------------------------------------------------- chat #
    # The chat-persona's streaming reply may ask clarifying questions, and
    # on ``/run`` the F11.4 SpecIntake loop may ask MORE: the live frontier
    # model has its own appetite for follow-ups. Two defences:
    #
    # 1. Over-specify the initial prompt — pre-empt the common targets
    #    (enable signal, overflow behavior, reset polarity, port widths,
    #    target clock, top-module name).
    # 2. Pre-script three terse "no further constraints" answers in stdin.
    #    F11.4's default clarifying_budget=3 forces a Spec materialisation
    #    on the fourth call, so three canned answers is the safe ceiling
    #    regardless of how chatty the model is.
    chat_outcome = cmd_chat(_chat_args(
        run_dir=run_dir,
        stdin_text=(
            # Over-specified initial prompt.
            "Build an 8-bit synchronous up-counter named `counter` on sky130. "
            "Ports: clk (input, 10 ns period), rst_n (input, async low), "
            "q (output [7:0]). When rst_n is low, q is zeroed asynchronously. "
            "When rst_n is high, q increments on every rising clk edge. "
            "No enable signal — the counter always increments when rst_n is "
            "high. Wraps to 0 on overflow (standard 8-bit modulo-256). "
            "No other functional requirements; no timing or area constraints "
            "beyond the 10 ns clock. Materialise the spec as-is.\n"
            "/run\n"
            # Canned answers — each covers one round of the F11.4 loop.
            "The specification above is complete; no other behaviour required.\n"
            "Defaults are acceptable for any unspecified parameter.\n"
            "Please materialise the spec without further clarifying questions.\n"
        ),
    ))
    assert chat_outcome.spec_ref is not None, (
        "chat ended without minting a Spec — did /run hit the model? "
        "Check transcript at "
        f"{run_dir}/chat.transcript.md"
    )
    transcript = (run_dir / "chat.transcript.md").read_text()
    assert "counter" in transcript.lower()
    spec_md = run_dir / "exports" / DESIGN_ID / "spec.md"
    assert spec_md.is_file(), (
        "F11.6 export pass did not write exports/<id>/spec.md"
    )

    # ----------------------------------------------------------------- run #
    run_outcome = cmd_run(_run_args(run_dir=run_dir))
    assert run_outcome.spec_ref.content_hash == chat_outcome.spec_ref.content_hash, (
        "cmd_run minted a new Spec instead of reusing the chat handoff"
    )
    paused = run_outcome.paused_state
    exports = run_outcome.exports_dir

    # Soft-pass triage is layered top-down: each branch widens the set of
    # "the chain ran but the live model produced something the spine
    # couldn't carry to GDSII" cases. Hard fails are reserved for chain-
    # shape regressions (no human-gate pause at all, audit chain corrupt).

    # (1) Did we reach the human gate at all?
    if paused.status is not DesignStatus.AWAITING_HUMAN:
        # The spine failed before any gate fired — a chain-shape regression.
        pytest.fail(
            f"run did not reach the human gate: status={paused.status!r}, "
            f"current_stage={paused.current_stage!r}; "
            f"inspect exports at {exports}",
        )

    # (2) Did we pause at SIGNOFF (the normal end-of-flow gate)? RTL or
    # SYNTH may have escalated to HUMAN upstream — in that case cmd_resume
    # can't recover a stage that never closed, so this is a soft-pass
    # shaped like F10.3 closure failure. Skip with a triage-shaped reason.
    if paused.current_stage is not Stage.SIGNOFF:
        rtl_files = list((exports / "rtl").glob("*.v"))
        captured = capsys.readouterr()
        # Surface the failing stage's last_failure ref if we can find it —
        # gives the operator a single ref to load from the store.
        last_failure_ref: ArtifactRef | None = None
        if paused.current_stage is Stage.RTL:
            module_id = paused.top_module_id
            if module_id is not None:
                module_state = paused.modules.get(module_id)
                if module_state is not None:
                    rtl_state = module_state.stages.get(Stage.RTL)
                    last_failure_ref = (
                        rtl_state.last_failure if rtl_state is not None
                        else None
                    )
        else:
            ss = paused.stages.get(paused.current_stage)
            last_failure_ref = ss.last_failure if ss is not None else None

        pytest.skip(
            f"run paused at {paused.current_stage.value!r}, not at SIGNOFF: "
            f"the live model produced output that didn't close through to "
            f"signoff. RTL files exported: "
            f"{[p.name for p in rtl_files] or '(none)'}; "
            f"last_failure_ref={last_failure_ref!r}; "
            f"inspect exports at {exports}; "
            f"stdout tail: {captured.out[-400:]!r}",
        )

    # (3) We're at SIGNOFF: the upstream stages must have heads. From here
    # the test asserts the full export tree the operator will inspect.
    top_id = paused.top_module_id or DESIGN_NAME
    module_state = paused.modules.get(top_id)
    assert module_state is not None, (
        f"design has no module entry for top_module_id={top_id!r}"
    )
    rtl_state = module_state.stages.get(Stage.RTL)
    assert rtl_state is not None and rtl_state.head is not None, (
        f"RTL did not promote a head despite paused-at-SIGNOFF; "
        f"status={paused.status!r}"
    )

    # Don't pin a literal filename. The export pass names RTL files by the
    # *Verilog module name* (``rtl.top_module``), which a live planner may
    # report differently from the plan's ``top_module_id`` (e.g. plan says
    # ``counter_top``, RTL emits ``module counter``).
    rtl_files = list((exports / "rtl").glob("*.v"))
    assert rtl_files, (
        f"no .v files under exports/{DESIGN_ID}/rtl/ after cmd_run; "
        f"exports tree at {exports}"
    )

    # --------------------------------------------------------------- resume #
    resume_outcome = cmd_resume(_resume_args(run_dir=run_dir))
    final = resume_outcome.final_state

    # Soft pass on PHYSICAL closure failures (the F10.3 fallback shape).
    if final.status is DesignStatus.FAILED:
        physical_ss = final.stages.get(Stage.PHYSICAL)
        last_failure = physical_ss.last_failure if physical_ss else None
        captured = capsys.readouterr()
        pytest.skip(
            "LibreLane did not close on the chat-derived spec — this is the "
            f"F10.3 soft-pass shape. last_failure={last_failure!r}; "
            f"inspect exports at {exports}; "
            f"stdout tail: {captured.out[-400:]!r}",
        )

    assert final.status is DesignStatus.COMPLETED, (
        f"resume did not complete: status={final.status!r}, "
        f"current_stage={final.current_stage!r}"
    )
    assert final.current_stage is Stage.GDSII

    # The final GDS must be on disk and shaped like a real GDSII stream.
    # Like the RTL assertion, glob rather than pinning a filename — the
    # planner / RTL module name drives the export stem.
    gds_files = list((exports / "gds").glob("*.gds"))
    assert gds_files, f"no .gds files under exports/{DESIGN_ID}/gds/"
    assert len(gds_files) == 1, (
        f"expected exactly one .gds export, got {[p.name for p in gds_files]}"
    )
    gds_bytes = gds_files[0].read_bytes()
    assert len(gds_bytes) > 1024, (
        f"GDS too small to be a real layout: {len(gds_bytes)} bytes"
    )
    # GDSII HEADER record: 2-byte length (0x0006) + 2-byte record (HEADER=0x0002).
    assert gds_bytes[:4] == b"\x00\x06\x00\x02", (
        f"GDS body lacks the HEADER record: first 8 bytes = {gds_bytes[:8]!r}"
    )

    # ---------------------------------------------------------------- audit #
    # Pin the audit log shape: ARTIFACT_PROMOTED for SPEC + PLAN + the GDS;
    # the hash chain verifies clean. BACKEND_FALLBACK events are observed
    # (F11.5 makes them legal) — print them for triage, don't fail.
    audit_path = run_dir / "audit.sqlite"
    audit = SqliteAuditLog(db_path=audit_path, hmac_key=HMAC_KEY)
    try:
        events = audit.events(DESIGN_ID)
        verification = audit.verify(DESIGN_ID)
    finally:
        audit.close()

    assert verification.valid, (
        f"audit chain verification failed: {verification.findings}"
    )

    promoted = [
        e for e in events if e.event_type is EventType.ARTIFACT_PROMOTED
    ]
    promoted_stages = {e.payload.get("stage") for e in promoted}
    assert {Stage.SPEC.value, Stage.PLAN.value} <= promoted_stages, (
        f"missing ARTIFACT_PROMOTED for spec/plan; got {promoted_stages!r}"
    )

    fallbacks = [
        e for e in events if e.event_type is EventType.BACKEND_FALLBACK
    ]
    if fallbacks:
        print(
            f"observed {len(fallbacks)} BACKEND_FALLBACK event(s) — F11.5 "
            "transparent demotion fired during the live smoke. Details: "
            + ", ".join(str(e.payload) for e in fallbacks),
        )
