"""Before/after benchmark for BudgetGate's storage re-aggregation fix
(audit: 06-High #3 -- "previously re-aggregated on every call, measured
0.17ms -> 119ms as the tag grew").

"Before": every ``check()`` call re-queries
``storage.query_spend_by_op`` (an O(total rows for the tag) scan) to
learn the current spend. "After": the real ``BudgetGate.check()``,
which queries storage once per ``(tag, op)`` and tracks spend/
reservations in memory from then on (see ``runner/budget.py``'s
``_seeded_keys`` dict).

Run:
    python benchmarks/bench_budget_gate.py
"""

from __future__ import annotations

import asyncio
import time

from llm_bench import ExperimentTag, InMemoryStorage, RunRow
from llm_bench.runner.budget import BudgetGate, StageBudget

TAG = ExperimentTag("bench_budget")
N_CHECKS = 200


def _row(i: int) -> RunRow:
    return RunRow(
        composite_hash=f"h{i}",
        provider="openrouter",
        model="m",
        thinking="",
        stage="enrich",
        task_unit_id=f"t{i}",
        stratum=None,
        lang=None,
        parent_prompt_hash=None,
        experiment_tag=TAG,
        response='{"ok": true, "padding": "enough length here"}',
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


async def _seed_storage(n_rows: int) -> InMemoryStorage:
    storage = InMemoryStorage()
    await storage.initialize()
    for i in range(n_rows):
        await storage.record_call(_row(i))
    return storage


async def _bench_old_reaggregate_every_call(storage: InMemoryStorage) -> float:
    """Simulates the pre-fix behavior: re-query full spend on every check()."""
    start = time.perf_counter()
    for _ in range(N_CHECKS):
        by_op = await storage.query_spend_by_op(experiment_tag=TAG)
        _spent = by_op.get("enrich", 0.0)
        _cap = 10.0
        _ok = _spent + 0.01 <= _cap
    return time.perf_counter() - start


async def _bench_new_seed_once(storage: InMemoryStorage) -> float:
    gate = BudgetGate(budgets_by_op={"enrich": StageBudget(cap_usd=10.0)})
    start = time.perf_counter()
    for _ in range(N_CHECKS):
        ok, _spent, _cap, reservation = await gate.check(storage, TAG, "enrich")
        if ok:
            await gate.record_cost(TAG, "enrich", 0.001, reservation=reservation)
    return time.perf_counter() - start


async def main() -> None:
    print(f"BudgetGate.check() before/after ({N_CHECKS} checks per row count)\n")
    print(f"{'rows in tag':>12} {'before (s)':>12} {'after (s)':>12} {'speedup':>10}")
    for n_rows in (50, 500, 2000, 5000):
        storage = await _seed_storage(n_rows)
        before = await _bench_old_reaggregate_every_call(storage)
        after = await _bench_new_seed_once(storage)
        print(f"{n_rows:>12} {before:>12.4f} {after:>12.4f} {before / after:>9.1f}x")


if __name__ == "__main__":
    asyncio.run(main())
