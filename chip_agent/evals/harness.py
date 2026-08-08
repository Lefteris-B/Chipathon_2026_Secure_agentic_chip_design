"""Eval harness over VerilogEval / RTLLM-style benchmarks (F7.4).

Runs a suite of :class:`EvalProblem`s through a pluggable
:class:`Solver` (the RTL agent) + :class:`Verifier` (lint + elaborate +
sim) seam, captures pass / fail and PPA metrics per attempt, and emits a
deterministic :class:`Scorecard` whose CSV bytes are stable across runs
seeded the same way. CLAUDE.md fingerprints VerilogEval + RTLLM as the
benchmark sources; the harness is neutral to which suite is loaded so
the same scorecard machinery serves both.

The two F7.4 AC strands:

* **Scorecard CSV.** :meth:`Scorecard.to_csv` writes one row per
  problem with the pass@k columns the caller requested plus the PPA
  averages computed over passing attempts only.
* **Reproducibility.** Solver and Verifier receive a ``(seed,
  sample_idx)`` pair per attempt; a deterministic implementation
  (test fixture or real LLM with a frozen seed) produces identical
  outcomes across runs, so the CSV bytes are stable.

This module ships the harness + metrics + I/O. The Solver / Verifier
implementations sit alongside the M4 RTL stage and M6 backend stages;
the F7.4 tests use small in-memory stubs that exercise the surface.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

__all__ = [
    "EvalAttempt",
    "EvalProblem",
    "EvalResult",
    "EvalRunner",
    "Scorecard",
    "Solver",
    "Verifier",
    "VerifyOutcome",
    "pass_at_k",
]


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvalProblem:
    """One benchmark problem the harness drives end-to-end."""

    problem_id: str
    name: str
    description: str
    prompt: str
    testbench: str
    top_module: str
    reference_solution: str | None = None


@dataclass(frozen=True)
class VerifyOutcome:
    """Single attempt's verifier verdict + PPA hint."""

    passed: bool
    ppa: dict[str, float] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class EvalAttempt:
    """One (problem, sample_idx) attempt's record."""

    problem_id: str
    sample_idx: int
    passed: bool
    ppa: dict[str, float] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class EvalResult:
    """Aggregated record for one problem across ``n_samples`` attempts."""

    problem_id: str
    name: str
    n_samples: int
    n_pass: int
    attempts: list[EvalAttempt]
    ppa_avg: dict[str, float]


# --------------------------------------------------------------------------- #
# pass@k — the standard unbiased estimator (Chen et al., 2021)
# --------------------------------------------------------------------------- #
def pass_at_k(*, n: int, c: int, k: int) -> float:
    """Compute the unbiased pass@k estimator.

    Args:
        n: number of samples drawn for the problem.
        c: number of those samples that passed.
        k: the ``k`` in pass@k.

    The estimator is :math:`1 - \\binom{n-c}{k} / \\binom{n}{k}` when
    ``n - c >= k``; if fewer than ``k`` samples failed, every k-subset
    must contain at least one passing sample, so pass@k = 1.

    Edge cases:

    * ``k <= 0`` raises ``ValueError`` — pass@0 is undefined.
    * ``k > n`` raises ``ValueError`` — can't draw more than ``n``.
    * ``c < 0`` or ``c > n`` raises ``ValueError``.
    """
    if k <= 0:
        raise ValueError("k must be >= 1")
    if n <= 0:
        raise ValueError("n must be >= 1")
    if k > n:
        raise ValueError(f"k ({k}) cannot exceed n ({n})")
    if c < 0 or c > n:
        raise ValueError(f"c ({c}) must lie in [0, n={n}]")
    if n - c < k:
        return 1.0
    # 1 - C(n-c, k) / C(n, k). Use math.comb (exact integer) for accuracy.
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


# --------------------------------------------------------------------------- #
# Solver / Verifier seams
# --------------------------------------------------------------------------- #
class Solver(Protocol):
    """One RTL-generation attempt — the stand-in for the rtl_gen agent.

    Production wires :class:`~chip_agent.agents.rtl_gen.RTLGenerationAgent`
    behind this Protocol; tests inject deterministic stubs.
    """

    def solve(
        self,
        problem: EvalProblem,
        *,
        sample_idx: int,
        seed: int,
    ) -> str: ...


