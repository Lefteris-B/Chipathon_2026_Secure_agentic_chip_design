"""F7.4 acceptance: produces a scorecard CSV; numbers reproducible across runs
with fixed seeds."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chip_agent.evals.harness import (
    EvalAttempt,
    EvalProblem,
    EvalRunner,
    Scorecard,
    VerifyOutcome,
    aggregate_ppa,
    pass_at_k,
)


# --------------------------------------------------------------------------- #
# Fixtures: a small synthetic suite + deterministic stubs
# --------------------------------------------------------------------------- #
def _problems() -> list[EvalProblem]:
    return [
        EvalProblem(
            problem_id="VEVAL/counter4",
            name="counter4",
            description="4-bit synchronous counter",
            prompt="Build a 4-bit synchronous counter with active-low reset.",
            testbench="// cocotb test\n",
            top_module="counter",
        ),
        EvalProblem(
            problem_id="VEVAL/mux2",
            name="mux2",
            description="2:1 multiplexer",
            prompt="Build a 2:1 mux.",
            testbench="// cocotb test\n",
            top_module="mux",
        ),
        EvalProblem(
            problem_id="VEVAL/adder8",
            name="adder8",
            description="8-bit ripple-carry adder",
            prompt="Build an 8-bit adder.",
            testbench="// cocotb test\n",
            top_module="adder",
        ),
    ]


@dataclass
class StubSolver:
    """A deterministic solver — same (problem, sample_idx, seed) -> same RTL."""

    calls: list[tuple[str, int, int]] = field(default_factory=list)

    def solve(self, problem: EvalProblem, *, sample_idx: int, seed: int) -> str:
        self.calls.append((problem.problem_id, sample_idx, seed))
        # Deterministic content: encodes the inputs so different seeds /
        # sample_idx values produce different RTL bytes.
        return (
            f"// {problem.problem_id} sample={sample_idx} seed={seed}\n"
            f"module {problem.top_module}; endmodule\n"
        )


@dataclass
class StubVerifier:
    """Verdict + PPA driven by a per-problem schedule.

    For each problem, callers can specify which sample indices pass
    (everything else fails). PPA values rise with sample_idx so the
    averaging logic is testable.
    """

    pass_schedule: dict[str, set[int]]
    base_ppa: dict[str, float] = field(
        default_factory=lambda: {"cell_count": 40.0, "wns_ns": 0.3, "area_um2": 100.0}
    )
    calls: list[tuple[str, str]] = field(default_factory=list)

    def verify(self, problem: EvalProblem, rtl: str) -> VerifyOutcome:
        self.calls.append((problem.problem_id, rtl))
        # Figure out which sample_idx this is by reading the encoded RTL.
        sample_idx = _extract_sample_idx(rtl)
        passing = sample_idx in self.pass_schedule.get(problem.problem_id, set())
        if not passing:
            return VerifyOutcome(passed=False, error="sim failed")
        ppa = {k: v + sample_idx for k, v in self.base_ppa.items()}
        return VerifyOutcome(passed=True, ppa=ppa)


def _extract_sample_idx(rtl: str) -> int:
    # RTL header is "// <pid> sample=<idx> seed=<seed>"; pull idx out.
    for tok in rtl.split():
        if tok.startswith("sample="):
            return int(tok.split("=", 1)[1])
    raise AssertionError(f"no sample=<n> token in stub RTL: {rtl!r}")


# --------------------------------------------------------------------------- #
# pass_at_k formula
# --------------------------------------------------------------------------- #
def test_pass_at_k_all_pass_is_one() -> None:
    assert pass_at_k(n=10, c=10, k=1) == pytest.approx(1.0)
    assert pass_at_k(n=10, c=10, k=10) == pytest.approx(1.0)


def test_pass_at_k_none_pass_is_zero() -> None:
    assert pass_at_k(n=10, c=0, k=1) == 0.0
    assert pass_at_k(n=10, c=0, k=10) == 0.0


def test_pass_at_k_single_pass_at_k1_is_one_over_n() -> None:
    # 1 sample passing out of 10 -> pass@1 = 1/10 = 0.1.
    assert pass_at_k(n=10, c=1, k=1) == pytest.approx(0.1)


def test_pass_at_k_matches_codex_formula() -> None:
    # Known reference: n=10, c=3, k=5
    # pass@5 = 1 - C(7, 5) / C(10, 5) = 1 - 21/252 = 1 - 1/12 ≈ 0.9167
    assert pass_at_k(n=10, c=3, k=5) == pytest.approx(1 - 21 / 252)


def test_pass_at_k_when_failures_smaller_than_k_is_one() -> None:
    # n=10, c=8 (so n-c=2 < k=5) -> every k=5 subset includes a pass.
    assert pass_at_k(n=10, c=8, k=5) == 1.0


def test_pass_at_k_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="k must be"):
        pass_at_k(n=10, c=0, k=0)
    with pytest.raises(ValueError, match="n must be"):
        pass_at_k(n=0, c=0, k=1)
    with pytest.raises(ValueError, match="cannot exceed"):
        pass_at_k(n=5, c=0, k=10)
    with pytest.raises(ValueError, match=r"c \(.*\) must lie in"):
        pass_at_k(n=10, c=-1, k=1)
    with pytest.raises(ValueError, match=r"c \(.*\) must lie in"):
        pass_at_k(n=10, c=11, k=1)


# --------------------------------------------------------------------------- #
# Runner: collects per-problem results
# --------------------------------------------------------------------------- #
def test_runner_executes_n_samples_per_problem() -> None:
    solver = StubSolver()
    verifier = StubVerifier(pass_schedule={p.problem_id: set() for p in _problems()})
    runner = EvalRunner(solver=solver, verifier=verifier, n_samples=4, seed=7)
    card = runner.run(_problems(), suite_name="VerilogEval")

    assert card.suite_name == "VerilogEval"
    assert card.seed == 7
    assert card.n_samples == 4
    assert len(card.results) == 3
    # Solver was called 4 times per problem.
    by_pid = {pid for pid, _, _ in solver.calls}
    assert by_pid == {p.problem_id for p in _problems()}
    assert len(solver.calls) == 4 * 3
    # All attempts failed -> n_pass = 0 per problem.
    for result in card.results:
        assert result.n_pass == 0


def test_runner_records_pass_counts() -> None:
    # counter4 passes on samples {0, 2}; mux2 passes on {0, 1, 3}; adder8 fails all.
    schedule = {
        "VEVAL/counter4": {0, 2},
        "VEVAL/mux2": {0, 1, 3},
        "VEVAL/adder8": set(),
    }
    runner = EvalRunner(
        solver=StubSolver(),
        verifier=StubVerifier(pass_schedule=schedule),
        n_samples=4, seed=0,
    )
    card = runner.run(_problems())

    by_pid = {r.problem_id: r for r in card.results}
    assert by_pid["VEVAL/counter4"].n_pass == 2
    assert by_pid["VEVAL/mux2"].n_pass == 3
    assert by_pid["VEVAL/adder8"].n_pass == 0


def test_runner_averages_ppa_over_passing_attempts() -> None:
    # mux2 passes on samples {0, 4}. base cell_count=40, +sample_idx.
    # Average cell_count over passes = (40 + 44) / 2 = 42.0.
    schedule = {p.problem_id: set() for p in _problems()}
    schedule["VEVAL/mux2"] = {0, 4}
    runner = EvalRunner(
        solver=StubSolver(),
        verifier=StubVerifier(pass_schedule=schedule),
        n_samples=5, seed=0,
    )
    card = runner.run(_problems())
    by_pid = {r.problem_id: r for r in card.results}
    assert by_pid["VEVAL/mux2"].ppa_avg["cell_count"] == pytest.approx(42.0)
    # Failed problem has empty ppa_avg (no passing attempts).
    assert by_pid["VEVAL/adder8"].ppa_avg == {}


def test_runner_rejects_invalid_n_samples() -> None:
    with pytest.raises(ValueError):
        EvalRunner(solver=StubSolver(), verifier=StubVerifier(pass_schedule={}),
                   n_samples=0)


def test_runner_seed_threads_to_solver() -> None:
    solver = StubSolver()
    runner = EvalRunner(
        solver=solver, verifier=StubVerifier(pass_schedule={}),
        n_samples=2, seed=42,
    )
    runner.run(_problems()[:1])
    seeds = {seed for _, _, seed in solver.calls}
    assert seeds == {42}


# --------------------------------------------------------------------------- #
# Overall pass@k
# --------------------------------------------------------------------------- #
def test_overall_pass_at_k_averages_problems() -> None:
    schedule = {
        "VEVAL/counter4": {0, 2, 4, 6, 8},  # 5/10 pass -> pass@1 = 0.5
        "VEVAL/mux2": set(),                  # 0/10 pass -> pass@1 = 0
        "VEVAL/adder8": {0},                  # 1/10 pass -> pass@1 = 0.1
    }
    runner = EvalRunner(
        solver=StubSolver(),
        verifier=StubVerifier(pass_schedule=schedule),
        n_samples=10, seed=0,
    )
    card = runner.run(_problems())
    overall = card.overall_pass_at_k(1)
    assert overall == pytest.approx((0.5 + 0.0 + 0.1) / 3)


def test_overall_pass_at_k_empty_scorecard_is_zero() -> None:
    card = Scorecard(suite_name="empty", seed=0, n_samples=0, results=[])
    assert card.overall_pass_at_k(1) == 0.0


# --------------------------------------------------------------------------- #
# CSV emit + reproducibility (the F7.4 AC strands)
# --------------------------------------------------------------------------- #
def test_csv_header_includes_requested_pass_at_ks_and_ppa() -> None:
    schedule = {p.problem_id: set() for p in _problems()}
    schedule["VEVAL/counter4"] = {0, 1, 2}
    runner = EvalRunner(
        solver=StubSolver(),
        verifier=StubVerifier(pass_schedule=schedule),
        n_samples=5, seed=0,
    )
    card = runner.run(_problems())
    text = card.to_csv_text(ks=(1, 5), ppa_columns=("cell_count", "wns_ns"))
    header = text.splitlines()[0].split(",")
    assert header == [
        "problem_id", "name", "n_samples", "n_pass",
        "pass@1", "pass@5", "cell_count", "wns_ns",
    ]


def test_csv_row_shape_is_one_row_per_problem(tmp_path: Path) -> None:
    schedule = {p.problem_id: set() for p in _problems()}
    runner = EvalRunner(
        solver=StubSolver(),
        verifier=StubVerifier(pass_schedule=schedule),
        n_samples=2, seed=0,
    )
    card = runner.run(_problems())
    path = tmp_path / "scorecard.csv"
    card.to_csv(path)
    lines = path.read_text().splitlines()
    # 1 header + 3 data rows.
    assert len(lines) == 4


def test_csv_bytes_are_reproducible_across_runs(tmp_path: Path) -> None:
    # F7.4 AC: numbers reproducible across runs with fixed seeds.
    schedule = {
        "VEVAL/counter4": {0, 2, 4},
        "VEVAL/mux2": {1, 3},
        "VEVAL/adder8": {0, 1, 2, 3, 4, 5},
    }

    def _run() -> bytes:
        runner = EvalRunner(
            solver=StubSolver(),
            verifier=StubVerifier(pass_schedule=schedule),
            n_samples=10, seed=42,
        )
        card = runner.run(_problems(), suite_name="VerilogEval")
        return card.to_csv_text().encode()

    bytes_a = _run()
    bytes_b = _run()
    bytes_c = _run()
    assert bytes_a == bytes_b == bytes_c


def test_csv_pass_at_k_columns_clamp_to_n_samples(tmp_path: Path) -> None:
    schedule = {p.problem_id: {0, 1} for p in _problems()}
    runner = EvalRunner(
        solver=StubSolver(),
        verifier=StubVerifier(pass_schedule=schedule),
        n_samples=3, seed=0,
    )
    card = runner.run(_problems())
    text = card.to_csv_text(ks=(1, 3, 5))
    # pass@5 column exists but is empty for every row (n_samples=3 < k=5).
    header, *rows = text.splitlines()
    cols = header.split(",")
    pass_at_5_idx = cols.index("pass@5")
    for row in rows:
        assert row.split(",")[pass_at_5_idx] == ""


def test_csv_rejects_nonpositive_ks() -> None:
    card = Scorecard(suite_name="x", seed=0, n_samples=1, results=[])
    with pytest.raises(ValueError):
        card.to_csv_text(ks=(0, 1))


def test_csv_writes_to_disk(tmp_path: Path) -> None:
    schedule = {p.problem_id: {0} for p in _problems()}
    runner = EvalRunner(
        solver=StubSolver(),
        verifier=StubVerifier(pass_schedule=schedule),
        n_samples=2, seed=0,
    )
    card = runner.run(_problems(), suite_name="VerilogEval")
    out = tmp_path / "nested" / "dir" / "scorecard.csv"
    card.to_csv(out)
    assert out.exists()
    assert out.read_text().startswith("problem_id,name,n_samples,n_pass")


# --------------------------------------------------------------------------- #
# aggregate_ppa helper
# --------------------------------------------------------------------------- #
def test_aggregate_ppa_averages_passing_attempts_only() -> None:
    attempts = [
        EvalAttempt(problem_id="p", sample_idx=0, passed=True,
                    ppa={"cell_count": 10.0}),
        EvalAttempt(problem_id="p", sample_idx=1, passed=False,
                    ppa={"cell_count": 99.0}),  # ignored — failed sample
        EvalAttempt(problem_id="p", sample_idx=2, passed=True,
                    ppa={"cell_count": 20.0}),
    ]
    avg = aggregate_ppa(attempts)
    assert avg == {"cell_count": pytest.approx(15.0)}


def test_aggregate_ppa_empty_for_no_passes() -> None:
    attempts = [
        EvalAttempt(problem_id="p", sample_idx=0, passed=False, ppa={"x": 1}),
    ]
    assert aggregate_ppa(attempts) == {}


# --------------------------------------------------------------------------- #
# Determinism sanity: solver call order is the same across runs
# --------------------------------------------------------------------------- #
def test_solver_call_order_is_deterministic() -> None:
    runner = EvalRunner(
        solver=StubSolver(),
        verifier=StubVerifier(pass_schedule={p.problem_id: set() for p in _problems()}),
        n_samples=3, seed=0,
    )
    # Call twice; the same (problem_id, sample_idx, seed) sequence falls out.
    s1 = StubSolver()
    s2 = StubSolver()
    EvalRunner(
        solver=s1, verifier=StubVerifier(pass_schedule={}),
        n_samples=3, seed=0,
    ).run(_problems())
    EvalRunner(
        solver=s2, verifier=StubVerifier(pass_schedule={}),
        n_samples=3, seed=0,
    ).run(_problems())
    assert s1.calls == s2.calls
    del runner  # silence unused-var warning
    # Sanity: log(N) ordering is by-problem then by-sample.
    assert math.log(1 + len(s1.calls)) > 0  # smoke
