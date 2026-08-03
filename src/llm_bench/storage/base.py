"""BenchmarkStorage Protocol — pluggable persistence layer.

Three implementations must satisfy this contract:
  - ``InMemoryStorage`` (storage/memory.py) — tests + smoke
  - ``FileStorage``     (storage/file.py)   — solo runs, no-DB envs (Phase D)
  - ``PostgresStorage`` (storage/postgres.py) — production (Phase D)

The contract test (tests/unit/test_storage_protocol.py) parametrizes
over all three and runs the same assertion suite against each.

Key invariants every backend MUST preserve:
  1. ``record_call`` is idempotent on (composite_hash, provider, model,
     thinking): re-recording a row that already succeeded for that key
     is a no-op (NOT a duplicate row, and the stored SUCCESS is never
     overwritten). A row that PREVIOUSLY FAILED for that key is the one
     exception — since invariant #2 below means failed rows get
     re-tried, a later attempt (which may itself succeed or fail) MUST
     overwrite the stale failure rather than being silently dropped.
     (Audit note: earlier revisions of this docstring said "same key
     re-recorded is a no-op" with no failure/success distinction, which
     every backend implemented literally as "first row for a PK wins,
     unconditionally" — silently discarding a real successful retry
     forever. Fixed across all three backends; state the distinction
     explicitly here so a future 4th backend doesn't reintroduce it.)
  2. Failed rows (error_class non-None / non-empty) are EXCLUDED from
     ``prefetch_resume_cache`` and ``get_cached`` — they get re-tried.
  3. ``upsert_prompts`` is idempotent on the system/user text — the
     same prompts always produce the same hashes regardless of how
     many times they're recorded.
  4. The cache is TAG-AGNOSTIC — a row recorded under one experiment_tag
     is reusable as a cache hit by a run under any other tag, as long as
     (composite_hash, provider, model, thinking) match.
  5. ``persist_winners`` is idempotent on (experiment_tag, round_idx) —
     re-persisting overwrites prior content, never duplicates.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from collections.abc import AsyncIterator

from llm_bench.core.predicates import MIN_USABLE_RESPONSE_LEN
from llm_bench.core.types import CachedResponse, ExperimentTag, RunRow, WinnerSet


@runtime_checkable
class BenchmarkStorage(Protocol):
    """Pluggable persistence Protocol.

    All methods are async. Backends that don't need lifecycle setup
    (``InMemoryStorage``) implement ``initialize`` / ``close`` as
    no-ops. Backends with cold-start cost (Postgres schema migration,
    File index rebuild) do their work in ``initialize``.
    """

    # ── lifecycle ─────────────────────────────────────────────────
    async def initialize(self) -> None:
        """Idempotent setup: create schema, index, etc. Safe to call
        repeatedly."""
        ...

    async def close(self) -> None:
        """Release connections / file handles. Safe to call repeatedly."""
        ...

    # ── prompt dedup ──────────────────────────────────────────────
    async def upsert_prompts(
        self, *, system_text: str, user_text: str,
    ) -> tuple[str, str, str]:
        """Persist (system, user) prompts under their content hashes.

        Returns ``(system_hash, user_hash, composite_hash)``. All three
        are SHA256 hex digests. Re-recording the same text is a no-op.
        """
        ...

    # ── result row writes ─────────────────────────────────────────
    async def record_call(self, row: RunRow) -> str:
        """Persist a benchmark row. Returns the composite_hash.

        Idempotent on (composite_hash, provider, model, thinking).
        Storage backends MUST NOT raise on a duplicate insert — they
        no-op.
        """
        ...

    # ── resume cache ──────────────────────────────────────────────
    async def prefetch_resume_cache(
        self, *, only_successful: bool = True, min_response_len: int = MIN_USABLE_RESPONSE_LEN,
    ) -> dict[tuple[str, str, str, str], CachedResponse]:
        """Bulk-load successful past calls into an in-memory map.

        Tag-agnostic: returns ALL rows in storage that meet the filter.
        Key: ``(composite_hash, provider, model, thinking)``.

        ``only_successful=True`` excludes rows with non-empty
        ``error_class`` (they get re-tried). ``min_response_len`` filters
        out rows whose response body is suspiciously short (catches
        EmptyContent rows that slipped past error_class).

        Scale note (audit: 08-High, unbounded-memory half of the
        finding): this materializes the ENTIRE matching result set into
        one in-memory ``dict`` — no ``LIMIT``/age window/pagination.
        Deliberate: the resume cache's whole point is "find ANY prior
        successful call regardless of when it ran", so bounding by
        recency would silently break resumability for an old task unit.
        A backend expecting results at a scale where this dict itself
        becomes the bottleneck needs a different resume-cache design
        (a bounded LRU backed by lazy per-key lookups via
        ``get_cached`` instead of eager full-materialization), not a
        tweak to this method — out of scope for a drop-in fix.
        """
        ...

    async def get_cached(
        self, *, composite_hash: str, provider: str, model: str, thinking: str,
    ) -> CachedResponse | None:
        """Single-row lookup. Used as a fallback when the in-memory cache
        was not pre-loaded (e.g. smoke tests). Most callers go through
        ``prefetch_resume_cache`` once at startup."""
        ...

    # ── analytics ─────────────────────────────────────────────────
    def query_rows(
        self, *, experiment_tag: ExperimentTag, stage: str | None = None,
    ) -> AsyncIterator[RunRow]:
        """Stream all rows matching ``experiment_tag`` (and optionally
        a stage filter). Iteration order is unspecified but stable
        within a single call.

        Intentionally NOT declared ``async def`` here: implementations are
        async-generator functions (``async def ... yield``), which calling
        code consumes directly via ``async for`` without awaiting first.
        An ``async def`` Protocol signature would type this as a coroutine
        that must be awaited to get the iterator, mismatching every real
        implementation and every call site (found via mypy 2026-07-11)."""
        ...

    async def query_spend_total(
        self, *, experiment_tag: ExperimentTag,
    ) -> float:
        """Sum ``effective_cost_usd`` across all rows of the tag."""
        ...

    async def query_spend_by_stage(
        self, *, experiment_tag: ExperimentTag,
    ) -> dict[str, float]:
        """Group ``effective_cost_usd`` sum by stage label."""
        ...

    async def query_spend_by_op(
        self, *, experiment_tag: ExperimentTag,
    ) -> dict[str, float]:
        """Group ``effective_cost_usd`` sum by op (canonical short label).

        For VocabApp this collapses ``translate_es``, ``translate_de``, …
        into one ``translate`` bucket — used by the per-stage budget gate
        (each ``op`` has one cap shared across its language children).
        """
        ...

    # ── per-stage winners ─────────────────────────────────────────
    async def persist_winners(
        self, *, experiment_tag: ExperimentTag, round_idx: int,
        winners: WinnerSet,
    ) -> None:
        """Persist a WinnerSet for the (tag, round) pair. Idempotent —
        a re-persist overwrites prior content."""
        ...

    async def load_winners(
        self, *, experiment_tag: ExperimentTag, round_idx: int,
    ) -> WinnerSet | None:
        """Load the WinnerSet persisted for (tag, round). None when
        absent."""
        ...

    # ── admin ─────────────────────────────────────────────────────
    async def delete_experiment(
        self, *, experiment_tag: ExperimentTag,
    ) -> int:
        """Delete every row + winner persisted under ``experiment_tag``.
        Returns the count of rows removed.

        DESTRUCTIVE — callers MUST gate this behind explicit user
        authorization (per the no-auto-truncate rule). Backends do not
        prompt; the gate lives at the call site.
        """
        ...
