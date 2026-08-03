"""Single source of truth for the version string.

``llm_bench.__version__`` must match the ``[project] version`` line in
``pyproject.toml``. Drift between the two is the canonical "I forgot
to bump one of them" mistake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import llm_bench

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _version_from_pyproject() -> str:
    text = _PYPROJECT.read_text(encoding="utf-8")
    # Match the first ``version = "X.Y.Z"`` under ``[project]`` ONLY (avoid
    # false positives in tool-config sub-sections).
    m = re.search(
        r"\[project\][^\[]*?\nversion\s*=\s*\"([^\"]+)\"",
        text, re.DOTALL,
    )
    if m is None:
        pytest.fail(
            "Could not find [project] version line in pyproject.toml: " 'expected `version = "X.Y.Z"` under [project]. ' "Add the version field if missing."
        )
    return m.group(1)


def test_version_matches_pyproject():
    pkg_version = llm_bench.__version__
    py_version = _version_from_pyproject()
    if pkg_version != py_version:
        pytest.fail(
            f"Version drift: llm_bench.__version__ = {pkg_version!r} but " f"pyproject.toml [project].version = {py_version!r}. " f"Update both together."
        )
