"""Police the police: the meta-test suite itself follows actionable
failure-message + private-import discipline.

Two guarantees enforced:

  M1. Every ``pytest.fail(...)`` call inside ``tests/test_meta/`` carries
      an actionable message, via ``py_ci_shared.fail_message_quality``:
      a fix verb (``Add``, ``Either``, ``Refresh``, ...) or a ``<placeholder>``.
      A colon or a path no longer counts: every static message contains
      one, so the looser rule passed all of them without reading a word.

  M2. Meta-tests do NOT import private symbols (names starting with
      ``_``) from the production package. The whole point of a meta-test
      is to police the public contract; reaching into internals tests
      implementation, not behaviour. Whitelist via
      ``_PERMITTED_PRIVATE_IMPORTS`` for legitimate cases, checked by
      ``py_ci_shared.meta_private_imports``, where a permitted entry nothing
      imports fails.
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.meta_private_imports import assert_no_private_meta_imports
from py_ci_shared.fail_message_quality import assert_fail_messages_actionable

_TEST_META_DIR = Path(__file__).resolve().parent

# Private symbols a meta-test is allowed to touch. Format:
# "test_meta_filename::imported_dotted_name".
_PERMITTED_PRIVATE_IMPORTS: set[str] = set()


def test_pytest_fail_messages_are_actionable():
    """M1 — every static pytest.fail() message names a fix verb or a placeholder."""
    assert_fail_messages_actionable(_TEST_META_DIR, exclude=(Path(__file__).name,), min_audited=3)


def test_no_private_imports_from_production():
    """F2: a private import from a meta-test needs a permitted entry with its reason, and a permitted entry nothing imports fails."""
    assert_no_private_meta_imports(
        _TEST_META_DIR, ('llm_bench',), permitted=_PERMITTED_PRIVATE_IMPORTS, exclude=(Path(__file__).name,), any_segment=True, min_files=10
    )
