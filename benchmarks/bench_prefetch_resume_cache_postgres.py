"""Postgres partial-index effectiveness check for prefetch_resume_cache
(audit: 08-High / 06-High #4).

``postgres.py``'s DDL adds a partial index
(``idx_benchmark_results_resume_cache``) matching
``prefetch_resume_cache``'s WHERE clause specifically because that
query has no ``experiment_tag`` filter (the resume cache is
deliberately tag-agnostic) and would otherwise be a full sequential
scan of a table that grows without bound across every run ever
executed. This script seeds a temporary schema with a mix of
successful/failed rows, runs ``EXPLAIN (ANALYZE, BUFFERS)`` against the
EXACT SQL ``prefetch_resume_cache`` issues, and reports whether the
planner actually chose the partial index.

Gated on ``LLM_BENCH_TEST_DB_URL``, same as
``tests/integration/test_storage_protocol_postgres.py`` — skips
cleanly when no live Postgres is configured.

HONESTY NOTE: no live Postgres/Docker instance was available in the
environment this script was authored in, so it has NOT been executed
against a real server; only checked for import-time/syntax
correctness. Run it against a real database to get an actual verdict:
    docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16
    LLM_BENCH_TEST_DB_URL=postgresql://postgres:x@localhost/postgres \\
        python benchmarks/bench_prefetch_resume_cache_postgres.py

Run:
    python benchmarks/bench_prefetch_resume_cache_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from llm_bench.core.types import ExperimentTag, RunRow


def _row(i: int, *, failed: bool) -> RunRow:
    return RunRow(
        composite_hash=f"h{i}",
        provider="openrouter",
        model=f"m{i % 20}",
        thinking="",
        stage="enrich",
        task_unit_id=f"t{i}",
        stratum=None,
        lang=None,
        parent_prompt_hash=None,
        experiment_tag=ExperimentTag(f"tag{i % 50}"),
        response=None if failed else '{"ok": true, "padding": "enough length here"}',
        error_class="RateLimited" if failed else None,
        error_message=None,
        cost_usd=0.001,
        effective_cost_usd=0.001,
        input_tokens=10,
        output_tokens=5,
        reasoning_tokens=0,
        cache_hit_tokens=0,
        cache_write_tokens=0,
    )


async def main() -> int:
    db_url = os.environ.get("LLM_BENCH_TEST_DB_URL")
    if not db_url:
        print("SKIPPED: LLM_BENCH_TEST_DB_URL not set - no live Postgres to benchmark against.")
        return 0

    from llm_bench.storage.postgres import PostgresStorage

    n_rows = 20_000
    schema = f"bench_{uuid.uuid4().hex[:16]}"
    storage = PostgresStorage(db_url, schema_name=schema)
    await storage.initialize()
    try:
        print(f"Seeding {n_rows} rows into schema {schema!r} (~10% simulated failures)...")
        for i in range(n_rows):
            await storage.record_call(_row(i, failed=(i % 10 == 0)))

        pool = storage._require_pool()
        sql = (
            f"SELECT composite_hash, provider, model, thinking, response, "
            f"input_tokens, output_tokens, reasoning_tokens, cost_usd, "
            f"experiment_tag FROM {schema}.benchmark_results "
            f"WHERE response IS NOT NULL AND length(response) >= $1 "
            f"AND (error_class IS NULL OR error_class = '') "
            f"AND (parse_failure_prefix IS NULL OR parse_failure_prefix = '')"
        )
        async with pool.acquire() as conn:
            plan_rows = await conn.fetch(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", 20)
            plan_text = "\n".join(r[0] for r in plan_rows)

        print("\n--- EXPLAIN (ANALYZE, BUFFERS) ---")
        print(plan_text)
        print("-----------------------------------\n")

        used_partial_index = "idx_benchmark_results_resume_cache" in plan_text
        used_seq_scan = "Seq Scan" in plan_text
        if used_partial_index:
            print("VERDICT: planner used the partial index idx_benchmark_results_resume_cache.")
        elif used_seq_scan:
            print("VERDICT: planner fell back to a Seq Scan -- the partial index was NOT used. " "Investigate (row count too small for the planner to prefer it? predicate mismatch?).")
        else:
            print("VERDICT: inconclusive -- neither the index name nor 'Seq Scan' appeared in the plan text.")
    finally:
        pool = storage._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
