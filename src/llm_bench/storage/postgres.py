"""PostgresStorage — production backend backed by asyncpg.

Schema (default ``llm_bench`` schema; consumers can override via
``schema_name``):

    schema_version           (version) — single-row bookkeeping table
    benchmark_system_prompts (hash PK, prompt_text)
    benchmark_user_prompts   (hash PK, prompt_text)
    benchmark_prompts        (composite_hash PK, system_hash, user_hash,
                              system_text_ref, user_text_ref)
    benchmark_results        (composite_hash, provider, model, thinking)
                             PK = the four; everything else nullable.
    benchmark_winners        (experiment_tag, round_idx) PK; payload JSONB.

``initialize()`` runs the CREATE TABLE / CREATE INDEX statements
idempotently, then checks ``schema_version`` against ``_SCHEMA_VERSION``
below. No alembic dep — keeps the framework's transitive deps small.
``CREATE TABLE IF NOT EXISTS`` is a no-op against an EXISTING table, so
it can't retroactively apply a newer version's columns/constraints to
an already-deployed schema; the version check exists so that mismatch
surfaces as a clear ``RuntimeError`` at startup instead of a confusing
``UndefinedColumnError`` mid-run the first time new code touches a
column the deployed table doesn't have (audit: 08-High). Consumers
wanting a real migration tool still point their own Alembic/etc at the
schema — this repo just refuses to silently limp along on a stale one.

PK semantics (idempotent inserts):
  - ``record_call`` upserts: a fresh insert for a new key, or an
    overwrite of a PRIOR FAILURE by a later successful retry (never the
    reverse — a successful row is never overwritten by a later
    failure). Resume cache lookups read from ``benchmark_results``
    directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, UTC
from typing import Any
from collections.abc import AsyncIterator

from llm_bench.core.hashing import prompt_hashes
from llm_bench.core.predicates import MIN_USABLE_RESPONSE_LEN, is_row_usable
from llm_bench.core.redaction import redact_dsn
from llm_bench.core.types import CachedResponse, ExperimentTag, RunRow, WinnerSet

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _ddl(schema: str) -> list[str]:
    """Return idempotent CREATE statements for the framework's schema."""
    s = schema
    return [
        f"CREATE SCHEMA IF NOT EXISTS {s};",
        f"""
        CREATE TABLE IF NOT EXISTS {s}.schema_version (
            version INTEGER NOT NULL
        );
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {s}.benchmark_system_prompts (
            hash TEXT PRIMARY KEY,
            prompt_text TEXT NOT NULL,
            ts TIMESTAMPTZ DEFAULT NOW()
        );
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {s}.benchmark_user_prompts (
            hash TEXT PRIMARY KEY,
            prompt_text TEXT NOT NULL,
            ts TIMESTAMPTZ DEFAULT NOW()
        );
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {s}.benchmark_prompts (
            composite_hash TEXT PRIMARY KEY,
            system_hash TEXT NOT NULL REFERENCES {s}.benchmark_system_prompts(hash),
            user_hash TEXT NOT NULL REFERENCES {s}.benchmark_user_prompts(hash),
            ts TIMESTAMPTZ DEFAULT NOW()
        );
        """,
        # NOTE: benchmark_results.composite_hash is deliberately NOT a
        # FOREIGN KEY into benchmark_prompts, even though it logically
        # references it (record_call's real caller, round_runner.py,
        # always upserts the prompt first). A hard FK would reject any
        # record_call() invoked without a matching upsert_prompts() —
        # which is exactly how tests/unit/test_storage_protocol.py's
        # contract suite exercises record_call in isolation (synthetic
        # composite_hash literals, not values derived from real prompt
        # text) — silently making PostgresStorage diverge from the
        # InMemoryStorage/FileStorage contract those tests assert
        # identically. Left unenforced at the DB level; audited as a
        # deliberate trade-off (finding acknowledged, not applied).
        f"""
        CREATE TABLE IF NOT EXISTS {s}.benchmark_results (
            composite_hash TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            thinking TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL,
            task_unit_id TEXT NOT NULL,
            stratum TEXT,
            lang TEXT,
            parent_prompt_hash TEXT,
            experiment_tag TEXT NOT NULL,
            response TEXT,
            error_class TEXT,
            error_message TEXT,
            cost_usd NUMERIC CHECK (cost_usd IS NULL OR cost_usd >= 0),
            effective_cost_usd NUMERIC CHECK (effective_cost_usd IS NULL OR effective_cost_usd >= 0),
            input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
            output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
            reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
            cache_hit_tokens INTEGER CHECK (cache_hit_tokens IS NULL OR cache_hit_tokens >= 0),
            cache_write_tokens INTEGER CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
            cache_discount_usd NUMERIC CHECK (cache_discount_usd IS NULL OR cache_discount_usd >= 0),
            is_byok BOOLEAN,
            web_search_citations JSONB,
            upstream_resolved_model TEXT,
            upstream_provider TEXT,
            upstream_model TEXT,
            native_finish_reason TEXT,
            generation_id TEXT,
            mean_attempt_duration_sec NUMERIC CHECK (mean_attempt_duration_sec IS NULL OR mean_attempt_duration_sec >= 0),
            n_attempts INTEGER CHECK (n_attempts IS NULL OR n_attempts >= 0),
            http_status_sequence JSONB,
            per_attempt_durations_sec JSONB,
            logs TEXT,
            logs_compressed BYTEA,
            parse_failure_prefix TEXT,
            generation_details JSONB DEFAULT '{{}}',
            ts TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (composite_hash, provider, model, thinking)
        );
        """,
        f"CREATE INDEX IF NOT EXISTS idx_benchmark_results_tag ON {s}.benchmark_results (experiment_tag);",
        # Composite (experiment_tag, stage) — every actual query filtering
        # by `stage` also filters by `experiment_tag` in the same WHERE
        # (query_rows); a lone single-column `stage` index only let the
        # planner Bitmap-AND it with the tag index instead of doing one
        # direct composite lookup (audit: 08-Medium).
        f"CREATE INDEX IF NOT EXISTS idx_benchmark_results_tag_stage ON {s}.benchmark_results (experiment_tag, stage);",
        # No query in THIS file filters/joins on task_unit_id alone
        # (grepped every method: get_cached, prefetch_resume_cache,
        # query_rows, query_spend_total, query_spend_by_stage,
        # delete_experiment) — kept anyway as a deliberate accommodation
        # for an operator connecting directly via psql/a BI tool to
        # slice results by task unit, bypassing the Python API entirely
        # (audit: 08-Medium). Pure write-overhead if that external
        # use case never materializes for a given deployment; drop it
        # there if so — it costs nothing to leave documented-but-idle
        # for deployments that do use it.
        f"CREATE INDEX IF NOT EXISTS idx_benchmark_results_task_unit ON {s}.benchmark_results (task_unit_id);",
        # Partial index matching prefetch_resume_cache's WHERE clause
        # (response present, error_class empty, no parse failure) — that
        # query has no experiment_tag filter (the cache is deliberately
        # tag-agnostic), so none of the three indexes above cover it;
        # without this it's a full sequential scan of a table that grows
        # without bound across every run ever executed (audit: 08-High /
        # 06-High #4).
        f"""
        CREATE INDEX IF NOT EXISTS idx_benchmark_results_resume_cache
            ON {s}.benchmark_results (composite_hash, provider, model, thinking)
            WHERE response IS NOT NULL AND (error_class IS NULL OR error_class = '')
                  AND (parse_failure_prefix IS NULL OR parse_failure_prefix = '');
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {s}.benchmark_winners (
            experiment_tag TEXT NOT NULL,
            round_idx INTEGER NOT NULL,
            payload JSONB NOT NULL,
            ts TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (experiment_tag, round_idx)
        );
        """,
    ]


