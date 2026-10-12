"""No LLM call in llm_bench may carry a numeric output cap we chose. The model's own ceiling bounds the answer.

Ratchet: a cap found in the code must be on KNOWN_CAPS with its reason, and the list may only shrink. A new one fails.
The shared helpers live in py-ci-shared (``py_ci_shared.llm_output_caps``); this test only names the roots and the list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("py_ci_shared.llm_output_caps", reason="py-ci-shared is installed in the CI legs that run the meta tests")

from py_ci_shared.llm_output_caps import assert_known_caps_only_shrink, assert_no_new_output_caps, numeric_output_caps  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
_ROOTS = (REPO / "src", REPO / "benchmarks")

KNOWN_CAPS: dict[tuple[str, str, int], str] = {}


_NAME_HINTS = ("CAP_TOKENS", "PER_ITEM_CAP", "MAX_OUTPUT_TOKENS")
_DEFAULT_HINTS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def _found() -> set[tuple[str, str, int]]:
    return numeric_output_caps(_ROOTS, repo_root=REPO, name_hints=_NAME_HINTS, default_hints=_DEFAULT_HINTS)


def test_no_new_numeric_output_cap_in_llm_bench() -> None:
    assert_no_new_output_caps(_found(), KNOWN_CAPS)


def test_known_caps_only_shrink() -> None:
    assert_known_caps_only_shrink(_found(), KNOWN_CAPS)
