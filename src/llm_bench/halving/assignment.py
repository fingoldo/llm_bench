"""Per-round task-unit assignment.

Determines which task units each candidate sees in a given round.
Sequential Halving's statistical claim ("same expected accuracy as
``rounds[-1] * pool_size`` naive") requires apples-to-apples
comparison: every candidate in a round MUST see the same task-unit
slice. The "same N for everyone" assignment below is what makes the
mean-of-arm comparison fair.

History note (VocabApp wave-23): an earlier impl randomised per-arm
units in rounds 2/3 — each candidate got a DIFFERENT subset. That
broke the paired-comparison guarantee (arm A's score came from harder
units than arm B's). Now every round uses ``pool[:n_per_arm]``, which
means the ``pool`` ordering controls difficulty stratification
(consumers should sort hardest-first if they want round 1 to be the
hardest cut).
"""

from __future__ import annotations

import logging

from llm_bench.halving.schedule import HalvingSchedule

logger = logging.getLogger(__name__)


def assign_task_units_for_round(
    candidates: list[str],
    pool: list[str],
    round_idx: int,
    schedule: HalvingSchedule | None = None,
    *,
    rng_seed: int = 0,
) -> dict[str, list[str]]:
    """Returns ``{candidate: [task_unit_id, ...]}`` for one round.

    Round k uses ``pool[:units_per_arm[k-1]]`` for every candidate. The
    pool's first ``n_per_arm`` items are the round's slice; consumers
    own the ordering of ``pool`` (sort by difficulty / stratum
    interleave / whatever).

    When ``n_per_arm`` exceeds the pool size, clamps to the pool with a
    WARNING — Sequential Halving's statistical claim assumes the full
    pool size, so under-sized pools are operator-visible.

    The ``rng_seed`` arg is accepted for backward compat with callers
    that thread it through other randomised picks (validator-pair
    seeding); the assignment itself is deterministic.
    """
    schedule = schedule or HalvingSchedule()
    if round_idx < 1 or round_idx > len(schedule.units_per_arm):
        raise ValueError(f"round_idx {round_idx} out of range " f"(must be 1..{len(schedule.units_per_arm)})")
    n_per_arm = schedule.units_per_arm[round_idx - 1]
    if n_per_arm > len(pool):
        logger.warning(
            "[assign_task_units_for_round] round %d wants n_per_arm=%d "
            "but pool has only %d task units - clamping. Sequential "
            "Halving's statistical-power claim requires the full pool "
            "size; results from this round are weaker than designed.",
            round_idx, n_per_arm, len(pool),
        )
    n_per_arm = min(n_per_arm, len(pool))
    _ = rng_seed
    chosen = pool[:n_per_arm]
    return {c: list(chosen) for c in candidates}
