"""HalvingSchedule — plan-table for Sequential Halving rounds.

Default schedule (``round_sizes=(52, 26, 13, 7)`` /
``units_per_arm=(4, 8, 12, 12)``) reflects the 4-round design used by
VocabApp's Phase 1. Consumers override for their scale: JobApp POC
uses ``HalvingSchedule((20, 10, 5), (3, 6, 10))`` for example.

``units_per_arm`` (was ``synsets_per_arm`` in the VocabApp original; renamed
to match the framework's task-unit abstraction) is multiples of the
stratum count by convention so each round sees an equal slice of every
stratum (V/N/A/R for VocabApp, industry verticals for JobApp).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HalvingSchedule:
    """Plan-table for Sequential Halving."""

    round_sizes: tuple[int, ...] = (52, 26, 13, 7)
    """Candidates surviving INTO each round. The last entry is the
    final survivor count (the round-N output is the ranking)."""

    units_per_arm: tuple[int, ...] = (4, 8, 12, 12)
    """Task units assigned per candidate per round.

    Convention: multiples of the stratum count so each round's slice
    is stratum-balanced. Pre-fix history (VocabApp wave-23): the prior
    default ``(3, 4, 6, 12)`` had Round 1 = pool[:3] which under the
    interleave order ``[V0,N0,A0,R0,V1,...]`` dropped adverbs entirely
    from the round-1 elimination decision.
    """

    pool_size: int = 12
    """Total task units in the working pool. Used as the default cap
    when ``n_calls_for_stage`` isn't given an explicit ``pool_size``."""

    def __post_init__(self) -> None:
        if len(self.round_sizes) != len(self.units_per_arm):
            raise ValueError(
                f"HalvingSchedule: round_sizes has {len(self.round_sizes)} "
                f"entries but units_per_arm has {len(self.units_per_arm)} — "
                f"they must be the same length (one entry per round)",
            )
        if not self.round_sizes:
            raise ValueError("HalvingSchedule: round_sizes must not be empty")
        if any(n <= 0 for n in self.round_sizes):
            raise ValueError(f"HalvingSchedule: round_sizes must be all positive, got {self.round_sizes!r}")
        if any(n <= 0 for n in self.units_per_arm):
            raise ValueError(f"HalvingSchedule: units_per_arm must be all positive, got {self.units_per_arm!r}")
        if self.pool_size <= 0:
            raise ValueError(f"HalvingSchedule: pool_size must be positive, got {self.pool_size!r}")
        if any(b > a for a, b in zip(self.round_sizes, self.round_sizes[1:], strict=False)):
            logger.warning(
                "[HalvingSchedule] round_sizes=%r is not monotonically "
                "non-increasing - this configures halving rounds that GROW "
                "the candidate pool, which is almost certainly a config typo.",
                self.round_sizes,
            )

    def n_calls_for_stage(
        self, round_idx: int, *, pool_size: int | None = None,
    ) -> int:
        """Per-stage call count for the given round (1-indexed).

        Optional ``pool_size`` clamps units-per-arm against the actual
        pool. With default pool=12 and round-4 units-per-arm=12, returns
        7*12=84. With pool=8 (operator passed a smaller pool), returns
        7*min(12,8)=56.
        """
        i = round_idx - 1
        if i < 0 or i >= len(self.round_sizes):
            raise ValueError(f"round_idx {round_idx} out of range " f"(must be 1..{len(self.round_sizes)})")
        n_units = self.units_per_arm[i]
        effective_pool = pool_size if pool_size is not None else self.pool_size
        return self.round_sizes[i] * min(n_units, effective_pool)

    def total_calls_for_stage(self, *, pool_size: int | None = None) -> int:
        """Sum n_calls_for_stage across all rounds — what one stage
        will cost across the full cascade."""
        return sum(self.n_calls_for_stage(r, pool_size=pool_size) for r in range(1, len(self.round_sizes) + 1))

    def next_round_size(self, just_finished_round_idx: int) -> int:
        """Explicit promote() semantic.

        After round k completes, the next round target is round k+1's
        size — i.e. ``round_sizes[k]`` (0-indexed). This function makes
        the off-by-one EXPLICIT and asserts bounds.

        For the LAST round, returns 1 (a single final winner).
        """
        if just_finished_round_idx < 1 or just_finished_round_idx > len(self.round_sizes):
            raise ValueError(f"just_finished_round_idx {just_finished_round_idx} " f"out of [1, {len(self.round_sizes)}]")
        if just_finished_round_idx == len(self.round_sizes):
            # Last round — no further rounds; promote returns final winner pool
            return 1
        return self.round_sizes[just_finished_round_idx]
