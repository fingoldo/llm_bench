"""ResumeCache unit tests — direct, no Benchmark/round_runner involved.

Previously had ZERO test coverage anywhere in the repository (audit:
05-Medium) despite being the in-memory mirror the whole "resume an
interrupted run" story runs through. Covers populate_from_storage
(including its filters), get, put, and __len__.
"""

from __future__ import annotations

from llm_bench import CachedResponse, ExperimentTag, InMemoryStorage, RunRow
from llm_bench.runner.resume import ResumeCache

TAG = ExperimentTag("resume_test")


def _row(*, model: str = "m1", comp_hash: str = "h1", response: str | None = None, error_class: str | None = None) -> RunRow:
    return RunRow(
        composite_hash=comp_hash,
        provider="openrouter",
        model=model,
        thinking="",
        stage="enrich",
        task_unit_id="t1",
        stratum=None,
        lang=None,
        parent_prompt_hash=None,
        experiment_tag=TAG,
        response=response if response is not None else '{"ok": true, "padding": "enough length here"}',
        error_class=error_class,
        error_message=None,
        cost_usd=0.01,
        effective_cost_usd=0.01,
        input_tokens=10,
        output_tokens=5,
        reasoning_tokens=0,
        cache_hit_tokens=0,
        cache_write_tokens=0,
    )


async def _store() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.initialize()
    return s


class TestPopulateFromStorage:
    async def test_loads_successful_rows(self):
        store = await _store()
        await store.record_call(_row(comp_hash="h1"))
        cache = ResumeCache()
        n = await cache.populate_from_storage(store)
        assert n == 1
        assert len(cache) == 1

    async def test_excludes_failed_rows_by_default(self):
        store = await _store()
        await store.record_call(_row(comp_hash="h1", response=None, error_class="RateLimited"))
        cache = ResumeCache()
        n = await cache.populate_from_storage(store)
        assert n == 0

    async def test_only_successful_false_includes_failed_rows(self):
        store = await _store()
        await store.record_call(_row(comp_hash="h1", response=None, error_class="RateLimited"))
        cache = ResumeCache()
        n = await cache.populate_from_storage(store, only_successful=False)
        assert n == 1

    async def test_min_response_len_filters_short_rows(self):
        store = await _store()
        await store.record_call(_row(comp_hash="h1", response="x"))
        cache = ResumeCache()
        n = await cache.populate_from_storage(store, min_response_len=20)
        assert n == 0

    async def test_replaces_prior_contents_not_accumulates(self):
        store = await _store()
        await store.record_call(_row(comp_hash="h1"))
        cache = ResumeCache()
        await cache.populate_from_storage(store)
        assert len(cache) == 1
        # Re-populating from a now-empty store should REPLACE, not add to,
        # the cache's contents.
        empty_store = await _store()
        await cache.populate_from_storage(empty_store)
        assert len(cache) == 0


class TestGetAndPut:
    async def test_get_after_populate_is_o1_hit(self):
        store = await _store()
        await store.record_call(_row(comp_hash="h1", model="m1"))
        cache = ResumeCache()
        await cache.populate_from_storage(store)
        hit = await cache.get(composite_hash="h1", provider="openrouter", model="m1", thinking="")
        assert hit is not None
        assert hit.composite_hash == "h1"

    async def test_get_miss_returns_none(self):
        cache = ResumeCache()
        hit = await cache.get(composite_hash="nope", provider="openrouter", model="m1", thinking="")
        assert hit is None

    async def test_put_makes_subsequent_get_hit_without_repopulating(self):
        cache = ResumeCache()
        resp = CachedResponse(
            composite_hash="h2", response="fresh response text here",
            input_tokens=1, output_tokens=1, reasoning_tokens=0,
            cost_usd=0.001, thinking="",
        )
        await cache.put(resp, provider="openrouter", model="m2")
        hit = await cache.get(composite_hash="h2", provider="openrouter", model="m2", thinking="")
        assert hit is not None
        assert hit.response == "fresh response text here"

    async def test_put_then_get_respects_thinking_dimension(self):
        cache = ResumeCache()
        resp = CachedResponse(
            composite_hash="h3", response="thinking-mode response",
            input_tokens=1, output_tokens=1, reasoning_tokens=0,
            cost_usd=0.001, thinking="high",
        )
        await cache.put(resp, provider="openrouter", model="m3")
        assert await cache.get(composite_hash="h3", provider="openrouter", model="m3", thinking="high") is not None
        assert await cache.get(composite_hash="h3", provider="openrouter", model="m3", thinking="") is None


class TestLen:
    async def test_empty_cache_len_zero(self):
        assert len(ResumeCache()) == 0

    async def test_put_increments_len(self):
        cache = ResumeCache()
        resp = CachedResponse(
            composite_hash="h4", response="x" * 25,
            input_tokens=1, output_tokens=1, reasoning_tokens=0,
            cost_usd=0.001, thinking="",
        )
        await cache.put(resp, provider="openrouter", model="m4")
        assert len(cache) == 1