class Verifier(Protocol):
    """One verification pass — lint + elaborate + sim + (optional) PPA pull.

    Production wires the F4.4 / F4.5 stack behind this Protocol; tests
    inject pass/fail stubs that also seed PPA metrics for the scorecard.
    """

    def verify(self, problem: EvalProblem, rtl: str) -> VerifyOutcome: ...


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class EvalRunner:
    """Drive ``n_samples`` attempts per problem and assemble a :class:`Scorecard`."""

    def __init__(
        self,
        *,
        solver: Solver,
        verifier: Verifier,
        n_samples: int = 10,
        seed: int = 0,
    ) -> None:
        if n_samples < 1:
            raise ValueError("n_samples must be >= 1")
        self.solver = solver
        self.verifier = verifier
        self.n_samples = n_samples
        self.seed = seed

    def run(
        self,
        problems: Iterable[EvalProblem],
        *,
        suite_name: str = "eval",
    ) -> Scorecard:
        """Run every problem ``n_samples`` times and aggregate."""
        results: list[EvalResult] = []
        for problem in problems:
            result = self._run_one(problem)
            results.append(result)
        return Scorecard(
            suite_name=suite_name,
            seed=self.seed,
            n_samples=self.n_samples,
            results=results,
        )

    def _run_one(self, problem: EvalProblem) -> EvalResult:
        attempts: list[EvalAttempt] = []
        n_pass = 0
        ppa_sums: dict[str, float] = {}
        passing_count = 0
        for sample_idx in range(self.n_samples):
            rtl = self.solver.solve(
                problem, sample_idx=sample_idx, seed=self.seed,
            )
            outcome = self.verifier.verify(problem, rtl)
            attempts.append(EvalAttempt(
                problem_id=problem.problem_id,
                sample_idx=sample_idx,
                passed=outcome.passed,
                ppa=dict(outcome.ppa),
                error=outcome.error,
            ))
            if outcome.passed:
                n_pass += 1
                passing_count += 1
                for k, v in outcome.ppa.items():
                    ppa_sums[k] = ppa_sums.get(k, 0.0) + float(v)

        ppa_avg = {
            k: (v / passing_count if passing_count > 0 else 0.0)
            for k, v in ppa_sums.items()
        }
        return EvalResult(
            problem_id=problem.problem_id,
            name=problem.name,
            n_samples=self.n_samples,
            n_pass=n_pass,
            attempts=attempts,
            ppa_avg=ppa_avg,
        )


# --------------------------------------------------------------------------- #
# Scorecard + CSV emit
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scorecard:
    """Aggregated scorecard for one run."""

    suite_name: str
    seed: int
    n_samples: int
    results: list[EvalResult]

    def overall_pass_at_k(self, k: int) -> float:
        """Mean pass@k across every problem in the scorecard."""
        if not self.results:
            return 0.0
        return sum(
            pass_at_k(n=r.n_samples, c=r.n_pass, k=k) for r in self.results
        ) / len(self.results)

    def to_csv(
        self,
        path: Path | str,
        *,
        ks: Iterable[int] = (1, 5, 10),
        ppa_columns: Iterable[str] = ("cell_count", "wns_ns", "area_um2"),
    ) -> None:
        """Write the scorecard to ``path`` as CSV; bytes are deterministic.

        One row per problem in the order it was scored. Columns:

        * problem_id, name, n_samples, n_pass
        * pass@k for each ``k`` (clamped to ``k <= n_samples``)
        * PPA averages over passing attempts only, in the requested
          column order
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_csv_text(ks=ks, ppa_columns=ppa_columns)
        path.write_text(text, newline="")

    def to_csv_text(
        self,
        *,
        ks: Iterable[int] = (1, 5, 10),
        ppa_columns: Iterable[str] = ("cell_count", "wns_ns", "area_um2"),
    ) -> str:
        """Return the CSV text without touching disk (handy for tests)."""
        ks_list = [int(k) for k in ks]
        ppa_list = list(ppa_columns)
        if any(k < 1 for k in ks_list):
            raise ValueError("ks must be positive")

        header: list[str] = [
            "problem_id", "name", "n_samples", "n_pass",
            *[f"pass@{k}" for k in ks_list],
            *ppa_list,
        ]
        buf = io.StringIO()
        # ``lineterminator="\n"`` so CSV bytes don't depend on the host OS;
        # ``QUOTE_MINIMAL`` keeps deterministic quoting.
        writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for result in self.results:
            row: list[str] = [
                result.problem_id,
                result.name,
                str(result.n_samples),
                str(result.n_pass),
            ]
            for k in ks_list:
                row.append(_fmt_pass_at_k(result, k=k))
            for col in ppa_list:
                row.append(_fmt_metric(result.ppa_avg.get(col, 0.0)))
            writer.writerow(row)
        return buf.getvalue()


def _fmt_pass_at_k(result: EvalResult, *, k: int) -> str:
    if k > result.n_samples:
        return ""  # pass@k undefined for k > n_samples
    return _fmt_metric(
        pass_at_k(n=result.n_samples, c=result.n_pass, k=k),
    )


def _fmt_metric(value: float) -> str:
    """Format a float with stable trailing zeros — same value → same bytes."""
    if value == 0.0:
        return "0.0000"
    return f"{value:.4f}"


# Convenience for tests + dataset loaders.
def aggregate_ppa(attempts: Iterable[EvalAttempt]) -> Mapping[str, float]:
    """Mean of each PPA key over the passing subset of ``attempts``."""
    sums: dict[str, float] = {}
    n = 0
    for a in attempts:
        if not a.passed:
            continue
        n += 1
        for k, v in a.ppa.items():
            sums[k] = sums.get(k, 0.0) + float(v)
    if n == 0:
        return {}
    return {k: v / n for k, v in sums.items()}
