"""F11.3 acceptance: ``chip-agent chat`` REPL.

The five ACs from the M11 plan:

* The REPL streams chunks to stdout as they arrive (F11.2 router.stream).
* ``/run`` materialises a typed :class:`Spec` via :class:`SpecIntakeAgent`
  and stores it under ``<design_id>.spec``.
* ``/exit`` returns without minting a Spec; the store stays empty.
* On re-entry, an existing ``chat.transcript.md`` is loaded + echoed.
* End-to-end handoff: chat → ``/run`` → ``chip-agent run --design-id <same>``
  continues without re-running intake.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from chip_agent.agents.spec_intake import (
    UNDERSPECIFIED_REQUIREMENT_PREFIX,
)
from chip_agent.cli import RunArgs, cmd_chat, cmd_run
from chip_agent.cli_chat import (
    CHAT_SYSTEM_PROMPT,
    ChatSession,
    ChatSessionError,
    format_transcript,
    load_transcript,
)
from chip_agent.design_state import (
    ArtifactKind,
    DesignStatus,
    Spec,
)
from chip_agent.settings import Settings
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import (
    CHAT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

HMAC_KEY = b"f11.3-test-hmac-key"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


@pytest.fixture
def stub_backend() -> StubBackend:
    """Backend that recognises both the chat persona AND SpecIntakeAgent."""
    return StubBackend(matchers=CHAT_RESPONSES)


@pytest.fixture
def patched_router(
    monkeypatch: pytest.MonkeyPatch,
    routing_config: Path,
    stub_backend: StubBackend,
) -> StubBackend:
    """Make ``cli._resolve_router`` return our stub-backed router."""
    router, _ = make_test_router(config_path=routing_config, backend=stub_backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router",
        lambda _args, *, settings: router,
    )
    return stub_backend


def _chat_args(
    *,
    run_dir: Path,
    config_path: Path,
    name: str = "counter",
    design_id: str | None = None,
    stdin_text: str = "",
    stdout: io.StringIO | None = None,
) -> RunArgs:
    return RunArgs(
        cmd="chat",
        spec_path=None,
        name=name,
        run_dir=run_dir,
        design_id=design_id,
        hmac_key=HMAC_KEY,
        config_path=config_path,
        chat_stdin=io.StringIO(stdin_text),
        chat_stdout=stdout if stdout is not None else io.StringIO(),
    )


# --------------------------------------------------------------------------- #
# AC: streaming chunks reach stdout in order.
# --------------------------------------------------------------------------- #
def test_chat_repl_streams_chunks_to_stdout(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
) -> None:
    run_dir = tmp_path / "run"
    stdout = io.StringIO()
    # One user turn, then EOF — that lets the assistant stream once and stop.
    args = _chat_args(
        run_dir=run_dir, config_path=routing_config,
        design_id="chat-test", stdin_text="Make me an 8-bit counter\n",
        stdout=stdout,
    )
    outcome = cmd_chat(args)

    assert outcome.spec_ref is None  # EOF, no /run
    output = stdout.getvalue()
    # All three streaming chunks landed in stdout in order.
    assert "What clock period " in output
    assert "and reset polarity " in output
    assert "do you want?" in output
    assert output.index("What clock period ") < output.index("and reset polarity ")
    assert output.index("and reset polarity ") < output.index("do you want?")
    # And the assistant prefix is printed once before the deltas.
    assert "Assistant: What clock period " in output
    # Stub backend was invoked with stream() (not complete()) for the chat turn.
    chat_calls = [c for c in patched_router.calls if "chip-design intake" in c.get("system", "")]
    assert chat_calls, "chat persona never invoked"
    assert chat_calls[0].get("mode") == "stream"


# --------------------------------------------------------------------------- #
# AC: /run materialises a Spec and stores it.
# --------------------------------------------------------------------------- #
def test_chat_repl_run_command_mints_spec_artifact(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
) -> None:
    run_dir = tmp_path / "run"
    stdout = io.StringIO()
    # User turn, then /run.
    args = _chat_args(
        run_dir=run_dir, config_path=routing_config,
        design_id="chat-mint", name="counter",
        stdin_text="Make an 8-bit counter with active-low reset.\n/run\n",
        stdout=stdout,
    )
    outcome = cmd_chat(args)

    assert outcome.spec_ref is not None
    assert outcome.spec_ref.artifact_id == "chat-mint.spec"
    assert outcome.spec_ref.kind is ArtifactKind.SPEC

    # The Spec landed in the store with provider = the stub registry entry.
    store = SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    )
    try:
        spec = store.get_by_id("chat-mint.spec")
        assert isinstance(spec, Spec)
        assert spec.design_id == "chat-mint"
        assert spec.provenance.model is not None
        assert spec.provenance.model.provider == "stub"
        # Spec normalised body comes from the SpecIntake matcher.
        assert "Ports" in spec.normalized
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# AC: /exit returns without minting a Spec; the store stays empty.
# --------------------------------------------------------------------------- #
def test_chat_repl_exit_command_returns_without_spec(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
) -> None:
    run_dir = tmp_path / "run"
    stdout = io.StringIO()
    args = _chat_args(
        run_dir=run_dir, config_path=routing_config,
        design_id="chat-exit", stdin_text="/exit\n", stdout=stdout,
    )
    outcome = cmd_chat(args)

    assert outcome.spec_ref is None

    # No spec in the store.
    store = SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    )
    try:
        with pytest.raises(Exception, match="no artifact"):
            store.get_by_id("chat-exit.spec")
    finally:
        store.close()

    # Transcript file was never written (no user turns).
    assert not (run_dir / "chat.transcript.md").exists()


# --------------------------------------------------------------------------- #
# AC: pre-existing transcript is loaded + echoed on re-entry.
# --------------------------------------------------------------------------- #
def test_chat_repl_resume_loads_prior_transcript(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    # Pre-populate the transcript file.
    transcript_path = run_dir / "chat.transcript.md"
    transcript_path.write_text(
        format_transcript(
            design_id="chat-resume", name="counter", turns=[
                __import__("chip_agent.cli_chat", fromlist=["ChatTurn"]).ChatTurn(
                    role="user", text="Make me an 8-bit counter",
                ),
                __import__("chip_agent.cli_chat", fromlist=["ChatTurn"]).ChatTurn(
                    role="assistant", text="What clock period do you want?",
                ),
            ],
        ),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    # User /exits immediately — we only care that the prior turns get echoed.
    args = _chat_args(
        run_dir=run_dir, config_path=routing_config,
        design_id="chat-resume", stdin_text="/exit\n", stdout=stdout,
    )
    cmd_chat(args)

    output = stdout.getvalue()
    assert "(resuming prior transcript)" in output
    assert "Make me an 8-bit counter" in output
    assert "What clock period do you want?" in output


# --------------------------------------------------------------------------- #
# AC: chat -> /run -> cmd_run handoff completes a full pipeline.
# --------------------------------------------------------------------------- #
def test_cmd_chat_then_run_handoff(
    tmp_path: Path, routing_config: Path, patched_router: StubBackend,
) -> None:
    run_dir = tmp_path / "run"

    # Chat: scripted /run after one user turn.
    chat_stdout = io.StringIO()
    chat_args = _chat_args(
        run_dir=run_dir, config_path=routing_config,
        design_id="handoff", name="counter",
        stdin_text=(
            "Make me an 8-bit counter with active-low async reset.\n/run\n"
        ),
        stdout=chat_stdout,
    )
    chat_outcome = cmd_chat(chat_args)
    assert chat_outcome.spec_ref is not None
    spec_ref_before = chat_outcome.spec_ref

    # Run: --design-id matches the chat-minted Spec; no --spec.
    run_args = RunArgs(
        cmd="run",
        spec_path=None,
        name="counter",
        run_dir=run_dir,
        design_id="handoff",
        hmac_key=HMAC_KEY,
        config_path=routing_config,
    )
    run_outcome = cmd_run(run_args)

    # cmd_run reused the existing Spec — no new version was minted (content
    # hash + version unchanged) — and the spine paused at the human gate.
    assert run_outcome.spec_ref.artifact_id == spec_ref_before.artifact_id
    assert run_outcome.spec_ref.content_hash == spec_ref_before.content_hash
    assert run_outcome.spec_ref.version == spec_ref_before.version
    assert run_outcome.paused_state.status is DesignStatus.AWAITING_HUMAN


# --------------------------------------------------------------------------- #
# Direct ChatSession tests — small surface, sharper invariants.
# --------------------------------------------------------------------------- #
def test_chat_session_run_with_empty_transcript_returns_none_on_eof(
    tmp_path: Path, routing_config: Path, stub_backend: StubBackend,
) -> None:
    settings = Settings.from_yaml(routing_config)
    router, _ = make_test_router(config_path=routing_config, backend=stub_backend)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    store = SqliteArtifactStore(
        db_path=store_dir / "store.sqlite", content_dir=store_dir / "content",
    )
    try:
        session = ChatSession(
            router=router, store=store, design_id="d0", name="t",
            transcript_path=tmp_path / "chat.md",
            defaults=settings.constraints,
            stdin=io.StringIO(""), stdout=io.StringIO(),
        )
        assert session.run() is None
    finally:
        store.close()


def test_chat_session_run_without_user_turn_raises(
    tmp_path: Path, routing_config: Path, stub_backend: StubBackend,
) -> None:
    """``/run`` before any user turn surfaces a typed error rather than
    minting an empty Spec."""
    settings = Settings.from_yaml(routing_config)
    router, _ = make_test_router(config_path=routing_config, backend=stub_backend)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    store = SqliteArtifactStore(
        db_path=store_dir / "store.sqlite", content_dir=store_dir / "content",
    )
    try:
        session = ChatSession(
            router=router, store=store, design_id="d0", name="t",
            transcript_path=tmp_path / "chat.md",
            defaults=settings.constraints,
            stdin=io.StringIO("/run\n"), stdout=io.StringIO(),
        )
        with pytest.raises(ChatSessionError):
            session.run()
    finally:
        store.close()


def test_chat_session_help_command_prints_help(
    tmp_path: Path, routing_config: Path, stub_backend: StubBackend,
) -> None:
    settings = Settings.from_yaml(routing_config)
    router, _ = make_test_router(config_path=routing_config, backend=stub_backend)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    store = SqliteArtifactStore(
        db_path=store_dir / "store.sqlite", content_dir=store_dir / "content",
    )
    try:
        stdout = io.StringIO()
        session = ChatSession(
            router=router, store=store, design_id="d0", name="t",
            transcript_path=tmp_path / "chat.md",
            defaults=settings.constraints,
            stdin=io.StringIO("/help\n/exit\n"), stdout=stdout,
        )
        session.run()
        out = stdout.getvalue()
        assert "/run" in out
        assert "/exit" in out
        assert "/show" in out
    finally:
        store.close()


def test_chat_session_persists_transcript_to_disk(
    tmp_path: Path, routing_config: Path, stub_backend: StubBackend,
) -> None:
    settings = Settings.from_yaml(routing_config)
    router, _ = make_test_router(config_path=routing_config, backend=stub_backend)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    store = SqliteArtifactStore(
        db_path=store_dir / "store.sqlite", content_dir=store_dir / "content",
    )
    transcript_path = tmp_path / "chat.md"
    try:
        session = ChatSession(
            router=router, store=store, design_id="d0", name="t",
            transcript_path=transcript_path,
            defaults=settings.constraints,
            stdin=io.StringIO("Hello, build me a counter.\n"),
            stdout=io.StringIO(),
        )
        session.run()
    finally:
        store.close()

    text = transcript_path.read_text(encoding="utf-8")
    assert "design_id: d0" in text
    assert "Hello, build me a counter." in text
    assert "**You:**" in text
    assert "**Assistant:**" in text


def test_chat_session_round_trip_transcript(tmp_path: Path) -> None:
    from chip_agent.cli_chat import ChatTurn
    turns = [
        ChatTurn(role="user", text="describe a counter"),
        ChatTurn(role="assistant", text="what width?"),
        ChatTurn(role="user", text="8 bits"),
    ]
    text = format_transcript("d0", "counter", turns)
    path = tmp_path / "t.md"
    path.write_text(text)
    loaded = load_transcript(path)
    assert [(t.role, t.text) for t in loaded] == [
        ("user", "describe a counter"),
        ("assistant", "what width?"),
        ("user", "8 bits"),
    ]


def test_chat_system_prompt_describes_intake_role() -> None:
    """The chat persona prompt makes clear it elicits constraints — not
    that it materialises a Spec directly. Materialisation happens via
    SpecIntakeAgent's own prompt on /run."""
    p = CHAT_SYSTEM_PROMPT
    assert "intake" in p.lower()
    assert "clock" in p.lower()
    assert "reset" in p.lower()
    assert "/run" in p


