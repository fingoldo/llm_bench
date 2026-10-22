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
import sys
import types
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


def _install_fake_asyncpg(monkeypatch, create_pool):
    """Stand in for the whole asyncpg module, not just one attribute of it.

    initialize() does a function-local ``import asyncpg``, which resolves through
    ``sys.modules`` first, so these tests need neither a database nor the optional
    ``[postgres]`` extra installed: they exercise the same initialize()/close()
    branches in every environment, including CI jobs that install only ``[dev]``."""
    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=create_pool))


class TestInitializeWiring:
    async def test_command_timeout_passed_to_create_pool(self, monkeypatch):
        pool, _conn = _fake_pool()
        create_pool = AsyncMock(return_value=pool)
        _install_fake_asyncpg(monkeypatch, create_pool)

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
        pool, _conn = _fake_pool()
        create_pool = AsyncMock(return_value=pool)
        _install_fake_asyncpg(monkeypatch, create_pool)

        storage = PostgresStorage(FAKE_DSN)
        await asyncio.gather(*(storage.initialize() for _ in range(10)))

        create_pool.assert_awaited_once()
        await storage.close()

    async def test_create_pool_failure_wrapped_with_redacted_dsn(self, monkeypatch):
        create_pool = AsyncMock(side_effect=ConnectionRefusedError("connection refused"))
        _install_fake_asyncpg(monkeypatch, create_pool)

        storage = PostgresStorage(FAKE_DSN)
        with pytest.raises(RuntimeError) as exc_info:
            await storage.initialize()

        message = str(exc_info.value)
        assert "secretpass" not in message
        assert "***:***" in message
        assert exc_info.value.__cause__ is not None

    async def test_ddl_failure_closes_pool_and_leaves_uninitialized(self, monkeypatch):
        pool, _conn = _fake_pool(execute_side_effect=RuntimeError("ddl boom"))
        create_pool = AsyncMock(return_value=pool)
        _install_fake_asyncpg(monkeypatch, create_pool)

        storage = PostgresStorage(FAKE_DSN)
        with pytest.raises(RuntimeError, match="ddl boom"):
            await storage.initialize()

        pool.close.assert_awaited_once()
        with pytest.raises(RuntimeError, match="call initialize"):
            storage._require_pool()

    async def test_close_before_initialize_is_a_noop(self):
        storage = PostgresStorage(FAKE_DSN)
        await storage.close()  # must not raise


# ──────────────────────────────────────────────────────────────────────
# Query/branch coverage with a scripted fake connection (no database)
# ──────────────────────────────────────────────────────────────────────

_PG_RECORD_COLS = (
    "composite_hash provider model thinking stage task_unit_id stratum lang parent_prompt_hash experiment_tag response "
    "error_class error_message cost_usd effective_cost_usd input_tokens output_tokens reasoning_tokens cache_hit_tokens "
    "cache_write_tokens cache_discount_usd is_byok web_search_citations upstream_resolved_model upstream_provider "
    "upstream_model native_finish_reason generation_id mean_attempt_duration_sec n_attempts http_status_sequence "
    "per_attempt_durations_sec logs logs_compressed parse_failure_prefix generation_details ts"
).split()


def _record(**overrides):
    rec = dict.fromkeys(_PG_RECORD_COLS)
    rec.update(composite_hash="h", provider="openrouter", model="m", thinking="", stage="enrich", experiment_tag="tag")
    rec.update(overrides)
    return rec


