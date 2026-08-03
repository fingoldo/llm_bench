"""Baseline measurement for compute_ranking's documented, DELIBERATELY
DEFERRED perf trade-off (audit: 06-High #1 -- see the "Known perf
trade-off" paragraph in ``compute_ranking``'s own docstring in
``ranking/ranker.py``).

Unlike ``bench_mad_bootstrap_prune.py`` / ``bench_budget_gate.py``,
there is no "after" here: a correct fix needs a real schema/API change
(an explicit ``round_idx`` on ``RunRow``, or a content-hash-keyed
cache -- a naive PK-keyed cache is unsafe because the resume/retry fix
elsewhere in this framework lets a later successful attempt overwrite
an earlier failed row at the same PK, which would risk serving a stale
pre-overwrite score). This script exists to put a concrete number on
the documented trade-off rather than leave it purely qualitative, so a
future fix has a measured baseline to justify itself against.

``round_runner.py`` calls ``compute_ranking`` once per round, over ALL
rows recorded so far for the tag (every earlier round's rows get
re-streamed and re-scored every time) -- this script simulates that
cumulative pattern across an N-round run and reports the total
wall-clock spent purely on ranking recomputation.

Run:
    python benchmarks/bench_compute_ranking.py
"""

from __future__ import annotations

import asyncio
import time

from llm_bench import ExperimentTag, InMemoryStorage, RunRow, compute_ranking

TAG = ExperimentTag("bench_ranking")


def _row(round_idx: int, model: str, unit: int) -> RunRow:
    return RunRow(
        composite_hash=f"h_{round_idx}_{model}_{unit}",
        provider="openrouter",
        model=model,
        thinking="",
        stage="enrich",
        task_unit_id=f"t{unit}",
        stratum=None,
        lang=None,
        parent_prompt_hash=None,
        experiment_tag=TAG,
        response='{"ok": true, "padding": "enough length here to clear scorer"}',
        error_class=None,
        error_message=None,
        cost_usd=0.001,
        effective_cost_usd=0.001,
        input_tokens=10,
        output_tokens=5,
        reasoning_tokens=0,
        cache_hit_tokens=0,
        cache_write_tokens=0,
    )


async def main() -> None:
    n_rounds = 6
    candidates_per_round = 20
    units_per_round = 8

    storage = InMemoryStorage()
    await storage.initialize()

    print(f"compute_ranking cumulative cost across {n_rounds} rounds " f"({candidates_per_round} candidates x {units_per_round} units/round)\n")
    print(f"{'round':>6} {'rows_so_far':>12} {'this_call (s)':>14} {'cumulative (s)':>16}")

    cumulative = 0.0
    for round_idx in range(1, n_rounds + 1):
        for c in range(candidates_per_round):
            for u in range(units_per_round):
                await storage.record_call(_row(round_idx, f"m{c}", u + round_idx * units_per_round))

        start = time.perf_counter()
        report = await compute_ranking(storage, TAG)
        elapsed = time.perf_counter() - start
        cumulative += elapsed
        print(f"{round_idx:>6} {report.n_rows:>12} {elapsed:>14.4f} {cumulative:>16.4f}")

    print(
        "\nEvery round re-streams + re-scores every prior round's rows "
        "(no round_idx filter exists on RunRow to avoid it) -- this is "
        "the O(rounds^2)-shaped cost the docstring documents as a known,",
    )
    print("deliberately-deferred trade-off, not an oversight.")


if __name__ == "__main__":
    asyncio.run(main())
