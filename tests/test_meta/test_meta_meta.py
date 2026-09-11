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
      ``_PERMITTED_PRIVATE_IMPORTS`` for legitimate cases.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from py_ci_shared.fail_message_quality import assert_fail_messages_actionable

_TEST_META_DIR = Path(__file__).resolve().parent

# Private symbols a meta-test is allowed to touch. Format:
# "test_meta_filename::imported_dotted_name".
_PERMITTED_PRIVATE_IMPORTS: set[str] = set()


def _meta_test_files() -> list[Path]:
    """All test_*.py files in tests/test_meta/, except this file."""
    out = []
    for py in _TEST_META_DIR.glob("test_*.py"):
        if py.name == Path(__file__).name:
            continue
        out.append(py)
    return sorted(out)


def test_pytest_fail_messages_are_actionable():
    """M1 — every static pytest.fail() message names a fix verb or a placeholder."""
    assert_fail_messages_actionable(_TEST_META_DIR, exclude=(Path(__file__).name,), min_audited=3)


def test_no_private_imports_from_production():
    """M2 — meta-tests don't reach into private internals."""
    violations: list[str] = []
    for path in _meta_test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            violations.append(f"{path.name}: SyntaxError: {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith("llm_bench"):
                    continue
                for alias in node.names:
                    if alias.name.startswith("_"):
                        key = f"{path.stem}::{mod}.{alias.name}"
                        if key in _PERMITTED_PRIVATE_IMPORTS:
                            continue
                        violations.append(
                            f"{path.name}:{node.lineno}: imports private "
                            f"{alias.name!r} from {mod!r}. Either whitelist "
                            f"in _PERMITTED_PRIVATE_IMPORTS with rationale, "
                            f"or test the public contract instead."
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "llm_bench" and any(p.startswith("_") for p in parts):
                        violations.append(
                            f"{path.name}:{node.lineno}: imports private "
                            f"module path {alias.name!r}. Use the public "
                            f"surface or whitelist with rationale."
                        )
    if violations:
        msg = "\n  ".join(violations)
        pytest.fail(f"Meta-tests reaching into private internals:\n  {msg}")
