"""FileStorage — JSONL + SQLite-backed resume index.

Layout under ``root``:

    <root>/
      <experiment_tag>/
        prompts.jsonl              # one entry per (system, user) pair
        results.jsonl              # one entry per RunRow
        winners/round_<n>.json     # WinnerSet per round
      _index/
        resume_cache.sqlite        # tag-agnostic (composite_hash,
                                   #   provider, model, thinking) lookup

Strengths:
  - Zero-setup; runs on any disk.
  - Result rows are human-greppable JSONL — easy to diff between runs.
  - Resume index in SQLite keeps the hot-path lookup ~O(1).
  - Concurrent writers within a process serialise via asyncio.Lock. This
    backend is designed for SINGLE-WRITER-PROCESS use (solo runs / no-DB
    envs); ``PRAGMA busy_timeout`` + a bounded retry give a second
    process a fighting chance against index-write contention, but the
    raw JSONL append is NOT protected by any cross-process file lock —
    don't point two processes at the same ``root`` expecting the same
    guarantees as PostgresStorage (audit: 04-High corrected the prior
    "OS-locked via WAL" claim, which WAL mode alone does not provide).

Trade-offs (vs PostgresStorage):
  - Aggregation queries (``query_spend_by_stage``) scan JSONL — fast
    enough to ~50K rows; switch to PG beyond that.
  - No SQL joins / ad-hoc analytics — JSONL + jq is the surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import AsyncIterator

import aiosqlite
import orjson

from llm_bench.core.hashing import prompt_hashes
from llm_bench.core.predicates import MIN_USABLE_RESPONSE_LEN, is_row_usable
from llm_bench.core.types import CachedResponse, ExperimentTag, RunRow, WinnerSet, validate_experiment_tag

logger = logging.getLogger(__name__)

_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS resume_cache (
    composite_hash TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    thinking       TEXT NOT NULL,
    response       TEXT NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    reasoning_tokens INTEGER,
    cost_usd       REAL,
    error_class    TEXT,
    source_tag     TEXT,
    PRIMARY KEY (composite_hash, provider, model, thinking)
);
CREATE INDEX IF NOT EXISTS idx_resume_cache_tag
    ON resume_cache (source_tag);
CREATE TABLE IF NOT EXISTS prompts_index (
    composite_hash TEXT PRIMARY KEY
);
"""

_LOCK_RETRY_ATTEMPTS = 3
_LOCK_RETRY_BASE_DELAY_SEC = 0.1


