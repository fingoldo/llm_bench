"""A test must not assert on the text of a SQL statement.

Wires ``py_ci_shared.no_source_text_tests``: the spelling of a query does not show what it returns. Assert on the rows
a fake or real connection returns instead."""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.no_source_text_tests import assert_no_source_text_tests

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "tests", "examples", "benchmarks")
SCAN_FILES = sorted(p for d in SCAN_DIRS for p in (REPO_ROOT / d).rglob("*.py"))


def test_no_sql_source_text_tests():
    """No test asserts on SQL keyword text in the repo's test code."""
    assert_no_source_text_tests(SCAN_FILES, min_files=50)
