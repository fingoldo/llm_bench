"""PostgresStorage contract + Postgres-specific regression tests.

Gated on ``LLM_BENCH_TEST_DB_URL`` (see ``tests/conftest.py``'s
``postgres_test_url`` fixture) — skipped entirely when no test database
is configured, matching the ``pytest.mark.postgres`` convention already
declared in ``pyproject.toml``. No live Postgres/Docker instance was
available in the environment these tests were authored in, so they
have NOT been executed against a real server — only checked for
import-time/syntax correctness and structural consistency with the
InMemoryStorage/FileStorage contract tests in
``tests/unit/test_storage_protocol.py`` (whose ``_row`` helper this
file reuses so the two suites stay in lockstep).

Run against a real database with e.g.:
    docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16
    LLM_BENCH_TEST_DB_URL=postgresql://postgres:x@localhost/postgres \\
        pytest tests/integration/test_storage_protocol_postgres.py -v

Each test gets its own schema (``test_<uuid hex>``), dropped in
teardown via ``DROP SCHEMA ... CASCADE`` — tests never share state and
never touch a schema a real deployment would use.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("asyncpg")

from llm_bench.core.types import ExperimentTag, WinnerSet
from tests.unit.test_storage_protocol import _row

pytestmark = pytest.mark.postgres


@pytest.fixture
async def pg_storage(postgres_test_url):
    if postgres_test_url is None:
        pytest.skip("LLM_BENCH_TEST_DB_URL not set - no Postgres test DB configured")
    from llm_bench.storage.postgres import PostgresStorage

    schema = f"test_{uuid.uuid4().hex[:16]}"
    storage = PostgresStorage(postgres_test_url, schema_name=schema)
    await storage.initialize()
    try:
        yield storage
    finally:
        pool = storage._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await storage.close()


# ──────────────────────────────────────────────────────────────────────
# Contract parity with InMemoryStorage/FileStorage
# ──────────────────────────────────────────────────────────────────────

class TestPostgresContractParity:
    async def test_record_call_idempotent_same_key(self, pg_storage):
        r = _row(composite_hash="abc")
        h1 = await pg_storage.record_call(r)
        h2 = await pg_storage.record_call(r)
        assert h1 == h2 == "abc"
        total = await pg_storage.query_spend_total(experiment_tag=ExperimentTag("test_tag"))
        assert total == pytest.approx(0.001)

    async def test_failed_then_successful_retry_overwrites(self, pg_storage):
        # Direct regression test for the ON CONFLICT DO UPDATE fix
        # (audit: Critical) — the original ON CONFLICT DO NOTHING made
        # a resumed run's successful retry silently vanish against a
        # prior failed row for the same PK.
        failed = _row(composite_hash="retry_h", response=None, error_class="RateLimited", cost=0.0)
        await pg_storage.record_call(failed)
        ok = _row(composite_hash="retry_h", response='{"ok": true, "padding": "enough length here"}', error_class=None, cost=0.02)
        await pg_storage.record_call(ok)

        rows = [r async for r in pg_storage.query_rows(experiment_tag=ExperimentTag("test_tag")) if r.composite_hash == "retry_h"]
        assert len(rows) == 1
        assert rows[0].error_class is None

    async def test_successful_row_not_overwritten_by_later_failure(self, pg_storage):
        ok = _row(composite_hash="stable_h", response='{"ok": true, "padding": "enough length here"}', error_class=None, cost=0.02)
        await pg_storage.record_call(ok)
        later_failure = _row(composite_hash="stable_h", response=None, error_class="RateLimited", cost=0.0)
        await pg_storage.record_call(later_failure)

        rows = [r async for r in pg_storage.query_rows(experiment_tag=ExperimentTag("test_tag")) if r.composite_hash == "stable_h"]
        assert len(rows) == 1
        assert rows[0].error_class is None

    async def test_resume_cache_excludes_failed_rows(self, pg_storage):
        ok = _row(composite_hash="ok_h", error_class=None)
        bad = _row(composite_hash="bad_h", error_class="ContextOverflow")
        await pg_storage.record_call(ok)
        await pg_storage.record_call(bad)

        cache = await pg_storage.prefetch_resume_cache()
        assert ("ok_h", "openrouter", "test/model", "") in cache
        assert ("bad_h", "openrouter", "test/model", "") not in cache

    async def test_upsert_prompts_idempotent(self, pg_storage):
        sys_h1, usr_h1, comp_h1 = await pg_storage.upsert_prompts(system_text="SYS", user_text="USER")
        sys_h2, usr_h2, comp_h2 = await pg_storage.upsert_prompts(system_text="SYS", user_text="USER")
        assert (sys_h1, usr_h1, comp_h1) == (sys_h2, usr_h2, comp_h2)

    async def test_persist_winners_idempotent(self, pg_storage):
        ws = WinnerSet(
            experiment_tag=ExperimentTag("test_tag"), round_idx=1,
            winners={"enrich": "model_a"}, candidates_out=["model_a"],
        )
        await pg_storage.persist_winners(experiment_tag=ExperimentTag("test_tag"), round_idx=1, winners=ws)
        loaded = await pg_storage.load_winners(experiment_tag=ExperimentTag("test_tag"), round_idx=1)
        assert loaded is not None
        assert loaded.winners == {"enrich": "model_a"}

    async def test_delete_experiment_removes_rows_and_winners(self, pg_storage):
        await pg_storage.record_call(_row(composite_hash="a", tag="doomed"))
        await pg_storage.record_call(_row(composite_hash="b", tag="doomed"))
        await pg_storage.record_call(_row(composite_hash="c", tag="keep"))
        await pg_storage.persist_winners(
            experiment_tag=ExperimentTag("doomed"), round_idx=1,
            winners=WinnerSet(experiment_tag=ExperimentTag("doomed"), round_idx=1, winners={"x": "y"}),
        )
        n = await pg_storage.delete_experiment(experiment_tag=ExperimentTag("doomed"))
        assert n == 2
        assert await pg_storage.load_winners(experiment_tag=ExperimentTag("doomed"), round_idx=1) is None
        total_keep = await pg_storage.query_spend_total(experiment_tag=ExperimentTag("keep"))
        assert total_keep > 0


# ──────────────────────────────────────────────────────────────────────
# Postgres-specific: schema validation, constraints, transactions
# ──────────────────────────────────────────────────────────────────────

class TestPostgresSpecific:
    # schema_name validation moved to tests/unit/test_postgres_storage_offline.py
    # -- it's a pure constructor check needing no DB connection, so gating it
    # behind postgres_test_url meant it never ran in the standard offline
    # suite (flagged independently across multiple Phase G verification
    # passes as an avoidable coverage gap).

    async def test_negative_cost_rejected_by_check_constraint(self, pg_storage):
        # CHECK constraints on cost/token columns (audit: 08-High) must
        # reject a negative cost at the DB layer even if application
        # code has a bug that lets one through.
        import asyncpg

        r = _row(composite_hash="neg_cost", cost=-1.0)
        with pytest.raises(asyncpg.CheckViolationError):
            await pg_storage.record_call(r)

    async def test_task_unit_id_null_rejected_by_not_null_constraint(self, pg_storage):
        # task_unit_id TEXT NOT NULL (audit: 08-Medium) closes the DB-
        # level gap: the domain type RunRow.task_unit_id: str is
        # required at the dataclass level, but nothing previously
        # stopped a NULL from reaching this column via any write path
        # that bypasses RunRow's own constructor (raw SQL, a future
        # client of the same schema, a refactor that loosens the
        # domain type). Issues a raw INSERT (not through record_call,
        # which can't even construct a RunRow with task_unit_id=None
        # thanks to the domain type) to test the DB constraint itself.
        import asyncpg

        pool = pg_storage._require_pool()
        with pytest.raises(asyncpg.NotNullViolationError):
            async with pool.acquire() as conn:
                await conn.execute(
                    f"INSERT INTO {pg_storage.schema}.benchmark_results "
                    f"(composite_hash, provider, model, stage, task_unit_id, experiment_tag) "
                    f"VALUES ($1, $2, $3, $4, NULL, $5)",
                    "null_task_unit_test", "openrouter", "m", "enrich", "test_tag",
                )

    async def test_prefetch_resume_cache_runs_inside_transaction_no_crash(self, pg_storage):
        # Direct regression test for the Critical bug where
        # prefetch_resume_cache used a server-side cursor OUTSIDE an
        # explicit transaction, crashing on real Postgres with
        # NoActiveSQLTransactionError (asyncpg requires an explicit
        # transaction for cursor iteration; the in-memory/SQLite-backed
        # test doubles used during development never exercised this
        # path because neither has that requirement).
        for i in range(5):
            await pg_storage.record_call(_row(composite_hash=f"h{i}"))
        cache = await pg_storage.prefetch_resume_cache()
        assert len(cache) == 5

    async def test_schema_version_mismatch_raises_clear_error(self, pg_storage, postgres_test_url):
        # Simulate a stale deployed schema by bumping schema_version
        # past what this code expects, then re-initializing a fresh
        # PostgresStorage instance pointed at the same schema.
        from llm_bench.storage.postgres import PostgresStorage

        pool = pg_storage._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"UPDATE {pg_storage.schema}.schema_version SET version = version + 1")

        stale_reader = PostgresStorage(postgres_test_url, schema_name=pg_storage.schema)
        with pytest.raises(RuntimeError, match="version"):
            await stale_reader.initialize()
        await stale_reader.close()
