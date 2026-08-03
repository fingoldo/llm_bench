"""Cost projection unit tests — estimate_call_cost + filter + estimator."""

from __future__ import annotations

import pytest

from llm_bench import (
    CatalogueEntry,
    CostFilter,
    CostProfile,
    estimate_call_cost,
    estimate_top_n_by_cost,
)
from llm_bench.cost.catalogue import ModelCatalogue

# ──────────────────────────────────────────────────────────────────────
# estimate_call_cost
# ──────────────────────────────────────────────────────────────────────

class TestEstimateCallCost:
    def test_typical_model(self):
        # $1/M input, $2/M output, no cache discount.
        # 10K system + 1K user => 11K input ($0.011)
        # 2K output + 1K reasoning => 3K output ($0.006)
        # cache_hit_ratio=0.7 means 7000 sys cached (no discount → same rate)
        # Total: still 0.011 + 0.006 = 0.017
        profile = CostProfile()
        result = estimate_call_cost(
            input_price_per_m=1.0,
            output_price_per_m=2.0,
            cache_read_price_per_m=None,
            profile=profile,
        )
        assert result is not None
        total, fresh, cached, output, has_cache = result
        assert has_cache is False
        # 11K * $1/M = $0.011, 3K * $2/M = $0.006
        assert total == pytest.approx(0.017)

    def test_cache_discount_applied(self):
        # cache_read 0.1/M (10x cheaper than prompt 1.0/M)
        profile = CostProfile(cache_hit_ratio=0.7)
        result = estimate_call_cost(
            input_price_per_m=1.0,
            output_price_per_m=2.0,
            cache_read_price_per_m=0.1,
            profile=profile,
        )
        assert result is not None
        total, fresh, cached, output, has_cache = result
        assert has_cache is True
        # 7K cached * $0.1/M = $0.0007
        # 3K fresh sys + 1K user = 4K * $1/M = $0.004
        # 3K output * $2/M = $0.006
        assert total == pytest.approx(0.0107)

    def test_missing_rates_returns_none(self):
        assert estimate_call_cost(
            input_price_per_m=None,
            output_price_per_m=2.0,
            cache_read_price_per_m=None,
            profile=CostProfile(),
        ) is None
        assert estimate_call_cost(
            input_price_per_m=1.0,
            output_price_per_m=None,
            cache_read_price_per_m=None,
            profile=CostProfile(),
        ) is None

    def test_negative_rates_rejected(self):
        # Meta-router placeholders price at -1000000.
        assert estimate_call_cost(
            input_price_per_m=-1.0,
            output_price_per_m=2.0,
            cache_read_price_per_m=None,
            profile=CostProfile(),
        ) is None

    def test_zero_price_treated_as_free_not_unusable(self):
        # Direct regression test for the `<= 0` -> `< 0` fix (audit:
        # 03-Medium): a legitimately free model (0.0 price, not tagged
        # `:free` in the catalogue id) must be PRICED as free, not
        # dropped from the estimate entirely as if it were unusable.
        result = estimate_call_cost(
            input_price_per_m=0.0,
            output_price_per_m=0.0,
            cache_read_price_per_m=None,
            profile=CostProfile(),
        )
        assert result is not None
        total, _fresh, _cached, _output, _has_cache = result
        assert total == pytest.approx(0.0)

    def test_zero_input_price_with_nonzero_output_price(self):
        result = estimate_call_cost(
            input_price_per_m=0.0,
            output_price_per_m=2.0,
            cache_read_price_per_m=None,
            profile=CostProfile(),
        )
        assert result is not None
        total, fresh, _cached, output, _has_cache = result
        assert fresh == pytest.approx(0.0)  # free input contributes nothing
        assert output > 0.0  # output is still priced
        assert total == pytest.approx(output)


# ──────────────────────────────────────────────────────────────────────
# CostFilter
# ──────────────────────────────────────────────────────────────────────

def _entry(
    *, model_id="provider/m1", in_p=0.5, out_p=1.0, ctx=128000,
    json_mode=True, uptime=0.99, moderated=False,
) -> CatalogueEntry:
    return CatalogueEntry(
        model_id=model_id,
        input_price_per_m=in_p,
        output_price_per_m=out_p,
        input_cache_read_price_per_m=None,
        context_length=ctx,
        max_completion_tokens=8000,
        supports_reasoning=False,
        supports_json_mode=json_mode,
        is_moderated=moderated,
        best_uptime_30m=uptime,
        best_latency_p50_ms=200,
        best_throughput_p50_tps=50,
        best_upstream_provider="provider",
    )


