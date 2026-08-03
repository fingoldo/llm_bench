"""PostgresStorage unit tests that need NO live database connection —
pure constructor validation, pool-creation wiring, and the
initialize()/close() TOCTOU-race fix. Complements
``tests/integration/test_storage_protocol_postgres.py`` (which DOES
need a live server, gated on ``LLM_BENCH_TEST_DB_URL``).

Several regression tests that logically belong here were previously
only present in the DB-gated integration file, where they never ran in
the standard offline suite despite needing no DB connectivity at all
(audit follow-up: flagged independently by 5 of the Phase G
verification passes as an avoidable coverage gap) — moved/added here
so they execute in every CI run, not just when a Postgres service is
configured.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_bench.core.types import ExperimentTag
from llm_bench.storage.postgres import PostgresStorage

# redact_dsn itself is tested directly in tests/unit/test_redaction.py
# (the canonical module it now lives in); this file only checks that
# initialize()'s create_pool-failure path actually USES it end-to-end
# (see TestInitializeWiring::test_create_pool_failure_wrapped_with_redacted_dsn below).
FAKE_DSN = "postgresql://user:secretpass@localhost:5432/db"


class TestSchemaNameValidation:
    """Moved out of the Postgres-gated integration file: this check
    fires in __init__, before any I/O, so it needs no live DB at all."""

    def test_invalid_schema_name_rejected_at_construction(self):
        with pytest.raises(ValueError, match="schema_name"):
            PostgresStorage(FAKE_DSN, schema_name="bad; DROP TABLE x;--")

    def test_valid_schema_name_accepted(self):
        PostgresStorage(FAKE_DSN, schema_name="my_schema_1")  # must not raise

    def test_schema_name_too_long_rejected(self):
        with pytest.raises(ValueError, match="schema_name"):
            PostgresStorage(FAKE_DSN, schema_name="x" * 64)


class TestRequirePool:
    async def test_query_rows_raises_before_initialize(self):
        storage = PostgresStorage(FAKE_DSN)
        with pytest.raises(RuntimeError, match="call initialize"):
            async for _ in storage.query_rows(experiment_tag=ExperimentTag("t")):
                pass

    async def test_record_call_raises_before_initialize(self):
        from llm_bench.core.types import RunRow

        storage = PostgresStorage(FAKE_DSN)
        row = RunRow(
            composite_hash="h", provider="openrouter", model="m", thinking="",
            stage="enrich", task_unit_id="t", stratum=None, lang=None,
            parent_prompt_hash=None, experiment_tag="tag",
            response='{"ok": true, "padding": "enough length here"}',
            error_class=None, error_message=None, cost_usd=0.001,
            effective_cost_usd=0.001, input_tokens=1, output_tokens=1,
            reasoning_tokens=0, cache_hit_tokens=0, cache_write_tokens=0,
        )
        with pytest.raises(RuntimeError, match="call initialize"):
            await storage.record_call(row)


# ──────────────────────────────────────────────────────────────────────
# initialize() wiring — mocked asyncpg, no real connection
# ──────────────────────────────────────────────────────────────────────

def _fake_pool(execute_side_effect=None):
    """A MagicMock standing in for an asyncpg.Pool: acquire() is an
    async context manager yielding a connection whose execute/fetchrow
    are AsyncMocks."""
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=execute_side_effect)
    conn.fetchrow = AsyncMock(return_value={"version": 1})

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.close = AsyncMock()
    return pool, conn


class TestInitializeWiring:
    async def test_command_timeout_passed_to_create_pool(self, monkeypatch):
        import asyncpg

        pool, _conn = _fake_pool()
        create_pool = AsyncMock(return_value=pool)
        monkeypatch.setattr(asyncpg, "create_pool", create_pool)

        storage = PostgresStorage(FAKE_DSN, command_timeout=12.5, min_connections=2, max_connections=4)
        await storage.initialize()

        create_pool.assert_awaited_once()
        _args, kwargs = create_pool.call_args
        assert kwargs["command_timeout"] == 12.5
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 4
        await storage.close()

    async def test_concurrent_initialize_creates_pool_exactly_once(self, monkeypatch):
        # Direct regression test for the TOCTOU-race fix: N concurrent
        # initialize() callers must not each independently call
        # asyncpg.create_pool (leaking every loser's pool).
        import asyncpg

        pool, _conn = _fake_pool()
        create_pool = AsyncMock(return_value=pool)
        monkeypatch.setattr(asyncpg, "create_pool", create_pool)

        storage = PostgresStorage(FAKE_DSN)
        await asyncio.gather(*(storage.initialize() for _ in range(10)))

        create_pool.assert_awaited_once()
        await storage.close()

    async def test_create_pool_failure_wrapped_with_redacted_dsn(self, monkeypatch):
        import asyncpg

        create_pool = AsyncMock(side_effect=ConnectionRefusedError("connection refused"))
        monkeypatch.setattr(asyncpg, "create_pool", create_pool)

        storage = PostgresStorage(FAKE_DSN)
        with pytest.raises(RuntimeError) as exc_info:
            await storage.initialize()

        message = str(exc_info.value)
        assert "secretpass" not in message
        assert "***:***" in message
        assert exc_info.value.__cause__ is not None

    async def test_ddl_failure_closes_pool_and_leaves_uninitialized(self, monkeypatch):
        import asyncpg

        pool, _conn = _fake_pool(execute_side_effect=RuntimeError("ddl boom"))
        create_pool = AsyncMock(return_value=pool)
        monkeypatch.setattr(asyncpg, "create_pool", create_pool)

        storage = PostgresStorage(FAKE_DSN)
        with pytest.raises(RuntimeError, match="ddl boom"):
            await storage.initialize()

        pool.close.assert_awaited_once()
        with pytest.raises(RuntimeError, match="call initialize"):
            storage._require_pool()

    async def test_close_before_initialize_is_a_noop(self):
        storage = PostgresStorage(FAKE_DSN)
        await storage.close()  # must not raise
