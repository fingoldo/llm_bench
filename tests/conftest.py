"""Shared pytest fixtures for the llm_bench test suite.

Live tests (marker ``live``) require provider API keys; this conftest
auto-skips them when keys are absent, matching the pyutilz pattern.
"""

from __future__ import annotations

import os

import pytest

# ──────────────────────────────────────────────────────────────────────
# Live-test gating
# ──────────────────────────────────────────────────────────────────────

def pytest_collection_modifyitems(config, items):
    """Skip @pytest.mark.live tests when no provider key is set.

    Default CI runs ``pytest -m "not live"``; this auto-skip is a
    secondary safety net for `pytest -m live` invoked without env.
    """
    has_or = bool(os.environ.get("OPENROUTER_API_KEY"))
    has_anth = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_oai = bool(os.environ.get("OPENAI_API_KEY"))
    skip_live = pytest.mark.skip(reason="live test: set OPENROUTER_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY")
    for item in items:
        if "live" in item.keywords and not (has_or or has_anth or has_oai):
            item.add_marker(skip_live)


# ──────────────────────────────────────────────────────────────────────
# Postgres test DB gate
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def postgres_test_url() -> str | None:
    """Returns the URL when a Postgres test DB is configured, else None.

    Tests marked ``@pytest.mark.postgres`` should request this fixture
    and skip if it's None.
    """
    return os.environ.get("LLM_BENCH_TEST_DB_URL") or None
