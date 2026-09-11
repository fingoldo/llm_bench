"""No guard that checks a VALUE may ride on an ``assert`` in ``src/llm_bench``.

``python -O`` deletes every ``assert``. A guard that only narrows a type for mypy loses nothing when it goes; a guard
that checks a bound, a sum or a membership loses the only thing enforcing it. ``py_ci_shared.value_bearing_asserts``
keeps ``assert x is not None`` and a bare name legal and flags the rest, ``isinstance`` included. There are none today,
so there is no baseline: the first one fails.
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.value_bearing_asserts import assert_no_value_bearing_asserts

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "llm_bench"


def test_no_value_bearing_asserts():
    """No production assert checks a value; raise explicitly instead."""
    # 7 asserts in src/llm_bench today; a walk that saw fewer than 3 has stopped reaching the package.
    assert_no_value_bearing_asserts(PACKAGE_ROOT, min_asserts_seen=3)
