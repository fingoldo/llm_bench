"""cost/openrouter.py unit tests — the raw-OR-JSON mapping layer.

Previously had ZERO test coverage (audit: 05/09-Medium) despite being
the ONLY code in the repository that parses a "dirty", partially-
trusted external API's response shape, with several defensive branches
(meta-router price sentinels, uptime as 0-100 vs 0-1, status as str vs
int) that clearly exist because real OpenRouter data has hit them
before — exactly the edge cases worth pinning down.
"""

from __future__ import annotations

from typing import Any

import pytest

from llm_bench.cost.openrouter import (
    OpenRouterCatalogue,
    _entry_from_or_row,
    _normalise_uptime,
    _per_token_to_per_m,
    _pick_best_upstream,
)


class TestPerTokenToPerM:
    def test_typical_value(self):
        assert _per_token_to_per_m(0.000001) == 1.0

    def test_none_returns_none(self):
        assert _per_token_to_per_m(None) is None

    def test_negative_sentinel_rejected(self):
        # Meta-router placeholders price at -1000000.
        assert _per_token_to_per_m(-1000000) is None

    def test_zero_is_valid_free_price(self):
        assert _per_token_to_per_m(0.0) == 0.0

    def test_string_number_coerced(self):
        assert _per_token_to_per_m("0.000002") == 2.0

    def test_unparseable_string_returns_none(self):
        assert _per_token_to_per_m("not-a-number") is None


class TestNormaliseUptime:
    def test_fraction_form_passthrough(self):
        assert _normalise_uptime(0.998) == 0.998

    def test_percentage_form_normalised(self):
        assert _normalise_uptime(99.8) == 0.998

    def test_exactly_one_treated_as_fraction(self):
        # 1.0 is ambiguous (100% as a fraction, or 1% as a percentage?)
        # — the implementation's `v > 1.0` check means exactly 1.0 stays
        # a fraction (100% uptime), which is the more common real case.
        assert _normalise_uptime(1.0) == 1.0

    def test_none_returns_none(self):
        assert _normalise_uptime(None) is None

    def test_unparseable_returns_none(self):
        assert _normalise_uptime("bad") is None


class TestPickBestUpstream:
    def test_empty_list_returns_empty_dict(self):
        assert _pick_best_upstream([]) == {}

    def test_prefers_live_over_degraded_string_status(self):
        endpoints = [
            {"status": "degraded", "provider_name": "b", "uptime_30m": 0.99},
            {"status": "live", "provider_name": "a", "uptime_30m": 0.90},
        ]
        assert _pick_best_upstream(endpoints)["provider_name"] == "a"

    def test_prefers_live_over_degraded_numeric_status(self):
        endpoints = [
            {"status": -1, "provider_name": "down"},
            {"status": 0, "provider_name": "up"},
        ]
        assert _pick_best_upstream(endpoints)["provider_name"] == "up"

    def test_missing_status_treated_as_degraded_not_worst(self):
        endpoints: list[dict[str, Any]] = [
            {"status": -1, "provider_name": "down"},
            {"provider_name": "unknown_status"},  # no "status" key at all
        ]
        assert _pick_best_upstream(endpoints)["provider_name"] == "unknown_status"

    def test_higher_uptime_wins_among_live(self):
        endpoints = [
            {"status": "live", "provider_name": "low", "uptime_30m": 0.90},
            {"status": "live", "provider_name": "high", "uptime_30m": 0.999},
        ]
        assert _pick_best_upstream(endpoints)["provider_name"] == "high"

    def test_lower_latency_wins_on_uptime_tie(self):
        endpoints = [
            {"status": "live", "provider_name": "slow", "uptime_30m": 0.99, "latency_p50_ms": 500},
            {"status": "live", "provider_name": "fast", "uptime_30m": 0.99, "latency_p50_ms": 100},
        ]
        assert _pick_best_upstream(endpoints)["provider_name"] == "fast"