# --------------------------------------------------------------------------- #
# F11.4 — clarifying-question round-trip inside the REPL.
# --------------------------------------------------------------------------- #
def test_chat_repl_clarifying_question_round_trip(
    tmp_path: Path,
    routing_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scripted dialog: user describes a counter, types /run, intake asks a
    clarifying question, user answers, intake mints a Spec, ChatOutcome
    carries the spec_ref.

    The stub backend's SpecIntake matcher cycles its response: first call
    returns a JSON question, second call returns a normalised body.
    """
    import io
    import json

    from chip_agent.design_state import Spec
    from chip_agent.store.sqlite_store import SqliteArtifactStore
    from tests._routing_stub import (
        CHAT_RESPONSES,
        COUNTER_SPEC_NORMALISED,
        StubBackend,
        _PromptMatcher,
        make_test_router,
    )

    question_json = json.dumps({
        "type": "question",
        "question": "What port width should the counter have?",
        "missing_field": "ports",
        "rationale": "no width stated",
    })
    # Build a matcher tuple where the SpecIntake matcher cycles
    # [question, spec] across two calls.
    matchers = tuple(
        _PromptMatcher(
            needle=m.needle,
            response=m.response,
            response_sequence=(question_json, COUNTER_SPEC_NORMALISED)
            if "normalise natural-language" in m.needle else None,
            stream_chunks=m.stream_chunks,
            prompt_tokens=m.prompt_tokens,
            completion_tokens=m.completion_tokens,
            cost_usd=m.cost_usd,
        )
        for m in CHAT_RESPONSES
    )
    backend = StubBackend(matchers=matchers)
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router",
        lambda _args, *, settings: router,
    )

    run_dir = tmp_path / "run"
    stdout = io.StringIO()
    # Scripted stdin: describe → /run → answer the clarifying question.
    args = _chat_args(
        run_dir=run_dir, config_path=routing_config,
        design_id="clarify", name="counter",
        stdin_text="Make me a counter.\n/run\n8 bits, active-low async reset.\n",
        stdout=stdout,
    )
    outcome = cmd_chat(args)

    assert outcome.spec_ref is not None
    assert outcome.spec_ref.artifact_id == "clarify.spec"

    # The clarifying question landed in stdout as an assistant turn.
    output = stdout.getvalue()
    assert "What port width should the counter have?" in output

    # The store holds the materialised Spec; it does NOT carry the
    # under-spec flag (intake succeeded on the second pass, not via the
    # budget-exhaustion path).
    store = SqliteArtifactStore(
        db_path=run_dir / "store.sqlite", content_dir=run_dir / "content",
    )
    try:
        spec = store.get_by_id("clarify.spec")
        assert isinstance(spec, Spec)
        assert not any(
            r.startswith(UNDERSPECIFIED_REQUIREMENT_PREFIX)
            for r in spec.requirements
        )
    finally:
        store.close()

    # The transcript was extended with the clarifying Q + the user's A.
    transcript = (run_dir / "chat.transcript.md").read_text(encoding="utf-8")
    assert "What port width" in transcript
    assert "8 bits, active-low async reset" in transcript


def test_chat_repl_clarifying_question_user_eof_ends_cleanly(
    tmp_path: Path,
    routing_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user EOFs mid-clarification, the REPL ends without minting
    a Spec — no exception, ``ChatOutcome.spec_ref is None``."""
    import io
    import json

    from tests._routing_stub import (
        CHAT_RESPONSES,
        StubBackend,
        _PromptMatcher,
        make_test_router,
    )

    question_json = json.dumps({
        "type": "question",
        "question": "What clock period?",
        "missing_field": "clock",
        "rationale": "no period",
    })
    matchers = tuple(
        _PromptMatcher(
            needle=m.needle,
            response=question_json if "normalise natural-language" in m.needle
            else m.response,
            stream_chunks=m.stream_chunks,
            prompt_tokens=m.prompt_tokens,
            completion_tokens=m.completion_tokens,
            cost_usd=m.cost_usd,
        )
        for m in CHAT_RESPONSES
    )
    backend = StubBackend(matchers=matchers)
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router",
        lambda _args, *, settings: router,
    )

    run_dir = tmp_path / "run"
    stdout = io.StringIO()
    # Describe + /run, then EOF mid-clarification.
    args = _chat_args(
        run_dir=run_dir, config_path=routing_config,
        design_id="clarify-eof", name="counter",
        stdin_text="Make me a counter.\n/run\n",
        stdout=stdout,
    )
    outcome = cmd_chat(args)
    assert outcome.spec_ref is None
    # The question still made it to stdout.
    assert "What clock period?" in stdout.getvalue()
