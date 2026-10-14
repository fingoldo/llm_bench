"""A membership test and a later write to one shared dict must share one lock block.

Wires ``py_ci_shared.check_then_act_outside_lock``: when the test and the write are not covered by one lock, two
threads can both pass the test and both write."""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.check_then_act_outside_lock import assert_no_check_then_act_outside_lock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "tests", "examples", "benchmarks")
SCAN_FILES = sorted(p for d in SCAN_DIRS for p in (REPO_ROOT / d).rglob("*.py"))


def test_no_check_then_act_outside_lock():
    """Every check-then-act on a module-level dict is covered by one lock."""
    assert_no_check_then_act_outside_lock(SCAN_FILES, min_files=50)
