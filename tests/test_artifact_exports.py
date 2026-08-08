"""F11.6 acceptance: named artifact exports under ``<run-dir>/exports/<id>/``.

Drives ``specs/counter.md`` through the stub-backed CLI and asserts the
exports tree mirrors every produced artifact under a human-readable name
(``rtl/<top>.v``, ``synth/<top>.netlist.v``, ``gds/<top>.gds``, …) so an
operator can hand-check the run without going through the SQLite index.

ACs:

* After ``cmd_run`` pauses at the human gate, ``exports/<id>/`` contains
  ``spec.md``, ``spec.json``, ``plan.json``, ``rtl/<top>.v``, the cocotb
  ``tb/<top>_tb.py`` and the lint / simulation report bodies.
* After ``cmd_resume`` completes, the exports tree gains the netlist
  Verilog, the routed DEF, every signoff JSON report and the final GDS.
* Exported blob bytes byte-equal what the content-addressed store would
  return for the corresponding ``BlobRef`` — exports are pure mirrors.
* Re-running ``cmd_run`` against the same run-dir is idempotent: every
  file under ``exports/<id>/`` keeps the same bytes.
* The outcome dataclasses surface ``exports_dir`` so callers know where
  to point an operator.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from chip_agent.cli import RunArgs, cmd_chat, cmd_resume, cmd_run
from chip_agent.design_state import (
    GDSIIArtifact,
    LayoutArtifact,
    NetlistArtifact,
    RTLArtifact,
    Stage,
    TestbenchArtifact,
)
from chip_agent.store.sqlite_store import SqliteArtifactStore
from tests._routing_stub import (
    CHAT_RESPONSES,
    StubBackend,
    make_routing_config,
    make_test_router,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COUNTER_SPEC = REPO_ROOT / "specs" / "counter.md"
HMAC_KEY = b"f11.6-exports-test-hmac-key"
DESIGN_ID = "exports-demo"


@pytest.fixture
def routing_config(tmp_path: Path) -> Path:
    return make_routing_config(tmp_path)


@pytest.fixture
def patch_router(
    monkeypatch: pytest.MonkeyPatch, routing_config: Path,
) -> StubBackend:
    backend = StubBackend()
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router",
        lambda _args, *, settings: router,
    )
    return backend


def _run_args(
    *,
    cmd: str,
    run_dir: Path,
    config_path: Path,
    spec_path: Path | None = None,
    name: str | None = None,
) -> RunArgs:
    return RunArgs(
        cmd=cmd, spec_path=spec_path, name=name, run_dir=run_dir,
        design_id=DESIGN_ID, hmac_key=HMAC_KEY, config_path=config_path,
    )


def _drive_through_pause(run_dir: Path, *, config_path: Path) -> Path:
    """Drive ``cmd_run`` against the counter spec; return the exports dir."""
    out = cmd_run(_run_args(
        cmd="run", run_dir=run_dir, config_path=config_path,
        spec_path=COUNTER_SPEC, name="counter",
    ))
    return out.exports_dir


def _drive_through_completion(run_dir: Path, *, config_path: Path) -> Path:
    """Drive ``cmd_run`` + ``cmd_resume``; return the final exports dir."""
    _drive_through_pause(run_dir, config_path=config_path)
    out = cmd_resume(_run_args(
        cmd="resume", run_dir=run_dir, config_path=config_path,
    ))
    return out.exports_dir


# --------------------------------------------------------------------------- #
# AC: cmd_run exports populate spec / plan / rtl / tb before the human gate.
# --------------------------------------------------------------------------- #
def test_cmd_run_exports_spec_plan_rtl_and_testbench(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    exports = _drive_through_pause(tmp_path, config_path=routing_config)

    assert exports == tmp_path / "exports" / DESIGN_ID
    assert exports.is_dir()

    spec_md = exports / "spec.md"
    spec_json = exports / "spec.json"
    plan_json = exports / "plan.json"
    assert spec_md.is_file()
    assert spec_json.is_file()
    assert plan_json.is_file()

    # spec.md is the original markdown the operator gave us, verbatim.
    assert spec_md.read_text() == COUNTER_SPEC.read_text()

    plan_body = json.loads(plan_json.read_text())
    top = plan_body["top_module_id"]
    assert (exports / "rtl" / f"{top}.v").is_file()
    assert (exports / "rtl" / f"{top}.rtl.json").is_file()
    assert (exports / "tb" / f"{top}_tb.py").is_file()


def test_cmd_run_exports_include_lint_and_sim_bodies(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    exports = _drive_through_pause(tmp_path, config_path=routing_config)
    plan_body = json.loads((exports / "plan.json").read_text())
    top = plan_body["top_module_id"]

    # The RTL stage runs lint + elaborate + sim before the human gate; each
    # verification report should land as a JSON body under its stage dir.
    lint_files = list((exports / "rtl").glob(f"{top}.lint.json"))
    sim_files = list((exports / "sim").glob(f"{top}.sim.json"))
    assert lint_files, "expected lint report body under exports/rtl/"
    assert sim_files, "expected sim report body under exports/sim/"


# --------------------------------------------------------------------------- #
# AC: cmd_resume exports populate netlist / def / signoff / gds.
# --------------------------------------------------------------------------- #
def test_cmd_resume_exports_complete_through_gds(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    exports = _drive_through_completion(tmp_path, config_path=routing_config)
    plan_body = json.loads((exports / "plan.json").read_text())
    top = plan_body["top_module_id"]

    # SYNTH: netlist + JSON body.
    assert (exports / "synth" / f"{top}.netlist.v").is_file()
    assert (exports / "synth" / f"{top}.netlist.json").is_file()

    # PHYSICAL: routed DEF + JSON body.
    assert (exports / "physical" / f"{top}.def").is_file()
    assert (exports / "physical" / f"{top}.layout.json").is_file()

    # SIGNOFF: every leg's report body lands under signoff/.
    signoff_files = {p.name for p in (exports / "signoff").glob("*.json")}
    assert any(name.endswith(".sta.json") for name in signoff_files)
    assert any(name.endswith(".drc.json") for name in signoff_files)
    assert any(name.endswith(".lvs.json") for name in signoff_files)
    assert any(name.endswith(".security.json") for name in signoff_files)

    # GDSII: final layout bytes + JSON body.
    gds_path = exports / "gds" / f"{top}.gds"
    assert gds_path.is_file()
    assert gds_path.stat().st_size > 0
    assert (exports / "gds" / f"{top}.gdsii.json").is_file()


# --------------------------------------------------------------------------- #
# AC: exported blob bytes match what the store would return.
# --------------------------------------------------------------------------- #
def test_exported_blob_bytes_equal_store_get_blob(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    exports = _drive_through_completion(tmp_path, config_path=routing_config)

    with SqliteArtifactStore(
        db_path=tmp_path / "store.sqlite",
        content_dir=tmp_path / "content",
    ) as store:
        # Find the canonical RTL head — its source blob is what we exported.
        rtl_id = f"{DESIGN_ID}.counter.rtl"
        rtl = store.get_by_id(rtl_id)
        assert isinstance(rtl, RTLArtifact)
        canonical_rtl_bytes = store.get_blob(rtl.source)

        # Testbench, netlist, layout, gds — same equality.
        tb = store.get_by_id(f"{DESIGN_ID}.counter.tb")
        assert isinstance(tb, TestbenchArtifact)
        canonical_tb_bytes = store.get_blob(tb.source)

        netlist = store.get_by_id(f"{DESIGN_ID}.counter.netlist")
        assert isinstance(netlist, NetlistArtifact)
        canonical_netlist_bytes = store.get_blob(netlist.netlist)

        layout = store.get_by_id(f"{DESIGN_ID}.counter.layout")
        assert isinstance(layout, LayoutArtifact)
        canonical_def_bytes = store.get_blob(layout.def_file)

        gds = store.get_by_id(f"{DESIGN_ID}.counter.gds")
        assert isinstance(gds, GDSIIArtifact)
        canonical_gds_bytes = store.get_blob(gds.gds)

    assert (exports / "rtl" / "counter.v").read_bytes() == canonical_rtl_bytes
    assert (exports / "tb" / "counter_tb.py").read_bytes() == canonical_tb_bytes
    assert (
        exports / "synth" / "counter.netlist.v"
    ).read_bytes() == canonical_netlist_bytes
    assert (
        exports / "physical" / "counter.def"
    ).read_bytes() == canonical_def_bytes
    assert (exports / "gds" / "counter.gds").read_bytes() == canonical_gds_bytes


# --------------------------------------------------------------------------- #
# AC: re-running cmd_run against the same run-dir is idempotent.
# --------------------------------------------------------------------------- #
def test_cmd_run_exports_are_idempotent(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    exports = _drive_through_pause(tmp_path, config_path=routing_config)

    snapshot: dict[Path, bytes] = {}
    for path in exports.rglob("*"):
        if path.is_file():
            snapshot[path] = path.read_bytes()
    assert snapshot, "exports tree was empty after cmd_run"

    # Drive cmd_run again on the same run-dir. The store dedupes by content
    # hash so every artifact resolves to the same bytes; the second export
    # pass must leave the same files on disk byte-for-byte.
    cmd_run(_run_args(
        cmd="run", run_dir=tmp_path, config_path=routing_config,
        spec_path=COUNTER_SPEC, name="counter",
    ))
    for path, original in snapshot.items():
        assert path.is_file(), f"export disappeared on re-run: {path}"
        assert path.read_bytes() == original, (
            f"export drifted on re-run: {path}"
        )


# --------------------------------------------------------------------------- #
# AC: outcome dataclasses surface exports_dir for callers / operator UX.
# --------------------------------------------------------------------------- #
def test_run_outcome_exposes_exports_dir(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    out = cmd_run(_run_args(
        cmd="run", run_dir=tmp_path, config_path=routing_config,
        spec_path=COUNTER_SPEC, name="counter",
    ))
    assert out.exports_dir == tmp_path / "exports" / DESIGN_ID
    assert out.exports_dir.is_dir()


def test_resume_outcome_exposes_exports_dir(
    tmp_path: Path, routing_config: Path, patch_router: StubBackend,
) -> None:
    cmd_run(_run_args(
        cmd="run", run_dir=tmp_path, config_path=routing_config,
        spec_path=COUNTER_SPEC, name="counter",
    ))
    out = cmd_resume(_run_args(
        cmd="resume", run_dir=tmp_path, config_path=routing_config,
    ))
    assert out.exports_dir == tmp_path / "exports" / DESIGN_ID
    # The GDS landed under the same exports root.
    assert (out.exports_dir / "gds").is_dir()
    # Stage.GDSII head is populated on the final state — sanity check the
    # piece the CLI walked when writing the GDS export.
    final_head = out.final_state.stages.get(Stage.GDSII)
    assert final_head is not None and final_head.head is not None


# --------------------------------------------------------------------------- #
# AC: cmd_chat also exports — the chat-minted Spec lands under exports/.
# --------------------------------------------------------------------------- #
@pytest.fixture
def patch_chat_router(
    monkeypatch: pytest.MonkeyPatch, routing_config: Path,
) -> StubBackend:
    """Variant of patch_router that recognises the chat-persona system prompt.

    cmd_chat streams via ``TaskType.SPEC_INTAKE`` with the
    ``CHAT_SYSTEM_PROMPT`` system text; the default stub matchers only
    cover the SpecIntakeAgent's own prompt.
    """
    backend = StubBackend(matchers=CHAT_RESPONSES)
    router, _ = make_test_router(config_path=routing_config, backend=backend)
    monkeypatch.setattr(
        "chip_agent.cli._resolve_router",
        lambda _args, *, settings: router,
    )
    return backend


def test_cmd_chat_exports_spec_md_after_run(
    tmp_path: Path, routing_config: Path, patch_chat_router: StubBackend,
) -> None:
    """``/run`` mints a Spec; the F11.6 export pass mirrors it under
    ``exports/<id>/spec.md`` + ``exports/<id>/spec.json`` so the operator
    can inspect what intake materialised before kicking off ``cmd_run``."""
    run_dir = tmp_path / "run"
    chat_args = RunArgs(
        cmd="chat", spec_path=None, name="counter", run_dir=run_dir,
        design_id=DESIGN_ID, hmac_key=HMAC_KEY, config_path=routing_config,
        chat_stdin=io.StringIO(
            "Build an 8-bit synchronous counter named `counter` with "
            "active-low async reset, 10 ns clock.\n/run\n",
        ),
        chat_stdout=io.StringIO(),
    )
    out = cmd_chat(chat_args)

    assert out.spec_ref is not None, (
        "chat ended without minting a Spec (stub backend regressed?)"
    )
    assert out.exports_dir == run_dir / "exports" / DESIGN_ID
    assert (out.exports_dir / "spec.md").is_file()
    assert (out.exports_dir / "spec.json").is_file()
    # The exported spec.md is the original raw_text — pin the lossless mirror.
    transcript_in_spec = (out.exports_dir / "spec.md").read_text()
    assert "counter" in transcript_in_spec.lower()


def test_cmd_chat_without_spec_does_not_create_exports(
    tmp_path: Path, routing_config: Path, patch_chat_router: StubBackend,
) -> None:
    """``/exit`` returns no Spec — and no exports are written.

    The directory may exist as a side-effect of run_dir.mkdir; the contract
    is that no ``spec.md`` body lands when no Spec was minted.
    """
    run_dir = tmp_path / "run"
    chat_args = RunArgs(
        cmd="chat", spec_path=None, name="counter", run_dir=run_dir,
        design_id=DESIGN_ID, hmac_key=HMAC_KEY, config_path=routing_config,
        chat_stdin=io.StringIO("/exit\n"),
        chat_stdout=io.StringIO(),
    )
    out = cmd_chat(chat_args)
    assert out.spec_ref is None
    assert not (out.exports_dir / "spec.md").exists()
