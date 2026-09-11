"""Every pytest marker used under tests/ is registered, or ``--strict-markers`` errors at collection.

``py_ci_shared.pytest_markers`` does the scan: decorators, ``pytestmark`` (plain or annotated), aliased marks and
``item.add_marker(...)``, against pyproject's ``markers`` and any conftest registration, with the markers pytest and
its plugins register themselves already known. ``expect_registered`` names markers this repository registers, so
a parser that stopped reading pyproject would fail here instead of reporting a clean tree.
"""

from __future__ import annotations

from pathlib import Path

from py_ci_shared.pytest_markers import assert_markers_registered

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_used_marker_is_registered():
    assert_markers_registered(REPO_ROOT, expect_registered=("slow", "integration", "live", "postgres"))