class TestEntryFromOrRow:
    def _row(self, **overrides):
        base = {
            "id": "vendor/model-name",
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "context_length": 128000,
            "top_provider": {"context_length": 128000, "max_completion_tokens": 8000, "is_moderated": False},
            "supported_parameters": ["response_format", "reasoning"],
            "health": {
                "best_uptime_30m": 99.5,
                "best_latency_p50_ms": 200,
                "best_throughput_p50_tps": 40,
                "endpoints": [{"status": "live", "provider_name": "vendor", "uptime_30m": 0.995, "max_completion_tokens": 8000}],
            },
        }
        base.update(overrides)
        return base

    def test_typical_row_parses(self):
        entry = _entry_from_or_row(self._row())
        assert entry is not None
        assert entry.model_id == "vendor/model-name"
        assert entry.input_price_per_m == 1.0
        assert entry.output_price_per_m == 2.0
        assert entry.context_length == 128000
        assert entry.supports_json_mode is True
        assert entry.supports_reasoning is True
        assert entry.is_moderated is False
        assert entry.best_uptime_30m == 0.995

    def test_missing_id_rejected(self):
        assert _entry_from_or_row(self._row(id="")) is None

    def test_free_tier_tag_rejected(self):
        assert _entry_from_or_row(self._row(id="vendor/model:free")) is None

    def test_free_tier_tag_case_insensitive(self):
        assert _entry_from_or_row(self._row(id="vendor/Model:FREE")) is None

    def test_context_length_takes_more_pessimistic_of_two_fields(self):
        row = self._row(
            context_length=200000,
            top_provider={"context_length": 64000, "max_completion_tokens": 8000, "is_moderated": False},
        )
        entry = _entry_from_or_row(row)
        assert entry is not None
        assert entry.context_length == 64000

    def test_missing_pricing_gives_none_prices_not_a_crash(self):
        entry = _entry_from_or_row(self._row(pricing={}))
        assert entry is not None
        assert entry.input_price_per_m is None
        assert entry.output_price_per_m is None

    def test_cache_read_price_first_matching_key_wins(self):
        row = self._row(pricing={"prompt": "0.000001", "completion": "0.000002", "input_cache_read": "0.0000001"})
        entry = _entry_from_or_row(row)
        assert entry is not None
        assert entry.input_cache_read_price_per_m == pytest.approx(0.1)

    def test_status_as_numeric_code_parsed(self):
        row = self._row(health={
            "best_uptime_30m": 99.0, "best_latency_p50_ms": 100, "best_throughput_p50_tps": 10,
            "endpoints": [{"status": 0, "provider_name": "num_status", "max_completion_tokens": 4000}],
        })
        entry = _entry_from_or_row(row)
        assert entry is not None
        assert entry.best_upstream_provider == "num_status"

    def test_max_completion_tokens_prefers_live_upstream_over_top_provider_claim(self):
        # top_provider claims 16000 (meta-router's optimistic max), but
        # the actual live upstream endpoint only supports 4000 — the
        # real upstream value should win.
        row = self._row(
            top_provider={"context_length": 128000, "max_completion_tokens": 16000, "is_moderated": False},
            health={
                "best_uptime_30m": 99.0, "best_latency_p50_ms": 100, "best_throughput_p50_tps": 10,
                "endpoints": [{"status": "live", "provider_name": "real", "max_completion_tokens": 4000}],
            },
        )
        entry = _entry_from_or_row(row)
        assert entry is not None
        assert entry.max_completion_tokens == 4000

    def test_no_healthy_endpoints_falls_back_to_top_provider_max_tokens(self):
        row = self._row(health={"best_uptime_30m": None, "best_latency_p50_ms": None, "best_throughput_p50_tps": None, "endpoints": []})
        entry = _entry_from_or_row(row)
        assert entry is not None
        assert entry.max_completion_tokens == 8000  # from top_provider

    def test_is_moderated_flag_propagates(self):
        row = self._row(top_provider={"context_length": 128000, "max_completion_tokens": 8000, "is_moderated": True})
        entry = _entry_from_or_row(row)
        assert entry is not None
        assert entry.is_moderated is True

    def test_supported_parameters_as_bare_string_normalised_to_list(self):
        row = self._row(supported_parameters="response_format")
        entry = _entry_from_or_row(row)
        assert entry is not None
        assert entry.supports_json_mode is True
        assert entry.supports_reasoning is False


class TestListModelsFetchFailure:
    """audit: 09-Medium -- a raised exception from pyutilz's fetch must
    surface as a clear, catalogue-scoped RuntimeError with the original
    exception chained, not propagate an opaque nested-dependency error
    with no indication of where it came from."""

    def test_network_failure_wrapped_in_runtime_error(self, monkeypatch):
        import pyutilz.llm

        def _raising_fetch(**kwargs):
            raise ConnectionError("OpenRouter API unreachable")

        monkeypatch.setattr(pyutilz.llm, "list_openrouter_models", _raising_fetch)

        cat = OpenRouterCatalogue()
        with pytest.raises(RuntimeError, match="failed to fetch") as exc_info:
            cat.list_models()
        assert isinstance(exc_info.value.__cause__, ConnectionError)
        assert "OpenRouter API unreachable" in str(exc_info.value.__cause__)

    def test_malformed_response_failure_also_wrapped(self, monkeypatch):
        import pyutilz.llm

        def _raising_fetch(**kwargs):
            raise ValueError("malformed catalogue response")

        monkeypatch.setattr(pyutilz.llm, "list_openrouter_models", _raising_fetch)

        cat = OpenRouterCatalogue()
        with pytest.raises(RuntimeError):
            cat.list_models()
