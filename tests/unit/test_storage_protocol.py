"""Storage contract test — every BenchmarkStorage backend must pass this.

Parametrized over backends:
  - InMemoryStorage (always available)
  - FileStorage     (Phase D — added when impl lands; until then xfail)
  - PostgresStorage (Phase D — gated on LLM_BENCH_TEST_DB_URL env)

Each backend gets a fresh instance per test (function-scoped fixture)
so tests don't leak into each other.

Invariants under test (mirror docstring of storage.base):
  1. ``record_call`` is idempotent on (composite_hash, provider, model, thinking)
  2. Failed rows are excluded from prefetch_resume_cache / get_cached
  3. ``upsert_prompts`` is idempotent on text content
  4. Cache is tag-agnostic
  5. ``persist_winners`` is idempotent on (tag, round)
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from llm_bench.core.types import ExperimentTag, RunRow, WinnerSet
from llm_bench.storage.base import BenchmarkStorage
from llm_bench.storage.memory import InMemoryStorage

# ──────────────────────────────────────────────────────────────────────
# Backend parametrization
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture(
    params=[
        pytest.param("memory", id="InMemoryStorage"),
        pytest.param("file", id="FileStorage"),
        # PostgresStorage parametrization gated on LLM_BENCH_TEST_DB_URL
        # — see test_storage_protocol_postgres.py (Phase D follow-up).
    ],
)
async def storage(request, tmp_path) -> AsyncIterator[BenchmarkStorage]:
    """A freshly-initialised storage backend, scoped to one test."""
    s: BenchmarkStorage
    if request.param == "memory":
        s = InMemoryStorage()
    elif request.param == "file":
        from llm_bench.storage.file import FileStorage
        s = FileStorage(tmp_path / "filestore")
    else:
        raise AssertionError(f"unknown backend: {request.param}")
    await s.initialize()
    yield s
    await s.close()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _row(
    *, composite_hash: str = "h_default",
    provider: str = "openrouter",
    model: str = "test/model",
    thinking: str = "",
    stage: str = "enrich",
    tag: str = "test_tag",
    response: str | None = '{"ok": true, "field": "value with enough length"}',
    error_class: str | None = None,
    cost: float = 0.001,
) -> RunRow:
    return RunRow(
        composite_hash=composite_hash,
        provider=provider,
        model=model,
        thinking=thinking,
        stage=stage,
        task_unit_id="task_001",
        stratum="v",
        lang=None,
        parent_prompt_hash=None,
        experiment_tag=tag,
        response=response,
        error_class=error_class,
        error_message=None,
        cost_usd=cost,
        effective_cost_usd=cost,
        input_tokens=100,
        output_tokens=50,
        reasoning_tokens=0,
        cache_hit_tokens=0,
        cache_write_tokens=0,
    )


# ──────────────────────────────────────────────────────────────────────
# Contract tests
# ──────────────────────────────────────────────────────────────────────


async def test_satisfies_protocol(storage):
    """The fixture-supplied object structurally satisfies the Protocol."""
    assert isinstance(storage, BenchmarkStorage)


async def test_upsert_prompts_idempotent(storage):
    """Same (sys, user) text => same hashes, no duplicate state."""
    sys_h1, usr_h1, comp_h1 = await storage.upsert_prompts(
        system_text="SYS", user_text="USER",
    )
    sys_h2, usr_h2, comp_h2 = await storage.upsert_prompts(
        system_text="SYS", user_text="USER",
    )
    assert (sys_h1, usr_h1, comp_h1) == (sys_h2, usr_h2, comp_h2)
    # Different text => different composite.
    _, _, comp_h3 = await storage.upsert_prompts(
        system_text="SYS", user_text="DIFFERENT",
    )
    assert comp_h3 != comp_h1


async def test_record_call_idempotent_same_key(storage):
    """Re-recording same PK is a no-op (no duplicate row, no exception)."""
    r = _row(composite_hash="abc")
    h1 = await storage.record_call(r)
    h2 = await storage.record_call(r)
    assert h1 == h2 == "abc"
    # Spend hasn't doubled.
    total = await storage.query_spend_total(experiment_tag=ExperimentTag("test_tag"))
    assert total == pytest.approx(0.001)


async def test_record_call_distinct_thinking_separate_rows(storage):
    """Same prompt+model but different thinking => separate rows."""
    r1 = _row(composite_hash="h1", thinking="off", cost=0.001)
    r2 = _row(composite_hash="h1", thinking="high", cost=0.005)
    await storage.record_call(r1)
    await storage.record_call(r2)
    total = await storage.query_spend_total(experiment_tag=ExperimentTag("test_tag"))
    assert total == pytest.approx(0.006)


async def test_resume_cache_excludes_failed_rows(storage):
    """error_class set => row NOT in resume cache (gets re-tried)."""
    ok = _row(composite_hash="ok_h", error_class=None)
    bad = _row(composite_hash="bad_h", error_class="ContextOverflow")
    await storage.record_call(ok)
    await storage.record_call(bad)

    cache = await storage.prefetch_resume_cache()
    keys = list(cache.keys())
    assert ("ok_h", "openrouter", "test/model", "") in cache
    assert ("bad_h", "openrouter", "test/model", "") not in cache
    assert len(keys) == 1


async def test_resume_cache_excludes_short_responses(storage):
    """Response shorter than min_response_len => excluded."""
    short = _row(composite_hash="short_h", response="x")
    full = _row(composite_hash="full_h", response='{"valid": true, "padding": "abcdefghijk"}')
    await storage.record_call(short)
    await storage.record_call(full)

    cache = await storage.prefetch_resume_cache(min_response_len=20)
    assert ("short_h", "openrouter", "test/model", "") not in cache
    assert ("full_h", "openrouter", "test/model", "") in cache


async def test_resume_cache_is_tag_agnostic(storage):
    """A row recorded under tag A is reusable from a query for tag B."""
    r = _row(composite_hash="cross_h", tag="run_A")
    await storage.record_call(r)

    cache = await storage.prefetch_resume_cache()
    hit = cache[("cross_h", "openrouter", "test/model", "")]
    assert hit.source_tag == "run_A"
    # Single-row lookup with different (composite_hash, ...) returns None.
    miss = await storage.get_cached(
        composite_hash="other", provider="openrouter",
        model="test/model", thinking="",
    )
    assert miss is None


async def test_get_cached_single_row(storage):
    """Single-row lookup returns the same shape as prefetch."""
    r = _row(composite_hash="gh", cost=0.005)
    await storage.record_call(r)
    hit = await storage.get_cached(
        composite_hash="gh", provider="openrouter",
        model="test/model", thinking="",
    )
    assert hit is not None
    assert hit.composite_hash == "gh"
    assert hit.cost_usd == pytest.approx(0.005)


async def test_query_spend_by_stage(storage):
    """Spend aggregation buckets by stage label."""
    await storage.record_call(_row(composite_hash="a", stage="enrich", cost=0.10))
    await storage.record_call(_row(composite_hash="b", stage="enrich", cost=0.05))
    await storage.record_call(_row(composite_hash="c", stage="translate_es", cost=0.20))
    await storage.record_call(_row(composite_hash="d", stage="translate_de", cost=0.15))

    by_stage = await storage.query_spend_by_stage(
        experiment_tag=ExperimentTag("test_tag"),
    )
    assert by_stage == pytest.approx({
        "enrich": 0.15,
        "translate_es": 0.20,
        "translate_de": 0.15,
    })


async def test_query_spend_by_op_collapses_lang_suffix(storage):
    """translate_es / translate_de share an ``op`` bucket."""
    await storage.record_call(_row(composite_hash="a", stage="enrich", cost=0.10))
    await storage.record_call(_row(composite_hash="c", stage="translate_es", cost=0.20))
    await storage.record_call(_row(composite_hash="d", stage="translate_de", cost=0.15))
    await storage.record_call(_row(composite_hash="e", stage="validate_translate_es", cost=0.03))

    by_op = await storage.query_spend_by_op(
        experiment_tag=ExperimentTag("test_tag"),
    )
    assert by_op == pytest.approx({
        "enrich": 0.10,
        "translate": 0.35,
        "validate_translate": 0.03,
    })


async def test_query_rows_filters_by_tag(storage):
    await storage.record_call(_row(composite_hash="a", tag="want"))
    await storage.record_call(_row(composite_hash="b", tag="other"))

    rows = [r async for r in storage.query_rows(experiment_tag=ExperimentTag("want"))]
    assert len(rows) == 1
    assert rows[0].composite_hash == "a"


async def test_query_rows_filters_by_stage(storage):
    await storage.record_call(_row(composite_hash="a", stage="enrich"))
    await storage.record_call(_row(composite_hash="b", stage="translate_es"))

    rows = [r async for r in storage.query_rows(
        experiment_tag=ExperimentTag("test_tag"), stage="enrich",
    )]
    assert len(rows) == 1
    assert rows[0].stage == "enrich"


async def test_persist_winners_idempotent(storage):
    """Persist + load round-trips; re-persist overwrites, never duplicates."""
    ws = WinnerSet(
        experiment_tag=ExperimentTag("test_tag"),
        round_idx=1,
        winners={"enrich": "model_a", "translate": "model_b"},
        candidates_out=["model_a", "model_b", "model_c"],
    )
    await storage.persist_winners(
        experiment_tag=ExperimentTag("test_tag"), round_idx=1, winners=ws,
    )
    loaded = await storage.load_winners(
        experiment_tag=ExperimentTag("test_tag"), round_idx=1,
    )
    assert loaded is not None
    assert loaded.winners == {"enrich": "model_a", "translate": "model_b"}
    assert loaded.candidates_out == ["model_a", "model_b", "model_c"]

    # Overwrite — re-persist with different content.
    ws2 = WinnerSet(
        experiment_tag=ExperimentTag("test_tag"),
        round_idx=1,
        winners={"enrich": "model_x"},
        candidates_out=["model_x"],
    )
    await storage.persist_winners(
        experiment_tag=ExperimentTag("test_tag"), round_idx=1, winners=ws2,
    )
    loaded2 = await storage.load_winners(
        experiment_tag=ExperimentTag("test_tag"), round_idx=1,
    )
    assert loaded2.winners == {"enrich": "model_x"}


async def test_load_winners_missing_returns_none(storage):
    res = await storage.load_winners(
        experiment_tag=ExperimentTag("nonexistent"), round_idx=42,
    )
    assert res is None


async def test_failed_then_successful_retry_same_pk_overwrites(storage):
    """Cross-backend regression test for the retry-overwrite fix
    (audit: Critical — a prior FAILED row for the same PK must be
    overwritable by a subsequent successful retry, since PK collision
    on (composite_hash, provider, model, thinking) is exactly what a
    resumed run produces for a call that failed last time and succeeds
    this time). Covers InMemoryStorage and FileStorage identically via
    the ``storage`` fixture parametrization."""
    failed = _row(composite_hash="retry_h", response=None, error_class="RateLimited", cost=0.0)
    await storage.record_call(failed)

    # Failed row must not be resumable.
    cache_before = await storage.prefetch_resume_cache()
    assert ("retry_h", "openrouter", "test/model", "") not in cache_before

    ok = _row(composite_hash="retry_h", response='{"ok": true, "padding": "enough length here"}', error_class=None, cost=0.02)
    await storage.record_call(ok)

    # Successful retry must have overwritten the failed row in place —
    # exactly one row for this PK, and it's now resumable.
    rows = [r async for r in storage.query_rows(experiment_tag=ExperimentTag("test_tag")) if r.composite_hash == "retry_h"]
    assert len(rows) == 1
    assert rows[0].error_class is None
    assert rows[0].response is not None

    cache_after = await storage.prefetch_resume_cache()
    assert ("retry_h", "openrouter", "test/model", "") in cache_after

    # Spend reflects only the successful call, not both attempts summed.
    total = await storage.query_spend_total(experiment_tag=ExperimentTag("test_tag"))
    assert total == pytest.approx(0.02)


async def test_successful_row_is_never_overwritten_by_a_later_failure(storage):
    """The inverse direction: once a PK has a SUCCESSFUL row, a later
    call landing on the same PK with an error must NOT clobber it —
    only failed-to-successful transitions are allowed to overwrite."""
    ok = _row(composite_hash="stable_h", response='{"ok": true, "padding": "enough length here"}', error_class=None, cost=0.02)
    await storage.record_call(ok)

    later_failure = _row(composite_hash="stable_h", response=None, error_class="RateLimited", cost=0.0)
    await storage.record_call(later_failure)

    rows = [r async for r in storage.query_rows(experiment_tag=ExperimentTag("test_tag")) if r.composite_hash == "stable_h"]
    assert len(rows) == 1
    assert rows[0].error_class is None
    assert rows[0].response is not None


async def test_upsert_prompts_actually_persists_text(storage):
    """``upsert_prompts`` must persist the actual prompt TEXT somewhere
    durable, not merely return a stable hash tuple — a caller that
    later needs to re-inspect a stored prompt (debugging, audit,
    winner-substrate replay) depends on the text being retrievable,
    not just deduplicated in-memory for the current process."""
    sys_h, usr_h, comp_h = await storage.upsert_prompts(
        system_text="SYSTEM PROMPT TEXT", user_text="USER PROMPT TEXT",
    )
    assert sys_h and usr_h and comp_h

    if isinstance(storage, InMemoryStorage):
        assert comp_h in storage._prompts
        stored_sys, stored_usr = storage._prompts[comp_h]
        assert stored_sys == "SYSTEM PROMPT TEXT"
        assert stored_usr == "USER PROMPT TEXT"
    else:
        from llm_bench.storage.file import FileStorage
        assert isinstance(storage, FileStorage)
        prompts_path = storage.root / "_prompts.jsonl"
        assert prompts_path.exists()
        content = prompts_path.read_text(encoding="utf-8")
        assert "SYSTEM PROMPT TEXT" in content
        assert "USER PROMPT TEXT" in content
        assert comp_h in content


# ──────────────────────────────────────────────────────────────────────
# FileStorage-specific: path-traversal rejection (audit: security
# Finding 1, Critical). Not parametrized over the ``storage`` fixture —
# InMemoryStorage has no filesystem concept to escape.
# ──────────────────────────────────────────────────────────────────────

class TestFileStoragePathTraversal:
    @pytest.fixture
    async def file_storage(self, tmp_path):
        from llm_bench.storage.file import FileStorage
        s = FileStorage(tmp_path / "filestore")
        await s.initialize()
        yield s
        await s.close()

    @pytest.mark.parametrize(
        "malicious_tag",
        [
            "../escape",
            "../../etc/passwd",
            "..\\..\\windows",
            "/absolute/path",
            "a/../../b",
            "",
        ],
    )
    async def test_record_call_rejects_traversal_tag(self, file_storage, malicious_tag):
        r = _row(composite_hash="x", tag=malicious_tag)
        with pytest.raises(ValueError):
            await file_storage.record_call(r)

    async def test_traversal_tag_does_not_create_files_outside_root(self, file_storage, tmp_path):
        r = _row(composite_hash="x", tag="../escaped_dir")
        with pytest.raises(ValueError):
            await file_storage.record_call(r)
        assert not (tmp_path / "escaped_dir").exists()

    async def test_delete_experiment_rejects_traversal_tag(self, file_storage):
        with pytest.raises(ValueError):
            await file_storage.delete_experiment(experiment_tag=ExperimentTag("../escape"))

    async def test_legitimate_tag_still_works(self, file_storage):
        r = _row(composite_hash="x", tag="legit_tag-1.0")
        h = await file_storage.record_call(r)
        assert h == "x"


# ──────────────────────────────────────────────────────────────────────
# FileStorage-specific: SQLite "database is locked" retry/backoff
# (audit: 04-High)
# ──────────────────────────────────────────────────────────────────────

class TestFileStorageLockRetry:
    @pytest.fixture
    async def file_storage(self, tmp_path):
        from llm_bench.storage.file import FileStorage
        s = FileStorage(tmp_path / "filestore")
        await s.initialize()
        yield s
        await s.close()

    async def test_retries_and_succeeds_after_transient_lock_errors(self, file_storage):
        import sqlite3
        from unittest.mock import AsyncMock

        # Only the retry-wrapped INSERT (the write _db_execute_with_retry
        # actually protects) should fail transiently -- record_call's
        # earlier unprotected SELECT lookup must pass through untouched,
        # or this test would be exercising the wrong call site.
        real_execute = file_storage._db.execute
        call_count = {"n": 0}

        async def _flaky_execute(sql, params=()):
            if not sql.strip().upper().startswith("INSERT"):
                return await real_execute(sql, params)
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return await real_execute(sql, params)

        file_storage._db.execute = AsyncMock(side_effect=_flaky_execute)

        r = _row(composite_hash="retry_ok", tag="t1")
        h = await file_storage.record_call(r)
        assert h == "retry_ok"
        assert call_count["n"] == 3  # 2 failures + 1 success

    async def test_gives_up_after_max_attempts_and_reraises(self, file_storage):
        import sqlite3
        from unittest.mock import AsyncMock

        real_execute = file_storage._db.execute
        call_count = {"n": 0}

        async def _always_locked(sql, params=()):
            if not sql.strip().upper().startswith("INSERT"):
                return await real_execute(sql, params)
            call_count["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        file_storage._db.execute = AsyncMock(side_effect=_always_locked)

        r = _row(composite_hash="retry_fail", tag="t1")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await file_storage.record_call(r)
        assert call_count["n"] == 3  # exhausted _LOCK_RETRY_ATTEMPTS, then re-raised

    async def test_non_lock_operational_error_not_retried(self, file_storage):
        import sqlite3
        from unittest.mock import AsyncMock

        real_execute = file_storage._db.execute
        call_count = {"n": 0}

        async def _other_error(sql, params=()):
            if not sql.strip().upper().startswith("INSERT"):
                return await real_execute(sql, params)
            call_count["n"] += 1
            raise sqlite3.OperationalError("no such table: bogus")

        file_storage._db.execute = AsyncMock(side_effect=_other_error)

        r = _row(composite_hash="retry_other", tag="t1")
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await file_storage.record_call(r)
        assert call_count["n"] == 1  # non-lock error: failed immediately, no retry loop


async def test_delete_experiment_removes_rows_and_winners(storage):
    """delete_experiment is destructive; only fires when caller asks."""
    await storage.record_call(_row(composite_hash="a", tag="doomed"))
    await storage.record_call(_row(composite_hash="b", tag="doomed"))
    await storage.record_call(_row(composite_hash="c", tag="keep"))
    await storage.persist_winners(
        experiment_tag=ExperimentTag("doomed"), round_idx=1,
        winners=WinnerSet(
            experiment_tag=ExperimentTag("doomed"),
            round_idx=1, winners={"x": "y"},
        ),
    )

    n = await storage.delete_experiment(experiment_tag=ExperimentTag("doomed"))
    assert n == 2

    # 'keep' rows survive
    total_keep = await storage.query_spend_total(
        experiment_tag=ExperimentTag("keep"),
    )
    assert total_keep > 0
    # Doomed winners gone
    assert await storage.load_winners(
        experiment_tag=ExperimentTag("doomed"), round_idx=1,
    ) is None