_RESULTS_COLS = (
    "composite_hash, provider, model, thinking, stage, task_unit_id, "
    "stratum, lang, parent_prompt_hash, experiment_tag, response, "
    "error_class, error_message, cost_usd, effective_cost_usd, "
    "input_tokens, output_tokens, reasoning_tokens, cache_hit_tokens, "
    "cache_write_tokens, cache_discount_usd, is_byok, "
    "web_search_citations, upstream_resolved_model, upstream_provider, "
    "upstream_model, native_finish_reason, generation_id, "
    "mean_attempt_duration_sec, n_attempts, http_status_sequence, "
    "per_attempt_durations_sec, logs, logs_compressed, "
    "parse_failure_prefix, generation_details, ts"
)
_RESULTS_PK_COLS = frozenset({"composite_hash", "provider", "model", "thinking"})
# Generated (not hand-duplicated) from _RESULTS_COLS so the UPDATE SET
# clause in record_call() can never drift out of sync with the column
# list itself (a hand-maintained parallel list was exactly the kind of
# duplication this framework's own audit flagged elsewhere).
_RESULTS_UPDATE_SET = ", ".join(f"{c}=EXCLUDED.{c}" for c in (col.strip() for col in _RESULTS_COLS.split(",")) if c not in _RESULTS_PK_COLS)