class TestCostFilter:
    def test_max_output_price(self):
        f = CostFilter(max_output_price_per_m=2.0)
        assert f.keep(_entry(out_p=1.5))
        assert not f.keep(_entry(out_p=3.0))

    def test_min_context_length(self):
        f = CostFilter(min_context_length=64000)
        assert f.keep(_entry(ctx=128000))
        assert not f.keep(_entry(ctx=32000))

    def test_require_json_mode(self):
        f = CostFilter(require_json_mode=True)
        assert f.keep(_entry(json_mode=True))
        assert not f.keep(_entry(json_mode=False))

    def test_min_uptime(self):
        f = CostFilter(require_healthy=True, min_uptime=0.95)
        assert f.keep(_entry(uptime=0.99))
        assert not f.keep(_entry(uptime=0.80))

    def test_exclude_moderated(self):
        f = CostFilter(exclude_moderated=True)
        assert f.keep(_entry(moderated=False))
        assert not f.keep(_entry(moderated=True))

    def test_moderated_penalty_breaks_cost_tie(self):
        # Direct regression test for moderated_penalty actually being
        # READ (audit: 05-Medium — the field was declared/documented
        # but never applied to the sort key). exclude_moderated=False
        # lets both entries through the hard filter; with equal raw
        # cost, the moderated one must still lose the tie once its
        # rank-cost is inflated by moderated_penalty > 1.0.
        cat = _FakeCatalogue([
            _entry(model_id="moderated/tied", in_p=1.0, out_p=2.0, moderated=True),
            _entry(model_id="open/tied", in_p=1.0, out_p=2.0, moderated=False),
        ])
        out = estimate_top_n_by_cost(
            cat, n=2, include_topn_best_by_provider=0,
            filter_=CostFilter(max_output_price_per_m=10.0, exclude_moderated=False, moderated_penalty=1.15),
        )
        assert [e.model_id for e in out] == ["open/tied", "moderated/tied"]
        # The projected dollar cost itself must NOT be inflated by the
        # penalty -- only the sort order changes (05-Medium's other
        # half: cost_usd stays the true projection callers see).
        assert out[0].cost_usd == pytest.approx(out[1].cost_usd)


# ──────────────────────────────────────────────────────────────────────
# estimate_top_n_by_cost end-to-end with a fake catalogue
# ──────────────────────────────────────────────────────────────────────

class _FakeCatalogue:
    """Minimal ModelCatalogue impl returning a fixed list."""
    def __init__(self, entries: list[CatalogueEntry]) -> None:
        self._entries = entries

    def list_models(
        self, *, refresh: bool = False, only_healthy: bool = True,
        min_uptime: float = 0.95,
    ) -> list[CatalogueEntry]:
        return list(self._entries)


class TestEstimateTopNByCost:
    def test_satisfies_protocol(self):
        cat = _FakeCatalogue([])
        assert isinstance(cat, ModelCatalogue)

    def test_cheapest_first(self):
        cat = _FakeCatalogue([
            _entry(model_id="exp/a", in_p=2.0, out_p=4.0),
            _entry(model_id="cheap/b", in_p=0.1, out_p=0.2),
            _entry(model_id="mid/c", in_p=1.0, out_p=2.0),
        ])
        # Loosen the default filter so all 3 land in the output.
        out = estimate_top_n_by_cost(
            cat, n=3, include_topn_best_by_provider=0,
            filter_=CostFilter(max_output_price_per_m=10.0),
        )
        assert [e.model_id for e in out] == ["cheap/b", "mid/c", "exp/a"]

    def test_filter_drops_expensive(self):
        cat = _FakeCatalogue([
            _entry(model_id="cheap/a", in_p=0.1, out_p=0.5),
            _entry(model_id="exp/b", in_p=10.0, out_p=20.0),
        ])
        out = estimate_top_n_by_cost(
            cat, n=10,
            filter_=CostFilter(max_output_price_per_m=2.0),
            include_topn_best_by_provider=0,
        )
        assert [e.model_id for e in out] == ["cheap/a"]

    def test_provider_diversity_seats(self):
        # Three providers with distinct cheapest models. n=2 normally
        # would only fit 2 entries, but provider diversity reserves
        # 1 seat per provider so all 3 land in the output.
        cat = _FakeCatalogue([
            _entry(model_id="prov_a/cheap", in_p=0.1, out_p=0.2),
            _entry(model_id="prov_a/expensive", in_p=1.0, out_p=2.0),
            _entry(model_id="prov_b/m1", in_p=0.5, out_p=1.0),
            _entry(model_id="prov_c/m1", in_p=0.3, out_p=0.6),
        ])
        out = estimate_top_n_by_cost(
            cat, n=2, include_topn_best_by_provider=1,
        )
        ids = {e.model_id for e in out}
        # All 3 providers represented, even though n=2.
        assert "prov_a/cheap" in ids
        assert "prov_b/m1" in ids
        assert "prov_c/m1" in ids
        assert "prov_a/expensive" not in ids

    def test_blacklist_drops_model(self):
        cat = _FakeCatalogue([
            _entry(model_id="bad/model", in_p=0.1, out_p=0.2),
            _entry(model_id="good/model", in_p=0.5, out_p=1.0),
        ])
        out = estimate_top_n_by_cost(
            cat, n=10, blacklist=["bad/model"],
            include_topn_best_by_provider=0,
        )
        assert [e.model_id for e in out] == ["good/model"]

    def test_no_reasoning_zeros_reasoning_cost(self):
        # Same catalogue, two profile modes — non-reasoning is cheaper.
        cat = _FakeCatalogue([_entry(model_id="m/1", in_p=0.5, out_p=1.0)])
        with_reason = estimate_top_n_by_cost(
            cat, n=1, include_reasoning_models=True,
            include_topn_best_by_provider=0,
        )[0]
        no_reason = estimate_top_n_by_cost(
            cat, n=1, include_reasoning_models=False,
            include_topn_best_by_provider=0,
        )[0]
        # Default profile has reasoning_tokens=1000 — turning that off
        # saves 1000 tokens * $1.0/M = $0.001
        assert no_reason.cost_usd < with_reason.cost_usd
        assert with_reason.cost_usd - no_reason.cost_usd == pytest.approx(0.001)