class FileStorage:
    """Append-only JSONL backend with a SQLite resume index.

    The same row appended twice (same PK) is deduplicated at the index
    level — duplicate JSONL lines may exist on disk but the cache /
    query paths use the index as the canonical source.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = asyncio.Lock()
        """ONE lock guards every operation — fast point-lookups
        (get_cached, upsert_prompts) and slow full-file scans
        (prefetch_resume_cache, query_rows) alike (audit: 06-Medium).
        Not split into separate read/write or per-operation locks: this
        backend is documented as single-writer-process (see class
        docstring), so the realistic contention is one round's
        concurrent pipelines against each other, not a true multi-
        reader/multi-writer workload where lock granularity would
        change throughput. Splitting the lock risks a real correctness
        bug (a scan observing a partially-written concurrent append)
        for a perf win that hasn't been measured as necessary at this
        backend's intended scale (solo runs / no-DB environments,
        documented elsewhere as the smaller-scale option vs.
        PostgresStorage)."""
        self._db: aiosqlite.Connection | None = None
        self._initialized = False

    def __getstate__(self) -> dict:
        # asyncio.Lock and the live aiosqlite.Connection are both
        # event-loop-bound and unpicklable; drop them and rebuild on
        # unpickle so instances survive multiprocessing/caching.
        state = self.__dict__.copy()
        del state["_lock"]
        state["_db"] = None
        state["_initialized"] = False
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────

    async def initialize(self) -> None:
        # Guards BOTH the filesystem-mkdir calls (previously ran on
        # every hot-path call via record_call/upsert_prompts, which
        # each unconditionally invoke initialize() — audit: 04-Low) and
        # the connection-creation check-then-act below (audit: 04-Medium
        # TOCTOU: two concurrent first-callers could otherwise both pass
        # `self._db is None` and each create + leak a connection).
        async with self._lock:
            if self._initialized:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            index_dir = self.root / "_index"
            index_dir.mkdir(exist_ok=True)
            self._db = await aiosqlite.connect(index_dir / "resume_cache.sqlite")
            await self._db.execute("PRAGMA journal_mode=WAL;")
            # Tell SQLite to internally wait (not fail immediately) when
            # a write hits a lock held by another process/connection —
            # the actual mechanism the module docstring's old "OS-locked
            # via WAL" claim was missing (audit: 04-High).
            await self._db.execute("PRAGMA busy_timeout=5000;")
            await self._db.executescript(_INDEX_DDL)
            await self._db.commit()
            self._initialized = True

    async def close(self) -> None:
        # Takes the lock (unlike the rest of this method previously)
        # so a concurrent in-flight record_call/query_rows holding the
        # lock finishes before the connection is torn out from under it
        # (audit: 04-Medium).
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None
            self._initialized = False

    # ── path safety ───────────────────────────────────────────────

    def _tag_dir(self, experiment_tag: str) -> Path:
        """Resolve ``experiment_tag`` to a directory under ``root``.

        Two independent layers: (1) an allowlist regex rejecting path
        separators / ``..`` / absolute-path characters outright, (2) a
        resolved-containment check as defence-in-depth in case a future
        platform-specific path quirk slips past the regex. Un-gated,
        ``self.root / experiment_tag`` let a caller-supplied tag escape
        ``root`` entirely — proven arbitrary file write AND (via
        ``delete_experiment``'s ``shutil.rmtree``) arbitrary recursive
        delete (audit: security Finding 1, Critical).
        """
        validate_experiment_tag(experiment_tag)
        root_resolved = self.root.resolve()
        candidate = (self.root / experiment_tag).resolve()
        if not candidate.is_relative_to(root_resolved):
            raise ValueError(f"experiment_tag {experiment_tag!r} resolves outside storage root {self.root!r}")
        return candidate

    # ── blocking-IO helpers (run off the event loop) ────────────────

    @staticmethod
    def _append_line(path: Path, line: bytes) -> None:
        with open(path, "ab") as f:
            f.write(line)

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    @staticmethod
    def _read_bytes_or_none(path: Path) -> bytes | None:
        if not path.exists():
            return None
        with open(path, "rb") as f:
            return f.read()

    async def _db_execute_with_retry(self, sql: str, params: tuple[Any, ...]) -> None:
        """Write via the shared connection, retrying a couple of times
        with backoff on "database is locked" beyond what
        ``PRAGMA busy_timeout`` already absorbs — belt-and-suspenders
        for a second process contending on the same SQLite index
        (audit: 04-High)."""
        assert self._db is not None
        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            try:
                await self._db.execute(sql, params)
                return
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower() or attempt == _LOCK_RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(_LOCK_RETRY_BASE_DELAY_SEC * (2**attempt))

    # ── prompt dedup ──────────────────────────────────────────────

    async def upsert_prompts(
        self, *, system_text: str, user_text: str,
    ) -> tuple[str, str, str]:
        sys_h, usr_h, comp_h = prompt_hashes(system_text, user_text)
        # Prompts are persisted PER experiment-tag. We don't know the
        # tag here (the Protocol doesn't pass it), so we write a global
        # ``_prompts.jsonl`` keyed by composite_hash. Idempotent: checked
        # against a DEDICATED ``prompts_index`` table — the earlier
        # version checked ``resume_cache`` (the RESULT-row index)
        # instead, so a prompt whose composite_hash had already reached
        # ``resume_cache`` via a successful ``record_call`` (a common
        # ordering — see round_runner.py calling upsert_prompts once
        # per pipeline, not necessarily first-ever for that hash) made
        # this method silently skip writing the prompt text at all
        # (audit: 05-Medium, reproduced).
        prompts_path = self.root / "_prompts.jsonl"
        await self.initialize()
        async with self._lock:
            assert self._db is not None
            cur = await self._db.execute(
                "SELECT 1 FROM prompts_index WHERE composite_hash = ? LIMIT 1",
                (comp_h,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is not None:
                return sys_h, usr_h, comp_h
            entry = {
                "composite_hash": comp_h,
                "system_hash": sys_h,
                "user_hash": usr_h,
                "system_text": system_text,
                "user_text": user_text,
            }
            line = orjson.dumps(entry, option=orjson.OPT_SORT_KEYS) + b"\n"
            await asyncio.to_thread(self._append_line, prompts_path, line)
            await self._db_execute_with_retry(
                "INSERT OR IGNORE INTO prompts_index (composite_hash) VALUES (?)",
                (comp_h,),
            )
            await self._db.commit()
        return sys_h, usr_h, comp_h

    # ── result row writes ─────────────────────────────────────────

    async def record_call(self, row: RunRow) -> str:
        await self.initialize()
        if row.ts is None:
            row = _stamp_ts(row)
        tag_dir = self._tag_dir(row.experiment_tag)
        tag_dir.mkdir(parents=True, exist_ok=True)
        results_path = tag_dir / "results.jsonl"

        async with self._lock:
            assert self._db is not None
            # Idempotent on PK: if a previously SUCCESSFUL call is
            # already indexed, this is a true no-op re-record (contract
            # invariant #1). A prior FAILED attempt for the same PK is
            # deliberately NOT indexed (see below), so a retry that
            # succeeds this time still falls through and gets appended
            # — this is what lets FileStorage actually persist a
            # successful retry (unlike the Postgres/InMemory
            # ``ON CONFLICT DO NOTHING`` bug fixed alongside this).
            cur = await self._db.execute(
                "SELECT 1 FROM resume_cache WHERE composite_hash=? AND " "provider=? AND model=? AND thinking=? LIMIT 1",
                (row.composite_hash, row.provider, row.model, row.thinking),
            )
            existing = await cur.fetchone()
            await cur.close()
            if existing is not None:
                return row.composite_hash

            # JSONL append.
            entry = _row_to_dict(row)
            line = orjson.dumps(entry, option=orjson.OPT_SORT_KEYS) + b"\n"
            await asyncio.to_thread(self._append_line, results_path, line)

            # Index insert (only genuinely usable rows are cache-eligible
            # — failed AND unparseable/refusal rows still appear in
            # JSONL for audit-trail purposes but are excluded from the
            # index entirely, so re-tries don't get blocked and a parse
            # failure/refusal can't be resurrected as a cache hit on a
            # future process restart, which reading it back purely from
            # the in-memory ResumeCache decision in round_runner.py
            # would NOT have prevented on its own — audit: Critical).
            if is_row_usable(response=row.response, error_class=row.error_class, parse_failure_prefix=row.parse_failure_prefix):
                await self._db_execute_with_retry(
                    "INSERT OR IGNORE INTO resume_cache "
                    "(composite_hash, provider, model, thinking, response, "
                    " input_tokens, output_tokens, reasoning_tokens, "
                    " cost_usd, error_class, source_tag) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.composite_hash, row.provider, row.model,
                        row.thinking, row.response,
                        row.input_tokens or 0, row.output_tokens or 0,
                        row.reasoning_tokens or 0,
                        row.cost_usd or 0.0,
                        row.error_class, row.experiment_tag,
                    ),
                )
                await self._db.commit()
        return row.composite_hash

    # ── resume cache ──────────────────────────────────────────────

    async def prefetch_resume_cache(
        self, *, only_successful: bool = True, min_response_len: int = MIN_USABLE_RESPONSE_LEN,
    ) -> dict[tuple[str, str, str, str], CachedResponse]:
        await self.initialize()
        out: dict[tuple[str, str, str, str], CachedResponse] = {}
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT composite_hash, provider, model, thinking, response, "
                "input_tokens, output_tokens, reasoning_tokens, cost_usd, "
                "error_class, source_tag FROM resume_cache"
            )
            async for r in cur:
                ch, pv, md, th, resp, in_t, out_t, rsn_t, cost, err, src = r
                # resume_cache's own INSERT is already gated on
                # is_row_usable (see record_call) so a parse-failure row
                # never lands here in the first place -- this check only
                # needs error_class/length, not parse_failure_prefix.
                if only_successful:
                    if not is_row_usable(response=resp, error_class=err, min_len=min_response_len):
                        continue
                elif resp is None or len(resp) < min_response_len:
                    continue
                out[(ch, pv, md, th)] = CachedResponse(
                    composite_hash=ch, response=resp,
                    input_tokens=in_t or 0, output_tokens=out_t or 0,
                    reasoning_tokens=rsn_t or 0, cost_usd=cost or 0.0,
                    thinking=th, source_tag=src,
                )
            await cur.close()
        return out

    async def get_cached(
        self, *, composite_hash: str, provider: str, model: str, thinking: str,
    ) -> CachedResponse | None:
        await self.initialize()
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT response, input_tokens, output_tokens, "
                "reasoning_tokens, cost_usd, error_class, source_tag "
                "FROM resume_cache WHERE composite_hash=? AND provider=? "
                "AND model=? AND thinking=?",
                (composite_hash, provider, model, thinking),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        resp, in_t, out_t, rsn_t, cost, err, src = row
        if not is_row_usable(response=resp, error_class=err):
            return None
        return CachedResponse(
            composite_hash=composite_hash, response=resp,
            input_tokens=in_t or 0, output_tokens=out_t or 0,
            reasoning_tokens=rsn_t or 0, cost_usd=cost or 0.0,
            thinking=thinking, source_tag=src,
        )

    # ── analytics ─────────────────────────────────────────────────

    async def query_rows(
        self, *, experiment_tag: ExperimentTag, stage: str | None = None,
    ) -> AsyncIterator[RunRow]:
        tag_dir = self._tag_dir(str(experiment_tag))
        results_path = tag_dir / "results.jsonl"

        def _read_all() -> list[RunRow]:
            # Last-wins dedup keyed by PK: a later JSONL line for the
            # same (composite_hash, provider, model, thinking) is a
            # NEWER attempt (e.g. a successful retry appended after an
            # earlier failure — see record_call above). The previous
            # first-wins dedup kept the OLDEST entry, so analytics/
            # ranking/coverage kept seeing a stale failed row forever
            # even after the resume-cache index had already moved on to
            # the successful retry (audit: 01-High, reproduced).
            by_key: dict[tuple[str, str, str, str], RunRow] = {}
            try:
                with open(results_path, "rb") as f:
                    for raw in f:
                        if not raw.strip():
                            continue
                        try:
                            d = orjson.loads(raw)
                        except orjson.JSONDecodeError:
                            continue
                        row = _dict_to_row(d)
                        # Post-hoc filter: `stage` narrows the RETURNED
                        # rows, but every line in results.jsonl is still
                        # read + JSON-decoded first regardless of this
                        # filter — there's no index to seek by stage in
                        # an append-only JSONL file, so `stage=` costs
                        # the same I/O as no filter at all (audit: 06-
                        # Medium). Not fixed: an actual I/O-level
                        # optimization here means a real secondary index
                        # (a sibling per-stage file, or moving to
                        # sqlite-backed storage of results, not just the
                        # resume-cache index) — out of scope for a
                        # solo-run/no-DB backend whose own docstring
                        # already frames it as the small-scale option.
                        if stage is not None and row.stage != stage:
                            continue
                        key = (row.composite_hash, row.provider, row.model, row.thinking)
                        by_key[key] = row
            except FileNotFoundError:
                return []
            return list(by_key.values())

        # Blocking read runs off the event loop (audit: 04-High — this
        # used to run synchronously inline, freezing the ENTIRE event
        # loop for the scan's duration, not just callers waiting on
        # this lock). Still serialised against concurrent writers via
        # the same lock record_call/upsert_prompts use, for a
        # consistent-enough snapshot on this single-writer-process
        # backend; released before the caller iterates (no lock held
        # across consumer code).
        async with self._lock:
            rows_to_yield = await asyncio.to_thread(_read_all)
        for r in rows_to_yield:
            yield r

    async def query_spend_total(
        self, *, experiment_tag: ExperimentTag,
    ) -> float:
        total = 0.0
        async for row in self.query_rows(experiment_tag=experiment_tag):
            total += row.effective_cost_usd or 0.0
        return total

    async def _query_spend_grouped(self, *, experiment_tag: ExperimentTag, key_fn) -> dict[str, float]:
        """Group ``query_rows`` by ``key_fn(row)`` and sum ``effective_cost_usd``.

        2026-08-02 near-duplicate-function-body finding: query_spend_by_stage and
        query_spend_by_op independently duplicated this group-and-sum loop,
        differing only in the grouping key.
        """
        out: dict[str, float] = defaultdict(float)
        async for row in self.query_rows(experiment_tag=experiment_tag):
            out[key_fn(row)] += row.effective_cost_usd or 0.0
        return dict(out)

    async def query_spend_by_stage(
        self, *, experiment_tag: ExperimentTag,
    ) -> dict[str, float]:
        return await self._query_spend_grouped(experiment_tag=experiment_tag, key_fn=lambda row: row.stage)

    async def query_spend_by_op(
        self, *, experiment_tag: ExperimentTag,
    ) -> dict[str, float]:
        from llm_bench.storage.memory import _stage_to_op
        return await self._query_spend_grouped(experiment_tag=experiment_tag, key_fn=lambda row: _stage_to_op(row.stage))

    # ── per-stage winners ─────────────────────────────────────────

    async def persist_winners(
        self, *, experiment_tag: ExperimentTag, round_idx: int,
        winners: WinnerSet,
    ) -> None:
        await self.initialize()
        tag_dir = self._tag_dir(str(experiment_tag))
        payload = {
            "experiment_tag": winners.experiment_tag,
            "round_idx": winners.round_idx,
            "winners": dict(winners.winners),
            "candidates_out": list(winners.candidates_out),
            "stage_scores": dict(winners.stage_scores),
            "runner_up": dict(winners.runner_up),
        }
        # Locked like every other write in this backend (previously the
        # ONLY write path in this file not serialised via self._lock,
        # found by a static within-class resource-guard scan comparing
        # this method's unguarded `asyncio.to_thread(...)` call against
        # record_call/upsert_prompts/query_rows's guarded ones). Without
        # this, a concurrent delete_experiment() for the same tag could
        # rmtree the winners directory mid-write (open()/os.replace()
        # racing shutil.rmtree: a real crash on Windows, not just a
        # theoretical TOCTOU).
        async with self._lock:
            winners_dir = tag_dir / "winners"
            winners_dir.mkdir(parents=True, exist_ok=True)
            path = winners_dir / f"round_{round_idx}.json"
            data = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2)
            # Atomic write via temp + rename so a crash mid-write doesn't
            # leave a partial file; off the event loop like the other
            # blocking file ops in this backend.
            await asyncio.to_thread(self._write_atomic, path, data)

    async def load_winners(
        self, *, experiment_tag: ExperimentTag, round_idx: int,
    ) -> WinnerSet | None:
        tag_dir = self._tag_dir(str(experiment_tag))
        path = tag_dir / "winners" / f"round_{round_idx}.json"
        # Locked for the same reason as persist_winners above: an
        # unguarded read here raced delete_experiment()'s directory
        # removal (a TOCTOU between _read_bytes_or_none's own
        # path.exists() check and its open() call, previously outside
        # any guard).
        async with self._lock:
            data = await asyncio.to_thread(self._read_bytes_or_none, path)
        if data is None:
            return None
        payload = orjson.loads(data)
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
        await self.initialize()
        tag = str(experiment_tag)
        tag_dir = self._tag_dir(tag)
        n = 0
        async for _ in self.query_rows(experiment_tag=experiment_tag):
            n += 1
        # Drop index entries FIRST, then the tag dir — if the process
        # dies between the two steps, a leftover tag dir with no index
        # entries is inert (just undeleted files, not a cache pointing
        # at nothing); the reverse order (previous behaviour) could
        # leave `resume_cache` rows referencing a JSONL file that no
        # longer exists on disk (audit: 04-Medium). Both steps now share
        # ONE self._lock scope (found by a static within-class resource-
        # guard scan): the directory removal used to run AFTER the lock
        # was released, so a concurrent record_call/persist_winners for
        # the SAME tag (both serialised on this same lock) could still
        # be mid-write while shutil.rmtree deleted the directory out
        # from under it.
        assert self._db is not None
        import shutil
        async with self._lock:
            await self._db_execute_with_retry(
                "DELETE FROM resume_cache WHERE source_tag=?",
                (tag,),
            )
            await self._db.commit()
            if tag_dir.exists():
                await asyncio.to_thread(shutil.rmtree, tag_dir)
        return n


# ──────────────────────────────────────────────────────────────────────
# Row <-> dict helpers
# ──────────────────────────────────────────────────────────────────────

def _stamp_ts(row: RunRow) -> RunRow:
    from dataclasses import replace
    return replace(row, ts=datetime.now(UTC))


def _row_to_dict(row: RunRow) -> dict[str, Any]:
    d = asdict(row)
    if d.get("ts") is not None:
        d["ts"] = d["ts"].isoformat()
    # bytes (logs_compressed) doesn't JSON-serialise; encode as base64.
    if d.get("logs_compressed") is not None:
        import base64
        d["logs_compressed"] = base64.b64encode(d["logs_compressed"]).decode("ascii")
    return d


def _dict_to_row(d: dict[str, Any]) -> RunRow:
    if isinstance(d.get("ts"), str):
        try:
            d = {**d, "ts": datetime.fromisoformat(d["ts"])}
        except ValueError:
            d = {**d, "ts": None}
    if isinstance(d.get("logs_compressed"), str):
        import base64
        d = {**d, "logs_compressed": base64.b64decode(d["logs_compressed"])}
    return RunRow(**d)
