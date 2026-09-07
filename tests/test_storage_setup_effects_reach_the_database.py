"""Two `initialize()` methods return None, and so do versions that set nothing up.

`FileStorage.initialize` opens the SQLite index, sets two PRAGMAs, applies the DDL and commits.
`PostgresStorage.initialize` opens the pool, applies its DDL and stamps the schema version. Both
return None, and both are called on the hot path, so with their statements deleted the store still
answers every subsequent read and write against whatever the database already happened to be.

THE TWO PRAGMAS ARE NOT CONFIGURATION, they are the concurrency mechanism, and neither has a symptom
until two processes run at once:

  * `journal_mode=WAL`. Without it SQLite uses a rollback journal, where a reader blocks a writer.
    Every single-process test passes exactly the same.
  * `busy_timeout=5000`. Without it a write that hits a held lock fails IMMEDIATELY rather than
    waiting. The module's docstring used to claim the store was "OS-locked via WAL" while this line
    was missing, so the wait it described did not exist -- the failure was a spurious
    `database is locked` under a parallel run, which reads as flakiness rather than as a missing
    PRAGMA.

`PostgresStorage.initialize` carries the other shape: its DDL and its version stamp run inside a `try` that closes
the pool and re-raises, precisely so a half-initialised pool is never assigned to `self._pool`. That
ordering is asserted below, because the failure it prevents -- a non-None pool with no schema behind
it -- surfaces as an error in the first unrelated query.

Written to py-ci-shared/WRITING_TESTS.md habits 1, 2 and 5.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_bench.storage.file import FileStorage
from llm_bench.storage.postgres import PostgresStorage


@pytest.fixture
def sqlite_store(tmp_path, monkeypatch):
    """A `FileStorage` whose aiosqlite connection is recorded rather than opened."""
    db = AsyncMock(name="db")
    db.execute = AsyncMock()
    db.executescript = AsyncMock()
    db.commit = AsyncMock()

    monkeypatch.setattr("llm_bench.storage.file.aiosqlite.connect", AsyncMock(return_value=db))
    store = FileStorage(tmp_path / "runs")
    store._recorded = db
    return store


def _statements(db) -> list[str]:
    return [str(call.args[0]) for call in db.execute.await_args_list if call.args]


class TestTheSqliteIndexIsConfiguredBeforeItIsUsed:
    """Kills: either `PRAGMA` -> `pass`, which nothing single-process can tell apart."""

    async def test_write_ahead_logging_is_enabled(self, sqlite_store):
        """Without it a reader blocks a writer, and every one-process test still passes."""
        await sqlite_store.initialize()

        assert any("journal_mode=WAL" in s for s in _statements(sqlite_store._recorded))

    async def test_a_busy_writer_waits_instead_of_failing(self, sqlite_store):
        """The audit finding. The docstring claimed the store was locked via WAL while this line
        was missing, so a write hitting a held lock failed immediately -- surfacing as a spurious
        `database is locked` that reads as flakiness."""
        await sqlite_store.initialize()

        assert any("busy_timeout" in s for s in _statements(sqlite_store._recorded))

    async def test_the_schema_is_applied(self, sqlite_store):
        await sqlite_store.initialize()

        sqlite_store._recorded.executescript.assert_awaited_once()

    async def test_the_setup_is_committed(self, sqlite_store):
        """The DDL is in a transaction like anything else; uncommitted, the next connection finds
        no tables and the store rebuilds an empty index over a populated directory."""
        await sqlite_store.initialize()

        sqlite_store._recorded.commit.assert_awaited()

    async def test_a_second_call_does_not_reconfigure(self, sqlite_store):
        """`initialize` is invoked unconditionally by every hot-path write, so re-running the
        PRAGMAs and the DDL per call is the cost this guard exists to remove."""
        await sqlite_store.initialize()
        before = sqlite_store._recorded.execute.await_count

        await sqlite_store.initialize()

        assert sqlite_store._recorded.execute.await_count == before


@pytest.fixture
def pg_store(monkeypatch):
    """A `PostgresStorage` whose asyncpg pool is recorded rather than connected."""
    conn = AsyncMock(name="conn")
    conn.fetchrow = AsyncMock(return_value={"version": 1})

    acquired = MagicMock(name="acquire_ctx")
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=False)

    pool = AsyncMock(name="pool")
    pool.acquire = MagicMock(return_value=acquired)
    pool.close = AsyncMock()

    monkeypatch.setattr("asyncpg.create_pool", AsyncMock(return_value=pool))
    store = PostgresStorage("postgresql://user:secret@host/db")
    store._recorded_pool = pool
    store._recorded_conn = conn
    return store


class TestThePostgresPoolIsOnlyPublishedOnceItsSchemaExists:
    """`initialize` builds the pool, applies the DDL and stamps the version -- then assigns."""

    async def test_the_ddl_is_applied(self, pg_store):
        await pg_store.initialize()

        assert pg_store._recorded_conn.execute.await_count > 0

    async def test_the_pool_is_assigned_after_the_ddl_succeeds(self, pg_store):
        await pg_store.initialize()

        assert pg_store._pool is pg_store._recorded_pool

    async def test_failing_ddl_closes_the_pool_and_leaves_none_behind(self, pg_store):
        """The ordering the code goes out of its way to get right. A non-None pool with no schema
        behind it fails in the first unrelated query instead of here."""
        pg_store._recorded_conn.execute.side_effect = RuntimeError("permission denied for schema")

        with pytest.raises(RuntimeError):
            await pg_store.initialize()

        assert pg_store._pool is None
        pg_store._recorded_pool.close.assert_awaited_once()

    async def test_a_failing_version_check_is_treated_the_same(self, pg_store):
        """The stamp runs in the same `try` for the same reason -- a pool whose schema is a version
        this code cannot read is no more usable than one with no schema."""
        pg_store._recorded_conn.fetchrow.side_effect = RuntimeError("relation does not exist")

        with pytest.raises(RuntimeError):
            await pg_store.initialize()

        assert pg_store._pool is None

    async def test_a_connection_failure_does_not_carry_the_dsn(self, pg_store, monkeypatch):
        """Security finding 07-Low: `doctor` prints this error verbatim, so an unredacted DSN puts
        the password on someone's terminal and in their scrollback."""
        monkeypatch.setattr("asyncpg.create_pool", AsyncMock(side_effect=OSError("connection refused")))

        with pytest.raises(RuntimeError) as raised:
            await pg_store.initialize()

        assert "secret" not in str(raised.value)
