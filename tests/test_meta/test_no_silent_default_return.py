"""An ``except`` handler must not return a default while logging nothing.

Wires the ``silent_default_return`` rule of ``py_ci_shared.fail_open_handlers`` over every scanned file, with no
baseline: a handler that returns a default must log the exception type, so the fallback is visible."""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.fail_open_handlers import find_fail_open_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "tests", "examples", "benchmarks")
SCAN_FILES = sorted(p for d in SCAN_DIRS for p in (REPO_ROOT / d).rglob("*.py"))


def test_no_silent_default_return():
    """Every except handler that returns a default logs the exception it swallowed."""
    found = find_fail_open_handlers(SCAN_FILES, repo_root=REPO_ROOT, gate_name_re=".*")
    assert not found, "silent default returns: " + "; ".join(f"{f.path}:{f.lineno} {f.scope}" for f in found)
