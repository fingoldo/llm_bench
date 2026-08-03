"""InMemoryStorage — non-persistent reference implementation.

Used for unit / property / smoke tests. NEVER for production: data is
lost on process exit.

Optionally accepts ``snapshot_path`` for tests that want to inspect
state post-mortem; the snapshot is JSON-dumped on ``close()``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, UTC
from collections.abc import AsyncIterator

from llm_bench.core.hashing import prompt_hashes
from llm_bench.core.predicates import MIN_USABLE_RESPONSE_LEN, is_row_usable
from llm_bench.core.types import CachedResponse, ExperimentTag, RunRow, WinnerSet, make_cache_key


class InMemoryStorage:
    """Pure-Python reference impl of BenchmarkStorage.

    Storage is three dicts:
      - ``_prompts``:   composite_hash -> (system_text, user_text)
      - ``_rows``:      (composite_hash, provider, model, thinking) -> RunRow
      - ``_winners``:   (experiment_tag, round_idx) -> WinnerSet

    All accesses are guarded by a single ``asyncio.Lock`` so concurrent
    writers from a gather() don't corrupt the dicts. Reads grab the lock
    too — keeps the contract simple at the cost of a tiny perf hit.
    """

    def __init__(self) -> None:
        self._prompts: dict[str, tuple[str, str]] = {}
        # PK = (composite_hash, provider, model, thinking)
        self._rows: dict[tuple[str, str, str, str], RunRow] = {}
        self._winners: dict[tuple[str, int], WinnerSet] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
        self._closed = False

    def __getstate__(self) -> dict:
        # asyncio.Lock is bound to an event loop and unpicklable; drop it and
        # rebuild on unpickle so instances survive multiprocessing/caching.
        state = self.__dict__.copy()
        del state["_lock"]
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────

    async def initialize(self) -> None:
        # No-op for in-memory; track flag so tests can assert it ran.
        self._initialized = True

    async def close(self) -> None:
        self._closed = True

    # ── prompt dedup ──────────────────────────────────────────────

    async def upsert_prompts(
        self, *, system_text: str, user_text: str,
    ) -> tuple[str, str, str]:
        sys_h, usr_h, comp_h = prompt_hashes(system_text, user_text)
        async with self._lock:
            # Idempotent: re-recording the same hash is a no-op.
            if comp_h not in self._prompts:
                self._prompts[comp_h] = (system_text, user_text)
        return sys_h, usr_h, comp_h

    # ── result row writes ─────────────────────────────────────────

    async def record_call(self, row: RunRow) -> str:
        key = row.cache_key
        async with self._lock:
            existing = self._rows.get(key)
            if existing is not None and is_row_usable(
                response=existing.response, error_class=existing.error_class,
                parse_failure_prefix=existing.parse_failure_prefix,
            ):
                # Existing row is a genuinely usable recorded SUCCESS —
                # idempotent no-op re-record (contract invariant #1).
                return row.composite_hash
            # Existing row (if any) was NOT usable (failed, or a parse
            # failure/refusal — see is_row_usable): a later attempt for
            # the same PK is a retry and overwrites it, whether this
            # attempt itself succeeds or fails. Previously this backend
            # unconditionally kept the FIRST row ever seen for a PK
            # (matching Postgres's old blanket `ON CONFLICT DO NOTHING`)
            # — a real successful retry after a transient failure
            # (RateLimited/timeout/etc — exactly the case
            # ResumeCache's "failed rows get re-tried" contract
            # promises) was silently discarded, permanently shadowed
            # behind the stale failure, and re-paid-for on every future
            # resume (audit: Critical, reproduced).
            stamped = row if row.ts is not None else replace(
                row, ts=datetime.now(UTC),
            )
            self._rows[key] = stamped
        return row.composite_hash

    # ── resume cache ──────────────────────────────────────────────

    async def prefetch_resume_cache(
        self, *, only_successful: bool = True, min_response_len: int = MIN_USABLE_RESPONSE_LEN,
    ) -> dict[tuple[str, str, str, str], CachedResponse]:
        out: dict[tuple[str, str, str, str], CachedResponse] = {}
        async with self._lock:
            for key, row in self._rows.items():
                if only_successful:
                    if not is_row_usable(
                        response=row.response, error_class=row.error_class,
                        parse_failure_prefix=row.parse_failure_prefix,
                        min_len=min_response_len,
                    ):
                        continue
                elif row.response is None or len(row.response) < min_response_len:
                    continue
                if row.response is None:
                    # Unreachable (is_row_usable / the elif above both
                    # already guarantee this) — narrows the type for
                    # mypy, which can't see through is_row_usable's own
                    # None-check to know CachedResponse.response below
                    # is safe.
                    continue
                out[key] = CachedResponse(
                    composite_hash=row.composite_hash,
                    response=row.response,
                    input_tokens=row.input_tokens or 0,
                    output_tokens=row.output_tokens or 0,
                    reasoning_tokens=row.reasoning_tokens or 0,
                    cost_usd=row.cost_usd or 0.0,
                    thinking=row.thinking,
                    source_tag=row.experiment_tag,
                )
        return out

    async def get_cached(
        self, *, composite_hash: str, provider: str, model: str, thinking: str,
    ) -> CachedResponse | None:
        key = make_cache_key(composite_hash, provider, model, thinking)
        async with self._lock:
            row = self._rows.get(key)
            if row is None:
                return None
            if not is_row_usable(response=row.response, error_class=row.error_class, parse_failure_prefix=row.parse_failure_prefix):
                return None
            if row.response is None:
                # Unreachable (is_row_usable already guarantees this) —
                # narrows the type for mypy.
                return None
            return CachedResponse(
                composite_hash=row.composite_hash,
                response=row.response,
                input_tokens=row.input_tokens or 0,
                output_tokens=row.output_tokens or 0,
                reasoning_tokens=row.reasoning_tokens or 0,
                cost_usd=row.cost_usd or 0.0,
                thinking=row.thinking,
                source_tag=row.experiment_tag,
            )

    # ── analytics ─────────────────────────────────────────────────

    async def query_rows(
        self, *, experiment_tag: ExperimentTag, stage: str | None = None,
    ) -> AsyncIterator[RunRow]:
        # Snapshot under the lock, then yield outside (don't hold the
        # lock across consumer code). ``replace(r)`` (shallow copy of
        # the dataclass's top-level fields) is enough to stop a caller
        # from mutating the stored RunRow via the yielded reference —
        # the only thing that actually matters for isolation — without
        # recursively deep-copying every nested list/dict field on every
        # row on every call (audit: 06-Low; this backend is test-only,
        # but the pattern is cheap to fix and property-tests may push
        # larger n through it).
        async with self._lock:
            snap = [replace(r) for r in self._rows.values() if r.experiment_tag == experiment_tag and (stage is None or r.stage == stage)]
        for r in snap:
            yield r

    async def query_spend_total(
        self, *, experiment_tag: ExperimentTag,
    ) -> float:
        async with self._lock:
            return sum((r.effective_cost_usd or 0.0) for r in self._rows.values() if r.experiment_tag == experiment_tag)

    async def query_spend_by_stage(
        self, *, experiment_tag: ExperimentTag,
    ) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        async with self._lock:
            for r in self._rows.values():
                if r.experiment_tag != experiment_tag:
                    continue
                out[r.stage] += r.effective_cost_usd or 0.0
        return dict(out)

    async def query_spend_by_op(
        self, *, experiment_tag: ExperimentTag,
    ) -> dict[str, float]:
        # The framework derives ``op`` from ``stage`` by stripping the
        # last ``_<lang>`` suffix when present (translate_es -> translate).
        # InMemoryStorage replicates that here so the contract test can
        # assert the same shape across all backends.
        out: dict[str, float] = defaultdict(float)
        async with self._lock:
            for r in self._rows.values():
                if r.experiment_tag != experiment_tag:
                    continue
                op = _stage_to_op(r.stage)
                out[op] += r.effective_cost_usd or 0.0
        return dict(out)

    # ── per-stage winners ─────────────────────────────────────────

    async def persist_winners(
        self, *, experiment_tag: ExperimentTag, round_idx: int,
        winners: WinnerSet,
    ) -> None:
        async with self._lock:
            self._winners[(experiment_tag, round_idx)] = winners

    async def load_winners(
        self, *, experiment_tag: ExperimentTag, round_idx: int,
    ) -> WinnerSet | None:
        async with self._lock:
            ws = self._winners.get((experiment_tag, round_idx))
            # Deliberately still a deepcopy (not the shallow `replace()`
            # used for RunRow above): WinnerSet's nested `winners`/
            # `candidates_out`/`stage_scores`/`runner_up` containers CAN
            # be mutated well after load (a caller building a follow-up
            # WinnerSet from this one, tests inspecting/mutating it) —
            # unlike RunRow, there's no "never touched after write"
            # guarantee to lean on, and this method is called once per
            # round (not once per row), so the perf case for a shallow
            # copy here is negligible anyway.
            return None if ws is None else deepcopy(ws)

    # ── admin ─────────────────────────────────────────────────────

    async def delete_experiment(
        self, *, experiment_tag: ExperimentTag,
    ) -> int:
        async with self._lock:
            keys_to_drop = [k for k, r in self._rows.items() if r.experiment_tag == experiment_tag]
            for k in keys_to_drop:
                del self._rows[k]
            winner_keys = [wk for wk in self._winners if wk[0] == experiment_tag]
            for wk in winner_keys:
                del self._winners[wk]
        return len(keys_to_drop)


# ──────────────────────────────────────────────────────────────────────
# Helpers (also used by other backends — kept here as the canonical impl)
# ──────────────────────────────────────────────────────────────────────

# Known multi-character language codes we treat as one token when stripping
# the trailing ``_<lang>`` suffix. Avoids edge cases like a hypothetical
# stage ``op_xx_yy`` becoming ``op_xx`` instead of ``op``.
_KNOWN_LANGS = frozenset({
    "en", "es", "fr", "it", "de", "ru", "ka", "pt", "ja", "zh",
    "uk", "pl", "nl", "tr", "ar", "ko",
})


def _stage_to_op(stage: str) -> str:
    """Strip the trailing ``_<lang>`` suffix when ``<lang>`` looks like
    a 2-letter ISO code (or a known multi-char code).

    ``translate_es`` -> ``translate``
    ``validate_translate_de`` -> ``validate_translate``
    ``enrich`` -> ``enrich``
    """
    if "_" not in stage:
        return stage
    head, _, tail = stage.rpartition("_")
    if tail in _KNOWN_LANGS or (len(tail) == 2 and tail.isalpha() and tail.islower()):
        return head
    return stage