class PostgresStorage:
    """Asyncpg-backed BenchmarkStorage.

    The connection pool is created lazily on first ``initialize()`` and
    held until ``close()``. ``schema_name`` defaults to ``llm_bench`` so
    the framework's tables don't collide with a consumer's existing
    ``llm.*`` schema (VocabApp overrides to ``llm`` for backwards compat
    with its current Postgres data). Validated against a strict
    Postgres-identifier allowlist at construction time (audit: security
    Finding 2 / SQL Finding 5 — previously accepted ANY string and
    f-string-interpolated it into every DDL/DML statement in this file
    with a docstring-only, code-unenforced "operator-supplied, trust
    me" justification for the bandit ``# nosec B608`` suppressions
    below).

    Every query below f-string-interpolates ``self._schema`` into the
    table's qualified name; every actual VALUE (experiment_tag, hashes,
    row fields, ...) is passed as an asyncpg ``$n`` placeholder, never
    interpolated. ``self._schema`` is now validated in ``__init__``
    against ``^[A-Za-z_][A-Za-z0-9_]{0,62}$`` (a valid unquoted Postgres
    identifier, length-capped at Postgres's own 63-byte limit) before
    it's ever used in a query — the ``# nosec B608`` suppressions below
    are backed by that runtime check, not just by convention.

    ``max_connections`` (default 8) is intentionally conservative, NOT
    sized to ``RoundConfig.global_concurrency`` (default 30) — every
    concurrent pipeline makes at least 2 round-trips per stage
    (``upsert_prompts`` + ``record_call``), so a pool much smaller than
    ``global_concurrency`` means most of a round's DB I/O queues on
    connection acquisition instead of running in parallel (audit: 08-
    Medium). Raise this to roughly match ``global_concurrency`` if
    throughput matters more than staying light on a shared Postgres
    instance's connection budget — the two aren't auto-reconciled.
    """

    def __init__(
        self,
        url: str,
        *,
        schema_name: str = "llm_bench",
        min_connections: int = 1,
        max_connections: int = 8,
        command_timeout: float = 60.0,
    ) -> None:
        if not _SCHEMA_NAME_RE.fullmatch(schema_name):
            raise ValueError(
                f"schema_name must be a valid Postgres identifier matching " f"{_SCHEMA_NAME_RE.pattern!r} — got {schema_name!r}",
            )
        self._url = url
        self._schema = schema_name
        self._min = min_connections
        self._max = max_connections
        self._command_timeout = command_timeout
        self._pool: Any = None  # asyncpg.Pool
        self._lock = asyncio.Lock()
        """Guards ``initialize()``/``close()`` against the same
        check-then-act TOCTOU race ``FileStorage`` had (audit: 04/08-
        Medium): two concurrent ``initialize()`` callers both seeing
        ``self._pool is None`` would otherwise both call
        ``asyncpg.create_pool`` and leak the loser's pool."""

    def __getstate__(self) -> dict:
        # asyncio.Lock and the live asyncpg.Pool are both event-loop-bound
        # and unpicklable; drop them and rebuild on unpickle.
        state = self.__dict__.copy()
        del state["_lock"]
        state["_pool"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._lock = asyncio.Lock()

    @property
    def schema(self) -> str:
        return self._schema

    def _require_pool(self) -> Any:
        """Replaces the bare ``assert self._pool is not None`` used at
        every call site below. ``assert`` disappears under ``python -O``
        (bandit B101), silently turning a clear "call initialize()
        first" error into a raw ``AttributeError`` on ``None`` deep
        inside the method (audit: 08-Low)."""
        if self._pool is None:
            raise RuntimeError(f"{type(self).__name__}: call initialize() first")
        return self._pool

    # ── lifecycle ─────────────────────────────────────────────────

    async def initialize(self) -> None:
        async with self._lock:
            if self._pool is not None:
                return
            import asyncpg
            try:
                pool = await asyncpg.create_pool(
                    dsn=self._url,
                    min_size=self._min,
                    max_size=self._max,
                    command_timeout=self._command_timeout,
                )
            except Exception as exc:
                # Never let a raw connection failure carry the DSN
                # (potentially including a password) up to a caller that
                # may log/print it verbatim — cli/main.py's `doctor`
                # command does exactly that (audit: security 07-Low).
                raise RuntimeError(
                    f"PostgresStorage: failed to connect to {redact_dsn(self._url)!r}: " f"{type(exc).__name__}: {exc}",
                ) from exc
            # A crash/error here must not leave a half-initialised,
            # non-None self._pool behind — assign only after DDL + the
            # version check both succeed.
            try:
                async with pool.acquire() as conn:
                    for stmt in _ddl(self._schema):
                        await conn.execute(stmt)
                    await self._check_or_stamp_schema_version(conn)
            except Exception:
                await pool.close()
                raise
            self._pool = pool

    async def _check_or_stamp_schema_version(self, conn: Any) -> None:
        row = await conn.fetchrow(f"SELECT version FROM {self._schema}.schema_version LIMIT 1")  # nosec B608
        if row is None:
            await conn.execute(
                f"INSERT INTO {self._schema}.schema_version (version) VALUES ($1)",  # nosec B608
                _SCHEMA_VERSION,
            )
            return
        deployed = row["version"]
        if deployed != _SCHEMA_VERSION:
            raise RuntimeError(
                f"PostgresStorage schema '{self._schema}' is at version "
                f"{deployed}, but this llm_bench build expects version "
                f"{_SCHEMA_VERSION}. `CREATE TABLE IF NOT EXISTS` cannot "
                f"retroactively add columns/constraints to an existing "
                f"deployed table — run a manual migration (or point your "
                f"own Alembic/etc at the schema) before using this build "
                f"against this database.",
            )

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None

    # ── prompt dedup ──────────────────────────────────────────────

    async def upsert_prompts(
        self, *, system_text: str, user_text: str,
    ) -> tuple[str, str, str]:
        sys_h, usr_h, comp_h = prompt_hashes(system_text, user_text)
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"INSERT INTO {self._schema}.benchmark_system_prompts "  # nosec B608
                    f"(hash, prompt_text) VALUES ($1, $2) "
                    f"ON CONFLICT (hash) DO NOTHING",
                    sys_h, system_text,
                )
                await conn.execute(
                    f"INSERT INTO {self._schema}.benchmark_user_prompts "  # nosec B608
                    f"(hash, prompt_text) VALUES ($1, $2) "
                    f"ON CONFLICT (hash) DO NOTHING",
                    usr_h, user_text,
                )
                await conn.execute(
                    f"INSERT INTO {self._schema}.benchmark_prompts "  # nosec B608
                    f"(composite_hash, system_hash, user_hash) "
                    f"VALUES ($1, $2, $3) "
                    f"ON CONFLICT (composite_hash) DO NOTHING",
                    comp_h, sys_h, usr_h,
                )
        return sys_h, usr_h, comp_h

    # ── result row writes ─────────────────────────────────────────

    async def record_call(self, row: RunRow) -> str:
        pool = self._require_pool()
        ts = row.ts or datetime.now(UTC)
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._schema}.benchmark_results "  # nosec B608
                f"({_RESULTS_COLS}) VALUES "
                f"($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,"
                f"$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,"
                f"$29,$30,$31,$32,$33,$34,$35,$36,$37) "
                f"ON CONFLICT (composite_hash, provider, model, thinking) "
                # A prior row for this PK is overwritten ONLY when it was
                # NOT genuinely usable — a real provider FAILURE, or an
                # HTTP-level success the parser rejected
                # (parse_failure_prefix set, see round_runner.py). A
                # fresh successful retry after a transient error
                # (RateLimited/timeout/etc, the exact scenario
                # ResumeCache's "failed rows get re-tried" contract
                # promises) used to be silently dropped by a blanket
                # DO NOTHING, permanently shadowing the success behind
                # the stale failure and re-paying for the same call on
                # every future resume forever (audit: Critical, multiple
                # reports, reproduced). A prior genuinely-usable SUCCESS
                # is never overwritten, preserving resume-cache
                # determinism for already-cached hits.
                f"DO UPDATE SET {_RESULTS_UPDATE_SET} "  # nosec B608
                f"WHERE ({self._schema}.benchmark_results.error_class IS NOT NULL "  # nosec B608
                f"AND {self._schema}.benchmark_results.error_class <> '') "  # nosec B608
                f"OR ({self._schema}.benchmark_results.parse_failure_prefix IS NOT NULL "  # nosec B608
                f"AND {self._schema}.benchmark_results.parse_failure_prefix <> '')",  # nosec B608
                row.composite_hash, row.provider, row.model, row.thinking,
                row.stage, row.task_unit_id, row.stratum, row.lang,
                row.parent_prompt_hash, row.experiment_tag, row.response,
                row.error_class, row.error_message,
                row.cost_usd, row.effective_cost_usd,
                row.input_tokens, row.output_tokens, row.reasoning_tokens,
                row.cache_hit_tokens, row.cache_write_tokens,
                row.cache_discount_usd, row.is_byok,
                _to_json(row.web_search_citations),
                row.upstream_resolved_model, row.upstream_provider,
                row.upstream_model, row.native_finish_reason,
                row.generation_id, row.mean_attempt_duration_sec,
                row.n_attempts,
                _to_json(row.http_status_sequence),
                _to_json(row.per_attempt_durations_sec),
                row.logs, row.logs_compressed, row.parse_failure_prefix,
                _to_json(row.generation_details),
                ts,
            )
        return row.composite_hash

    # ── resume cache ──────────────────────────────────────────────

    async def prefetch_resume_cache(
        self, *, only_successful: bool = True, min_response_len: int = MIN_USABLE_RESPONSE_LEN,
    ) -> dict[tuple[str, str, str, str], CachedResponse]:
        pool = self._require_pool()
        out: dict[tuple[str, str, str, str], CachedResponse] = {}
        sql = (
            f"SELECT composite_hash, provider, model, thinking, response, "  # nosec B608
            f"input_tokens, output_tokens, reasoning_tokens, cost_usd, "
            f"experiment_tag FROM {self._schema}.benchmark_results "
            f"WHERE response IS NOT NULL "
            f"AND length(response) >= $1"
        )
        params: list[Any] = [min_response_len]
        if only_successful:
            sql += " AND (error_class IS NULL OR error_class = '') " "AND (parse_failure_prefix IS NULL OR parse_failure_prefix = '')"
        async with pool.acquire() as conn:
            # A server-side cursor (asyncpg's Cursor protocol) REQUIRES
            # an active transaction — asyncpg raises
            # NoActiveSQLTransactionError otherwise. This call was
            # missing the transaction wrapper entirely, so every real
            # invocation against a live Postgres server crashed
            # immediately, at the FIRST thing Benchmark.initialize()
            # does — the resume cache (this framework's headline
            # feature) was 100% non-functional on the "production"
            # backend (audit: Critical, verified against installed
            # asyncpg's own source). query_rows() below already does
            # this correctly; this call just didn't match it.
            async with conn.transaction():
                async for r in conn.cursor(sql, *params):
                    out[(r["composite_hash"], r["provider"], r["model"], r["thinking"])] = CachedResponse(
                        composite_hash=r["composite_hash"],
                        response=r["response"],
                        input_tokens=r["input_tokens"] or 0,
                        output_tokens=r["output_tokens"] or 0,
                        reasoning_tokens=r["reasoning_tokens"] or 0,
                        cost_usd=float(r["cost_usd"] or 0.0),
                        thinking=r["thinking"],
                        source_tag=r["experiment_tag"],
                    )
        return out

    async def get_cached(
        self, *, composite_hash: str, provider: str, model: str, thinking: str,
    ) -> CachedResponse | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                f"SELECT response, input_tokens, output_tokens, "  # nosec B608
                f"reasoning_tokens, cost_usd, error_class, parse_failure_prefix, "
                f"experiment_tag "
                f"FROM {self._schema}.benchmark_results "
                f"WHERE composite_hash=$1 AND provider=$2 AND model=$3 "
                f"AND thinking=$4",
                composite_hash, provider, model, thinking,
            )
        if r is None:
            return None
        if not is_row_usable(response=r["response"], error_class=r["error_class"], parse_failure_prefix=r["parse_failure_prefix"]):
            return None
        return CachedResponse(
            composite_hash=composite_hash, response=r["response"],
            input_tokens=r["input_tokens"] or 0,
            output_tokens=r["output_tokens"] or 0,
            reasoning_tokens=r["reasoning_tokens"] or 0,
            cost_usd=float(r["cost_usd"] or 0.0),
            thinking=thinking, source_tag=r["experiment_tag"],
        )

    # ── analytics ─────────────────────────────────────────────────

    async def query_rows(
        self, *, experiment_tag: ExperimentTag, stage: str | None = None,
    ) -> AsyncIterator[RunRow]:
        pool = self._require_pool()
        sql = (
            f"SELECT {_RESULTS_COLS} FROM {self._schema}.benchmark_results "  # nosec B608
            f"WHERE experiment_tag=$1"
        )
        params: list[Any] = [experiment_tag]
        if stage is not None:
            sql += " AND stage=$2"
            params.append(stage)
        async with pool.acquire() as conn:
            async with conn.transaction():
                async for r in conn.cursor(sql, *params):
                    yield _record_to_row(r)

    async def query_spend_total(
        self, *, experiment_tag: ExperimentTag,
    ) -> float:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            v = await conn.fetchval(
                f"SELECT COALESCE(SUM(effective_cost_usd), 0) "  # nosec B608
                f"FROM {self._schema}.benchmark_results "
                f"WHERE experiment_tag=$1",
                experiment_tag,
            )
        return float(v or 0.0)

    async def query_spend_by_stage(
        self, *, experiment_tag: ExperimentTag,
    ) -> dict[str, float]:
        pool = self._require_pool()
        out: dict[str, float] = {}
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT stage, COALESCE(SUM(effective_cost_usd), 0) AS spent "  # nosec B608
                f"FROM {self._schema}.benchmark_results "
                f"WHERE experiment_tag=$1 GROUP BY stage",
                experiment_tag,
            )
        for r in rows:
            out[r["stage"]] = float(r["spent"])
        return out

    async def query_spend_by_op(
        self, *, experiment_tag: ExperimentTag,
    ) -> dict[str, float]:
        from llm_bench.storage.memory import _stage_to_op
        by_stage = await self.query_spend_by_stage(experiment_tag=experiment_tag)
        out: dict[str, float] = defaultdict(float)
        for stage, spent in by_stage.items():
            out[_stage_to_op(stage)] += spent
        return dict(out)

    # ── per-stage winners ─────────────────────────────────────────

    async def persist_winners(
        self, *, experiment_tag: ExperimentTag, round_idx: int,
        winners: WinnerSet,
    ) -> None:
        pool = self._require_pool()
        payload = {
            "experiment_tag": winners.experiment_tag,
            "round_idx": winners.round_idx,
            "winners": dict(winners.winners),
            "candidates_out": list(winners.candidates_out),
            "stage_scores": dict(winners.stage_scores),
            "runner_up": dict(winners.runner_up),
        }
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._schema}.benchmark_winners "  # nosec B608
                f"(experiment_tag, round_idx, payload) VALUES ($1, $2, $3) "
                f"ON CONFLICT (experiment_tag, round_idx) DO UPDATE "
                f"SET payload=EXCLUDED.payload, ts=NOW()",
                experiment_tag, round_idx, json.dumps(payload),
            )

    async def load_winners(
        self, *, experiment_tag: ExperimentTag, round_idx: int,
    ) -> WinnerSet | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                f"SELECT payload FROM {self._schema}.benchmark_winners "  # nosec B608
                f"WHERE experiment_tag=$1 AND round_idx=$2",
                experiment_tag, round_idx,
            )
        if r is None:
            return None
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return WinnerSet(
            experiment_tag=payload["experiment_tag"],
            round_idx=int(payload["round_idx"]),
            winners=dict(payload.get("winners") or {}),
            candidates_out=list(payload.get("candidates_out") or []),
            stage_scores=dict(payload.get("stage_scores") or {}),
            runner_up=dict(payload.get("runner_up") or {}),
        )

    # ── admin ─────────────────────────────────────────────────────

    async def delete_experiment(
        self, *, experiment_tag: ExperimentTag,
    ) -> int:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            # Both deletes now share one transaction — a crash/dropped
            # connection between them used to be able to leave
            # benchmark_winners rows for a tag whose benchmark_results
            # rows were already gone (audit: 04-Medium / 08-High).
            async with conn.transaction():
                n = await conn.fetchval(
                    f"WITH d AS (DELETE FROM {self._schema}.benchmark_results "  # nosec B608
                    f"WHERE experiment_tag=$1 RETURNING 1) "
                    f"SELECT COUNT(*) FROM d",
                    experiment_tag,
                )
                await conn.execute(
                    f"DELETE FROM {self._schema}.benchmark_winners "  # nosec B608
                    f"WHERE experiment_tag=$1",
                    experiment_tag,
                )
        return int(n or 0)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _to_json(value: Any) -> str | None:
    """asyncpg accepts JSONB as a text-encoded JSON string."""
    if value is None:
        return None
    return json.dumps(value)


