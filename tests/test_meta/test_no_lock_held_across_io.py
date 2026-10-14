"""Blocking I/O must not run while a lock is held.

Wires ``py_ci_shared.lock_held_across_io``: a ``with <lock>:`` block that calls connect, getconn, urlopen, sleep,
subprocess, or writes a file stalls every thread waiting for that lock. Move the I/O out and publish under the lock."""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.lock_held_across_io import assert_no_lock_held_across_io

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "tests", "examples", "benchmarks")
SCAN_FILES = sorted(p for d in SCAN_DIRS for p in (REPO_ROOT / d).rglob("*.py"))


def test_no_lock_held_across_io():
    """No blocking call sits inside a lock block in the repo's Python files."""
    assert_no_lock_held_across_io(SCAN_FILES, min_files=50)
