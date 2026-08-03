"""ResumeCache — in-memory wrapper over BenchmarkStorage.

Tag-agnostic by design: a row recorded under ANY prior experiment
satisfies a cache lookup as long as
``(composite_hash, provider, model, thinking)`` matches. This is what
makes "resume an interrupted run without paying for completed calls"
work even across runs with different tags.

The runner pre-loads the cache once at startup via
``populate_from_storage``; per-call ``get`` is then O(1).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from llm_bench.core.predicates import MIN_USABLE_RESPONSE_LEN
from llm_bench.core.types import CachedResponse, make_cache_key

if TYPE_CHECKING:
    from llm_bench.storage.base import BenchmarkStorage

logger = logging.getLogger(__name__)


class ResumeCache:
    """Tag-agnostic resume lookup.

    Lifecycle:
      1. ``populate_from_storage(storage)`` — pulls all successful past
         rows into the in-memory dict.
      2. ``get(composite_hash, provider, model, thinking)`` — O(1)
         lookup.
      3. Optional ``put(row)`` to insert a freshly-recorded row into the
         cache without re-querying storage.
    """

    def __init__(self) -> None:
        self._entries: dict[
            tuple[str, str, str, str], CachedResponse,
        ] = {}
        self._lock = asyncio.Lock()

    def __getstate__(self) -> dict:
        # asyncio.Lock is bound to an event loop and unpicklable; drop it and
        # rebuild on unpickle so instances survive multiprocessing/caching.
        state = self.__dict__.copy()
        del state["_lock"]
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._lock = asyncio.Lock()

    async def populate_from_storage(
        self,
        storage: "BenchmarkStorage",
        *,
        only_successful: bool = True,
        min_response_len: int = MIN_USABLE_RESPONSE_LEN,
    ) -> int:
        """Bulk-load successful rows from storage. Returns count loaded."""
        async with self._lock:
            self._entries = await storage.prefetch_resume_cache(
                only_successful=only_successful,
                min_response_len=min_response_len,
            )
        n = len(self._entries)
        logger.info("[resume] loaded %d entries", n)
        return n

    async def get(
        self,
        *,
        composite_hash: str,
        provider: str,
        model: str,
        thinking: str = "",
    ) -> CachedResponse | None:
        """O(1) cache lookup. Returns None on miss."""
        key = make_cache_key(composite_hash, provider, model, thinking)
        async with self._lock:
            return self._entries.get(key)

    async def put(self, response: CachedResponse, *, provider: str, model: str) -> None:
        """Insert a freshly-completed call into the in-memory cache so
        subsequent same-key lookups within this run hit instantly.

        The storage backend already persisted the row via
        ``record_call``; this is the in-memory mirror.
        """
        key = make_cache_key(response.composite_hash, provider, model, response.thinking)
        async with self._lock:
            self._entries[key] = response

    def __len__(self) -> int:
        return len(self._entries)
