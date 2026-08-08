"""Subprocess runner for F19.6 OracleVerificationGate.

Pure stdlib. Invoked as ``python runner.py <work_dir>``. The gate
copies this file into a freshly-created TemporaryDirectory, alongside
the oracle source, assertion source, and stimulus JSON, then runs
this script via :class:`SubprocessProcessRunner`.

Reads from ``<work_dir>``:
  * ``oracle.py``      — F19.4 reference module (must define a
                          top-level callable named per ``input.json``'s
                          ``reference_fn_name`` field).
  * ``assertions.py``  — F19.5 assertion module (defines ``assert_*``
                          functions; one per ``input.json``'s
                          ``callsites`` entries).
  * ``input.json``     — ``{"reference_fn_name", "stim", "callsites"}``.

Writes to ``<work_dir>``:
  * ``result.json``    — ``{"stim_cycles", "assertions_checked",
                            "assertions_passed", "results", "observed",
                            "duration_s"}``
    where each entry in ``results`` is ``{"name", "callsite", "passed",
    "message"}`` plus an optional ``"traceback"`` for assertions that
    raised an exception. ``observed`` is the per-cycle output list
    that ``reference(stim)`` returned — F19.8 reads it to embed
    expected outputs into the differential cocotb harness;
    F19.6's :class:`OracleVerificationGate` does not read it (additive
    field).

This script is NOT part of the agent's import graph at runtime — the
gate copies it into the tmp dir before invoking it via subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any


def _load_module(name: str, path: Path) -> Any:
    """Load a Python source file as a fresh module."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"can't load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(work_dir: Path) -> None:
    args = json.loads((work_dir / "input.json").read_text())
    reference_fn_name = args["reference_fn_name"]
    stim = args["stim"]
    callsites = args["callsites"]

    start = time.monotonic()

    oracle = _load_module("oracle", work_dir / "oracle.py")
    reference = getattr(oracle, reference_fn_name)
    observed = reference(stim)

    assertions = _load_module("assertions", work_dir / "assertions.py")

    results: list[dict[str, Any]] = []
    for cs in callsites:
        name = cs["name"]
        callsite = cs["callsite"]
        try:
            fn = getattr(assertions, callsite)
            passed, message = fn((stim, observed))
            results.append({
                "name": name,
                "callsite": callsite,
                "passed": bool(passed),
                "message": str(message),
            })
        except Exception as e:
            results.append({
                "name": name,
                "callsite": callsite,
                "passed": False,
                "message": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            })

    duration_s = time.monotonic() - start
    payload = {
        "stim_cycles": len(stim),
        "assertions_checked": len(results),
        "assertions_passed": sum(1 for r in results if r["passed"]),
        "results": results,
        # F19.8: per-cycle oracle output. The differential TB builder
        # embeds this into the cocotb harness as ``EXPECTED``; the
        # F19.6 OracleVerificationGate doesn't read it.
        "observed": observed,
        "duration_s": duration_s,
    }
    (work_dir / "result.json").write_text(json.dumps(payload))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
