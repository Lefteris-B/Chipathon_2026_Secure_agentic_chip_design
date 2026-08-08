"""F19.7 — deterministic stimulus ramp for the M19 Phase 1
OracleVerificationGate.

Pure function over :class:`ModuleDecl.ports`. The verification node
feeds the produced stim into the gate so two runs of the same module
produce the same OracleVerificationArtifact content hash (the verdict's
identity is a function of ``(oracle_ref, assertion_spec_ref, stim)``).

The ramp is intentionally simple. Phase 1's verification is warning-
only, so the goal is to exercise reset + a few cycles of input
activity — enough to catch a counter that increments by the wrong
constant or a missing reset path. F20.7 will replace this with
coverage-driven stimulus generation.
"""

from __future__ import annotations

import random

from chip_agent.design_state import ModuleDecl

__all__ = ["build_ramp_stim", "build_rich_stim"]


# Heuristic name sets, lowercased. Matched against the literal port
# name with ``.lower()`` so common synonyms route consistently.
_CLOCK_NAMES = frozenset({"clk", "clock"})
_ACTIVE_LOW_RESET_NAMES = frozenset({"rst_n", "rstn", "reset_n", "resetn"})
_ACTIVE_HIGH_RESET_NAMES = frozenset({"rst", "reset"})


def build_ramp_stim(
    module: ModuleDecl,
    *,
    cycles: int = 5,
) -> list[dict[str, int]]:
    """Build a fixed ``cycles``-step stimulus from ``module.ports``.

    The shape is fixed across runs to keep the F19.6 gate's verdict
    content hash reproducible (the demo goldens pin it). Heuristics:

    * Input ports whose lowercased name is in
      :data:`_CLOCK_NAMES` are pinned to ``1`` for every cycle. The
      runner doesn't simulate the clock — it just dispatches
      cycle-by-cycle — so a constant 1 is the natural value.
    * Active-low resets (``rst_n``, ``rstn``, ``reset_n``,
      ``resetn``) are ``0`` at cycle 0 (reset asserted), then ``1``.
    * Active-high resets (``rst``, ``reset``) are ``1`` at cycle 0,
      then ``0``.
    * Every other input port is ``0`` for cycles 0 and 1 (reset +
      first-cycle hold), then ramps ``1, 2, 3, ...`` from cycle 2
      onward modulo ``2**width`` (so an 8-bit port stays in
      ``[0, 255]`` and a 1-bit port toggles ``0/1``).
    * Output ports do not appear in stim.

    ``cycles`` must be ``>= 2`` so the reset assert + release
    transition is present. Sub-2 cycle stims would underflow the
    ramp logic.
    """
    if cycles < 2:
        raise ValueError(f"cycles must be >= 2; got {cycles}")

    stim: list[dict[str, int]] = []
    for cycle in range(cycles):
        snapshot: dict[str, int] = {}
        for port in module.ports:
            if port.direction != "in":
                continue
            name_lower = port.name.lower()
            if name_lower in _CLOCK_NAMES:
                snapshot[port.name] = 1
            elif name_lower in _ACTIVE_LOW_RESET_NAMES:
                snapshot[port.name] = 0 if cycle == 0 else 1
            elif name_lower in _ACTIVE_HIGH_RESET_NAMES:
                snapshot[port.name] = 1 if cycle == 0 else 0
            elif cycle < 2:
                snapshot[port.name] = 0
            else:
                # Ramp 1, 2, 3, ... modulo the port width's value
                # space. cycle=2 -> ramp_idx=1, etc.
                modulus = 1 << max(port.width, 1)
                snapshot[port.name] = (cycle - 1) % modulus
        stim.append(snapshot)
    return stim


def build_rich_stim(
    module: ModuleDecl,
    *,
    cycles: int = 24,
    seed: int = 0,
) -> list[dict[str, int]]:
    """Richer stimulus for the F19.8 differential cocotb harness.

    Cycle layout (default ``cycles=24``):

    * cycle 0: reset asserted (active polarity inferred from port
      name)
    * cycle 1: reset released
    * cycles 2-5: data inputs forced to 0
    * cycles 6-9: data inputs forced to all-ones (masked by width)
    * cycles 10-15: ramp 1, 2, 3, ... modulo ``2**width``
    * cycles 16-23: seeded pseudo-random
      (:class:`random.Random(seed)`)

    Clock ports stay at 1 across all cycles. Reset port stays
    deasserted from cycle 1 onward. ``cycles`` must be ``>= 24`` so
    every segment is at least one cycle long; smaller values
    truncate the random segment first.
    """
    if cycles < 2:
        raise ValueError(f"cycles must be >= 2; got {cycles}")

    rng = random.Random(seed)
    stim: list[dict[str, int]] = []
    for cycle in range(cycles):
        snapshot: dict[str, int] = {}
        for port in module.ports:
            if port.direction != "in":
                continue
            name_lower = port.name.lower()
            if name_lower in _CLOCK_NAMES:
                snapshot[port.name] = 1
            elif name_lower in _ACTIVE_LOW_RESET_NAMES:
                snapshot[port.name] = 0 if cycle == 0 else 1
            elif name_lower in _ACTIVE_HIGH_RESET_NAMES:
                snapshot[port.name] = 1 if cycle == 0 else 0
            else:
                modulus = 1 << max(port.width, 1)
                snapshot[port.name] = _rich_value(
                    cycle, modulus, rng,
                )
        stim.append(snapshot)
    return stim


def _rich_value(
    cycle: int, modulus: int, rng: random.Random,
) -> int:
    """Pick the per-cycle data-input value for ``build_rich_stim``.

    Segments laid out so cycle 0/1 = reset hold, then 4-cycle bands
    of zeros / ones / ramp / random. The ``rng.randrange`` call is
    only made inside the random band so segments before it stay
    deterministic regardless of how many ports are sampled in the
    random band.
    """
    if cycle < 2:
        return 0
    if cycle < 6:
        return 0
    if cycle < 10:
        return modulus - 1
    if cycle < 16:
        return (cycle - 9) % modulus
    return rng.randrange(modulus)
