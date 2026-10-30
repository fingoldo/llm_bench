"""FileStorage branch tests: concurrent-initialize publish race, tag containment, resume-cache filters,
tolerant results.jsonl reads and the row <-> dict codec."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import aiosqlite
import orjson
import pytest

from llm_bench.core.types import ExperimentTag, RunRow
from llm_bench.storage import file as file_mod
from llm_bench.storage.file import FileStorage, _dict_to_row, _row_to_dict


def _row(*, composite_hash="h1", tag="tag", response='{"ok": true, "field": "value with enough length"}', **kw) -> RunRow:
    return RunRow(
        composite_hash=composite_hash, provider="openrouter", model="m", thinking="", stage="enrich", task_unit_id="t",
        stratum=None, lang=None, parent_prompt_hash=None, experiment_tag=tag, response=response, error_class=None,
        error_message=None, cost_usd=0.001, effective_cost_usd=0.001, input_tokens=1, output_tokens=1, reasoning_tokens=0,
        cache_hit_tokens=0, cache_write_tokens=0, **kw,
    )


class TestConcurrentInitialize:
    async def test_losing_caller_closes_its_duplicate_connection(self, tmp_path, monkeypatch):
        real_connect = aiosqlite.connect
        opened: list = []
        closed: list = []

        # Both callers must be inside connect before either publishes, or the second one returns at the
        # early `_initialized` check and the duplicate-connection branch is reached only by scheduling luck.
        both_connecting = asyncio.Event()
        in_flight = 0

        async def _connect(*args, **kwargs):
            nonlocal in_flight
            in_flight += 1
            if in_flight == 2:
                both_connecting.set()
            await both_connecting.wait()
            conn = await real_connect(*args, **kwargs)
            real_close = conn.close

            async def _close():
                closed.append(conn)
                await real_close()

            monkeypatch.setattr(conn, "close", _close)
            opened.append(conn)
            return conn

        monkeypatch.setattr(file_mod.aiosqlite, "connect", _connect)
        storage = FileStorage(tmp_path / "fs")
        await asyncio.gather(storage.initialize(), storage.initialize())

        assert len(opened) == 2
        assert closed == [c for c in opened if c is not storage._db]
        assert storage._db in opened
        await storage.close()


class TestWalSwitchRetry:
    """The WAL switch retries a locked database a bounded number of times; any other error is raised at once."""

    @staticmethod
    def _flaky_connect(monkeypatch, failures: list):
        real_connect = aiosqlite.connect

        class _Flaky:
            def __init__(self, conn):
                self._conn = conn

            async def execute(self, sql, *args):
                if "journal_mode" in sql and failures:
                    raise aiosqlite.OperationalError(failures.pop(0))
                return await self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        async def _connect(*args, **kwargs):
            return _Flaky(await real_connect(*args, **kwargs))

        monkeypatch.setattr(file_mod.aiosqlite, "connect", _connect)

    async def test_a_locked_wal_switch_is_retried_until_it_succeeds(self, tmp_path, monkeypatch):
        self._flaky_connect(monkeypatch, ["database is locked", "database is locked"])
        storage = FileStorage(tmp_path / "fs")
        await storage.initialize()
        assert storage._initialized is True
        await storage.close()

    async def test_a_wal_switch_still_locked_after_every_attempt_raises(self, tmp_path, monkeypatch):
        self._flaky_connect(monkeypatch, ["database is locked"] * 10)
        storage = FileStorage(tmp_path / "fs")
        with pytest.raises(aiosqlite.OperationalError, match="locked"):
            await storage.initialize()

    async def test_a_non_lock_error_on_the_wal_switch_is_raised_without_retrying(self, tmp_path, monkeypatch):
        failures = ["disk I/O error", "disk I/O error"]
        self._flaky_connect(monkeypatch, failures)
        storage = FileStorage(tmp_path / "fs")
        with pytest.raises(aiosqlite.OperationalError, match="disk I/O error"):
            await storage.initialize()
        assert failures == ["disk I/O error"]


class TestTagDirContainment:
    def test_tag_resolving_outside_root_is_rejected(self, tmp_path, monkeypatch):
        # The tag regex already rejects separators; with it neutralised the resolved-path check must still refuse an escape.
        monkeypatch.setattr(file_mod, "validate_experiment_tag", lambda tag: tag)
        storage = FileStorage(tmp_path / "root")
        with pytest.raises(ValueError, match="resolves outside storage root"):
            storage._tag_dir("../escape")

    def test_plain_tag_resolves_inside_root(self, tmp_path):
        storage = FileStorage(tmp_path / "root")
        assert storage._tag_dir("tag") == (tmp_path / "root" / "tag").resolve()


class TestPrefetchResumeCacheLengthFilters:
    async def test_only_successful_skips_rows_shorter_than_min_len(self, tmp_path):
        storage = FileStorage(tmp_path / "fs")
        await storage.record_call(_row(response="x" * 40))
        assert len(await storage.prefetch_resume_cache(only_successful=True, min_response_len=10)) == 1
        assert await storage.prefetch_resume_cache(only_successful=True, min_response_len=500) == {}
        await storage.close()

    async def test_not_only_successful_skips_rows_shorter_than_min_len(self, tmp_path):
        storage = FileStorage(tmp_path / "fs")
        await storage.record_call(_row(response="x" * 40))
        assert len(await storage.prefetch_resume_cache(only_successful=False, min_response_len=10)) == 1
        assert await storage.prefetch_resume_cache(only_successful=False, min_response_len=500) == {}
        await storage.close()


class TestQueryRowsTolerantRead:
    async def test_blank_and_corrupt_lines_are_skipped(self, tmp_path):
        storage = FileStorage(tmp_path / "fs")
        await storage.record_call(_row(composite_hash="a"))
        results = tmp_path / "fs" / "tag" / "results.jsonl"
        with open(results, "ab") as f:
            f.write(b"\n   \n{this is not json\n")
        await storage.record_call(_row(composite_hash="b"))

        rows = [r async for r in storage.query_rows(experiment_tag=ExperimentTag("tag"))]
        assert sorted(r.composite_hash for r in rows) == ["a", "b"]
        await storage.close()

    async def test_missing_results_file_yields_no_rows_and_logs_debug(self, tmp_path, caplog):
        storage = FileStorage(tmp_path / "fs")
        with caplog.at_level(logging.DEBUG, logger=file_mod.logger.name):
            rows = [r async for r in storage.query_rows(experiment_tag=ExperimentTag("never_written"))]
        assert rows == []
        assert any("results file not found" in r.getMessage() and "FileNotFoundError" in r.getMessage() for r in caplog.records)
        await storage.close()


class TestRowDictCodec:
    def test_logs_compressed_bytes_roundtrip_through_base64_text(self):
        row = _row(logs_compressed=b"\x00\x01binary\xff")
        d = _row_to_dict(row)
        assert isinstance(d["logs_compressed"], str)
        assert _dict_to_row(orjson.loads(orjson.dumps(d))).logs_compressed == b"\x00\x01binary\xff"

    def test_ts_roundtrips_as_iso_text(self):
        ts = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
        d = _row_to_dict(_row(ts=ts))
        assert d["ts"] == ts.isoformat()
        assert _dict_to_row(d).ts == ts

    def test_unparseable_ts_text_becomes_none(self):
        d = _row_to_dict(_row())
        d["ts"] = "not-a-timestamp"
        assert _dict_to_row(d).ts is None
