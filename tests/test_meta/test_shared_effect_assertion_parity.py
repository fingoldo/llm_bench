"""Every database effect this repo performs is inspected by a test that imports the module.

A module that commits or executes, whose importing tests never look at that call, has a write
nothing checks. Deleting it would not fail one test, and in a storage backend that is rarely loud:
`initialize()` returns None either way, and every later read and write runs against whatever the
database already happened to be.

Three were reported when this first ran, all in setup paths where the missing statement has no
symptom until something else goes wrong -- the two SQLite PRAGMAs that ARE the concurrency mechanism
(a rollback journal and an immediate `database is locked` instead of a wait), and the Postgres DDL
that must succeed before the pool is published. They are asserted in
tests/test_storage_setup_effects_reach_the_database.py.

Zero now, so `accepted` is empty on purpose: a new unasserted effect is argued for here or not at
all.

The population assertion is not decoration. On a src layout this scan resolved no modules until a
fix landed upstream, so it passed having measured nothing -- and this repo was among those reported
clean on that basis. A count is the cheapest way to notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

py_ci_shared = pytest.importorskip("py_ci_shared", reason="py-ci-shared is a dev-only git dependency")

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_database_effect_is_asserted_by_an_importing_test():
    """Kills: a `commit` or `execute` that no importing test ever looks at."""
    from py_ci_shared.effect_assertion_parity import assert_effects_are_asserted, build_import_map

    import_map = build_import_map(REPO_ROOT)
    assert len(import_map) > 20, f"only {len(import_map)} modules resolved -- the scan lost its subject and this gate would pass vacuously"

    assert_effects_are_asserted(REPO_ROOT, import_map, ())
