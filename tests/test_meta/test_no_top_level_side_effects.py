"""``import llm_bench`` must NOT touch the network, the environment, or a database file at module-load time.

The probe is ``py_ci_shared.import_side_effects``: a fresh interpreter (so other tests' fixtures cannot pre-warm
anything) with ``socket.socket`` and ``urlopen`` blocked, ``os.environ`` writes recorded, and ``open()`` of a
database-style path refused, then ``import llm_bench``. A side effect the package catches and swallows still fails.
"""

from __future__ import annotations

from py_ci_shared.import_side_effects import assert_imports_have_no_side_effects


def test_top_level_import_pure():
    """``import llm_bench`` succeeds without network, environment writes or database I/O."""
    assert_imports_have_no_side_effects(
        ["llm_bench"], block_environ=True, forbid_open_tokens=(".db", ".sqlite", "postgresql://", "postgres://"), timeout=60
    )
