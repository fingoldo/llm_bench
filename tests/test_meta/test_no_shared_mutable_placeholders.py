"""A function must not return a module-level or enclosing-scope mutable placeholder by reference.

Wires ``py_ci_shared.shared_mutable_placeholder``: every caller would receive and mutate one shared object. Build the
value inside the function, so each call gets a fresh one."""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.shared_mutable_placeholder import assert_no_shared_mutable_placeholders

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "tests", "examples", "benchmarks")
SCAN_FILES = sorted(p for d in SCAN_DIRS for p in (REPO_ROOT / d).rglob("*.py"))


def test_no_shared_mutable_placeholders():
    """No function returns a shared mutable placeholder built once in an enclosing scope."""
    assert_no_shared_mutable_placeholders(SCAN_FILES, min_files=50)