def _pool_with_conn(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _scripted_conn(records=(), fetchrow=None):
    """conn.cursor(sql, *params) yields ``records``; conn.transaction() is an async context manager."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.execute = AsyncMock()

    def _cursor(sql, *params):
        async def _gen():
            for r in records:
                yield r

        return _gen()

    conn.cursor = MagicMock(side_effect=_cursor)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


def _storage_with(conn):
    storage = PostgresStorage(FAKE_DSN)
    storage._pool = _pool_with_conn(conn)
    return storage


class TestSchemaVersionCheck:
    async def test_empty_schema_version_table_is_stamped_with_current_version(self):
        from llm_bench.storage.postgres import _SCHEMA_VERSION

        conn = _scripted_conn(fetchrow=None)
        await PostgresStorage(FAKE_DSN)._check_or_stamp_schema_version(conn)

        conn.execute.assert_awaited_once()
        sql, version = conn.execute.await_args.args
        assert "INSERT INTO" in sql and "schema_version" in sql
        assert version == _SCHEMA_VERSION

    async def test_version_mismatch_raises_with_both_versions_in_message(self):
        from llm_bench.storage.postgres import _SCHEMA_VERSION

        conn = _scripted_conn(fetchrow={"version": _SCHEMA_VERSION + 41})
        with pytest.raises(RuntimeError, match=rf"version {_SCHEMA_VERSION + 41}.*expects version {_SCHEMA_VERSION}"):
            await PostgresStorage(FAKE_DSN)._check_or_stamp_schema_version(conn)
        conn.execute.assert_not_awaited()


class TestPrefetchResumeCacheSql:
    async def test_only_successful_adds_error_and_parse_failure_filters(self):
        rec = _record(response="x" * 40, input_tokens=None, output_tokens=7, reasoning_tokens=None, cost_usd=None)
        conn = _scripted_conn(records=[rec])
        out = await _storage_with(conn).prefetch_resume_cache(only_successful=True, min_response_len=10)

        sql, *params = conn.cursor.call_args.args
        assert "error_class IS NULL" in sql and "parse_failure_prefix IS NULL" in sql
        assert params == [10]
        cached = out[("h", "openrouter", "m", "")]
        assert (cached.input_tokens, cached.output_tokens, cached.reasoning_tokens, cached.cost_usd) == (0, 7, 0, 0.0)
        assert cached.source_tag == "tag"

    async def test_not_only_successful_omits_the_error_filters(self):
        conn = _scripted_conn()
        await _storage_with(conn).prefetch_resume_cache(only_successful=False)
        sql = conn.cursor.call_args.args[0]
        assert "error_class" not in sql and "parse_failure_prefix" not in sql


class TestGetCached:
    async def _get(self, fetchrow):
        return await _storage_with(_scripted_conn(fetchrow=fetchrow)).get_cached(
            composite_hash="h", provider="openrouter", model="m", thinking="",
        )

    async def test_missing_row_returns_none(self):
        assert await self._get(None) is None

    async def test_row_with_error_class_is_not_usable(self):
        row = {"response": "y" * 60, "error_class": "RateLimit", "parse_failure_prefix": None}
        assert await self._get(row) is None

    async def test_usable_row_is_returned_as_cached_response(self):
        row = {
            "response": "y" * 60, "error_class": None, "parse_failure_prefix": None, "input_tokens": 3,
            "output_tokens": None, "reasoning_tokens": 2, "cost_usd": 0.5, "experiment_tag": "tag",
        }
        cached = await self._get(row)
        assert cached is not None
        assert (cached.response, cached.input_tokens, cached.output_tokens, cached.reasoning_tokens) == ("y" * 60, 3, 0, 2)
        assert cached.cost_usd == 0.5


class TestQueryRowsStageFilter:
    async def test_stage_adds_a_second_bound_parameter(self):
        conn = _scripted_conn(records=[_record(stage="validate")])
        rows = [r async for r in _storage_with(conn).query_rows(experiment_tag=ExperimentTag("tag"), stage="validate")]

        sql, *params = conn.cursor.call_args.args
        assert "AND stage=$2" in sql
        assert params == ["tag", "validate"]
        assert [r.stage for r in rows] == ["validate"]

    async def test_no_stage_leaves_the_query_unfiltered(self):
        conn = _scripted_conn()
        _ = [r async for r in _storage_with(conn).query_rows(experiment_tag=ExperimentTag("tag"))]
        sql, *params = conn.cursor.call_args.args
        assert "stage=$2" not in sql
        assert params == ["tag"]


class TestLoadWinnersPayloadShape:
    async def test_text_encoded_jsonb_payload_is_decoded(self):
        import json

        payload = json.dumps({"experiment_tag": "tag", "round_idx": 2, "winners": {"enrich": "m1"}})
        ws = await _storage_with(_scripted_conn(fetchrow={"payload": payload})).load_winners(experiment_tag=ExperimentTag("tag"), round_idx=2)
        assert ws is not None
        assert (ws.round_idx, ws.winners) == (2, {"enrich": "m1"})

    async def test_already_decoded_payload_is_used_as_is(self):
        payload = {"experiment_tag": "tag", "round_idx": 3, "winners": {"enrich": "m2"}}
        ws = await _storage_with(_scripted_conn(fetchrow={"payload": payload})).load_winners(experiment_tag=ExperimentTag("tag"), round_idx=3)
        assert ws is not None and ws.winners == {"enrich": "m2"}


class TestRecordHelpers:
    def test_to_json_none_stays_none_and_values_are_dumped(self):
        from llm_bench.storage.postgres import _to_json

        assert _to_json(None) is None
        assert _to_json({"a": [1]}) == '{"a": [1]}'

    def test_record_to_row_nulls_stay_none(self):
        from llm_bench.storage.postgres import _record_to_row

        row = _record_to_row(_record())
        assert row.cost_usd is None and row.effective_cost_usd is None
        assert row.cache_discount_usd is None and row.mean_attempt_duration_sec is None
        assert row.generation_details == {}

    def test_record_to_row_converts_numerics_and_decodes_json_text(self):
        from decimal import Decimal

        from llm_bench.storage.postgres import _record_to_row

        rec = _record(
            cost_usd=Decimal("0.25"), effective_cost_usd=Decimal("0.2"), cache_discount_usd=Decimal("0.05"),
            mean_attempt_duration_sec=Decimal("1.5"), http_status_sequence="[429, 200]", web_search_citations=[{"u": 1}],
            per_attempt_durations_sec="not json {", generation_details='{"k": 1}',
        )
        row = _record_to_row(rec)
        assert (row.cost_usd, row.effective_cost_usd, row.cache_discount_usd, row.mean_attempt_duration_sec) == (0.25, 0.2, 0.05, 1.5)
        assert row.http_status_sequence == [429, 200]
        assert row.web_search_citations == [{"u": 1}]
        assert row.per_attempt_durations_sec == "not json {"
        assert row.generation_details == {"k": 1}