def _record_to_row(r: Any) -> RunRow:
    """Convert an asyncpg Record into a RunRow.

    JSONB columns come back as already-decoded Python values when the
    asyncpg jsonb codec is registered; stringly-typed when not. Handle
    both shapes defensively.
    """
    def _maybe_json(v):
        if v is None or not isinstance(v, str):
            return v
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v

    return RunRow(
        composite_hash=r["composite_hash"],
        provider=r["provider"],
        model=r["model"],
        thinking=r["thinking"],
        stage=r["stage"],
        task_unit_id=r["task_unit_id"],
        stratum=r["stratum"],
        lang=r["lang"],
        parent_prompt_hash=r["parent_prompt_hash"],
        experiment_tag=r["experiment_tag"],
        response=r["response"],
        error_class=r["error_class"],
        error_message=r["error_message"],
        cost_usd=float(r["cost_usd"]) if r["cost_usd"] is not None else None,
        effective_cost_usd=(float(r["effective_cost_usd"]) if r["effective_cost_usd"] is not None else None),
        input_tokens=r["input_tokens"],
        output_tokens=r["output_tokens"],
        reasoning_tokens=r["reasoning_tokens"],
        cache_hit_tokens=r["cache_hit_tokens"],
        cache_write_tokens=r["cache_write_tokens"],
        cache_discount_usd=(float(r["cache_discount_usd"]) if r["cache_discount_usd"] is not None else None),
        is_byok=r["is_byok"],
        web_search_citations=_maybe_json(r["web_search_citations"]),
        upstream_resolved_model=r["upstream_resolved_model"],
        upstream_provider=r["upstream_provider"],
        upstream_model=r["upstream_model"],
        native_finish_reason=r["native_finish_reason"],
        generation_id=r["generation_id"],
        mean_attempt_duration_sec=(float(r["mean_attempt_duration_sec"]) if r["mean_attempt_duration_sec"] is not None else None),
        n_attempts=r["n_attempts"],
        http_status_sequence=_maybe_json(r["http_status_sequence"]),
        per_attempt_durations_sec=_maybe_json(r["per_attempt_durations_sec"]),
        logs=r["logs"],
        logs_compressed=r["logs_compressed"],
        parse_failure_prefix=r["parse_failure_prefix"],
        generation_details=_maybe_json(r["generation_details"]) or {},
        ts=r["ts"],
    )
