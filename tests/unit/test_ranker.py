"""compute_ranking unit tests against InMemoryStorage."""

from __future__ import annotations

import math

import pytest

from llm_bench import (
    ExperimentTag,
    InMemoryStorage,
    RunRow,
    compute_ranking,
    default_row_scorer,
)
from llm_bench.ranking.ranker import _clamp_score


def _row(
    *, model: str, stage: str = "enrich",
    response: str | None = '{"ok": true}_______________padding',
    error_class: str | None = None,
    cost: float = 0.005,
    eff_cost: float | None = None,
    in_tok: int = 1000,
    out_tok: int = 200,
    rsn_tok: int = 0,
    latency: float = 5.0,
    tag: str = "run_x",
) -> RunRow:
    return RunRow(
        composite_hash=f"h_{model}_{stage}_{response[:5] if response else 'X'}",
        provider="openrouter",
        model=model,
        thinking="",
        stage=stage,
        task_unit_id="t1",
        stratum="v",
        lang=None,
        parent_prompt_hash=None,
        experiment_tag=tag,
        response=response,
        error_class=error_class,
        error_message=None,
        cost_usd=cost,
        effective_cost_usd=eff_cost if eff_cost is not None else cost,
        input_tokens=in_tok,
        output_tokens=out_tok,
        reasoning_tokens=rsn_tok,
        cache_hit_tokens=0,
        cache_write_tokens=0,
        mean_attempt_duration_sec=latency,
    )


class TestDefaultRowScorer:
    def test_success(self):
        assert default_row_scorer(_row(model="m1")) == 1.0

    def test_error_class_zero(self):
        assert default_row_scorer(_row(model="m1", error_class="ContextOverflow")) == 0.0

    def test_short_response_zero(self):
        assert default_row_scorer(_row(model="m1", response="x")) == 0.0

    def test_none_response_zero(self):
        assert default_row_scorer(_row(model="m1", response=None)) == 0.0


class TestComputeRanking:
    async def _seed(self, store: InMemoryStorage, *rows: RunRow) -> None:
        await store.initialize()
        for r in rows:
            await store.record_call(r)

    async def test_winner_picked(self):
        store = InMemoryStorage()
        # Two models on enrich; both succeed → tie. Cheaper wins.
        await self._seed(
            store,
            _row(model="cheap/m1", cost=0.001, eff_cost=0.001, response="A" * 50),
            _row(model="exp/m2", cost=0.010, eff_cost=0.010, response="B" * 50),
        )
        report = await compute_ranking(store, ExperimentTag("run_x"))
        assert "enrich" in report.stages
        sr = report.stages["enrich"]
        assert sr.model_scores["cheap/m1"] == pytest.approx(1.0)
        assert sr.model_scores["exp/m2"] == pytest.approx(1.0)
        assert sr.winner == "cheap/m1"  # cheaper wins on tie

    async def test_higher_score_wins(self):
        store = InMemoryStorage()
        await self._seed(
            store,
            # Two attempts on m1 — one succeeds, one fails. Score = 0.5
            _row(model="m1", response="A" * 50, cost=0.001),
            _row(model="m1", response=None, error_class="EmptyContent", cost=0.001),
            # Two attempts on m2 — both succeed. Score = 1.0
            _row(model="m2", response="B" * 50, cost=0.005),
            _row(model="m2", response="C" * 50, cost=0.005),
        )
        report = await compute_ranking(store, ExperimentTag("run_x"))
        sr = report.stages["enrich"]
        assert sr.model_scores["m1"] == pytest.approx(0.5)
        assert sr.model_scores["m2"] == pytest.approx(1.0)
        assert sr.winner == "m2"  # higher score wins despite higher cost

    async def test_per_model_summary_flattens(self):
        store = InMemoryStorage()
        await self._seed(
            store,
            _row(model="m1", stage="enrich", response="A" * 50),
            _row(model="m1", stage="translate_es", response="B" * 50),
            _row(model="m2", stage="enrich", response="C" * 50, error_class="ContextOverflow"),
        )
        report = await compute_ranking(store, ExperimentTag("run_x"))
        assert report.per_model_summary["m1"]["enrich"] == 1.0
        assert report.per_model_summary["m1"]["translate_es"] == 1.0
        assert report.per_model_summary["m2"]["enrich"] == 0.0

    async def test_token_averages_populated(self):
        store = InMemoryStorage()
        # Distinct responses → distinct composite_hash → both rows persist
        # (record_call is idempotent on the PK, so identical rows would
        # collapse into one).
        await self._seed(
            store,
            _row(model="m1", response="A" * 30 + "first", in_tok=100, out_tok=50, rsn_tok=10),
            _row(model="m1", response="B" * 30 + "second", in_tok=300, out_tok=150, rsn_tok=20),
        )
        report = await compute_ranking(store, ExperimentTag("run_x"))
        sr = report.stages["enrich"]
        assert sr.model_avg_input_tokens["m1"] == pytest.approx(200.0)
        assert sr.model_avg_output_tokens["m1"] == pytest.approx(100.0)
        assert sr.model_avg_reasoning_tokens["m1"] == pytest.approx(15.0)

    async def test_gold_checker_blends(self):
        # Custom GoldChecker that gives m1 = 0.5, m2 = 1.0 on every row.
        class _GC:
            async def score(self, *, task_unit, stage_id, response):
                if "m1" in (response or ""):
                    return 0.5
                return 1.0

        store = InMemoryStorage()
        await self._seed(
            store,
            _row(model="m1", response="m1_payload" + "x" * 50),
            _row(model="m2", response="m2_payload" + "x" * 50),
        )
        report = await compute_ranking(
            store, ExperimentTag("run_x"),
            gold_checker=_GC(), gold_blend_weight=0.3,
        )
        sr = report.stages["enrich"]
        # m1: 0.7*1.0 + 0.3*0.5 = 0.85
        # m2: 0.7*1.0 + 0.3*1.0 = 1.0
        assert sr.model_scores["m1"] == pytest.approx(0.85)
        assert sr.model_scores["m2"] == pytest.approx(1.0)
        assert ("m1", "enrich") in report.gold_accuracy
        _n, acc = report.gold_accuracy[("m1", "enrich")]
        assert acc == pytest.approx(0.5)


class TestClampScore:
    def test_in_range_value_unchanged(self):
        assert _clamp_score(0.5, source="RowScorer", model="m1", stage="s") == 0.5

    def test_nan_clamped_to_zero(self):
        assert _clamp_score(float("nan"), source="RowScorer", model="m1", stage="s") == 0.0

    def test_positive_infinity_clamped_to_one(self):
        assert _clamp_score(math.inf, source="RowScorer", model="m1", stage="s") == 1.0

    def test_negative_infinity_clamped_to_zero(self):
        assert _clamp_score(-math.inf, source="RowScorer", model="m1", stage="s") == 0.0

    def test_above_one_clamped(self):
        assert _clamp_score(1.5, source="GoldChecker", model="m1", stage="s") == 1.0

    def test_below_zero_clamped(self):
        assert _clamp_score(-0.3, source="GoldChecker", model="m1", stage="s") == 0.0

    def test_out_of_range_logs_warning(self, caplog):
        with caplog.at_level("WARNING"):
            _clamp_score(2.0, source="RowScorer", model="bad_model", stage="enrich")
        assert any("bad_model" in r.message for r in caplog.records)


class TestComputeRankingClampsScorerOutput:
    async def _seed(self, store: InMemoryStorage, *rows: RunRow) -> None:
        await store.initialize()
        for r in rows:
            await store.record_call(r)

    async def test_buggy_row_scorer_nan_does_not_corrupt_ranking(self):
        def buggy_scorer(row: RunRow) -> float:
            return float("nan") if row.model == "buggy" else 0.5

        store = InMemoryStorage()
        await self._seed(
            store,
            _row(model="buggy", response="A" * 50),
            _row(model="fine", response="B" * 50),
        )
        report = await compute_ranking(store, ExperimentTag("run_x"), row_scorer=buggy_scorer)
        sr = report.stages["enrich"]
        assert sr.model_scores["buggy"] == pytest.approx(0.0)  # nan clamped, not propagated
        assert sr.model_scores["fine"] == pytest.approx(0.5)
        assert not math.isnan(sr.model_scores["buggy"])

    async def test_buggy_gold_checker_out_of_range_does_not_corrupt_ranking(self):
        class _BuggyGC:
            async def score(self, *, task_unit, stage_id, response):
                return 3.7  # way outside [0, 1]

        store = InMemoryStorage()
        await self._seed(store, _row(model="m1", response="A" * 50))
        report = await compute_ranking(
            store, ExperimentTag("run_x"),
            gold_checker=_BuggyGC(), gold_blend_weight=0.3,
        )
        sr = report.stages["enrich"]
        assert sr.model_scores["m1"] <= 1.0  # blended result never exceeds [0, 1]
