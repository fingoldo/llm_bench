"""Per-stage winner selection + substrate lookup."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, UTC

import pytest

from llm_bench import (
    ExperimentTag,
    InMemoryStorage,
    RankingReport,
    RunRow,
    StageRanking,
    WinnerSet,
    load_winner_substrate,
    select_per_stage_winners,
)
from llm_bench.ranking.per_stage_winners import _latest_by_ts

# ──────────────────────────────────────────────────────────────────────
# select_per_stage_winners now trusts StageRanking.winner (set by
# compute_ranking via cost_tiebreak_key) instead of re-deriving a
# winner from model_scores alone — fixtures use the REAL StageRanking/
# RankingReport dataclasses (not a hand-rolled fake) so this test can't
# silently drift out of sync with what compute_ranking() actually
# produces (audit: 03-High fix — see per_stage_winners.py).
# ──────────────────────────────────────────────────────────────────────


class TestSelectPerStageWinners:
    def test_picks_top_per_stage(self):
        rep = RankingReport(experiment_tag="t", stages={
            "enrich": StageRanking(
                stage="enrich",
                model_scores={"a": 0.9, "b": 0.7, "c": 0.5},
                model_eff_cost={"a": 0.01, "b": 0.01, "c": 0.01},
                winner="a",
            ),
            "translate": StageRanking(
                stage="translate",
                model_scores={"a": 0.4, "b": 0.95, "c": 0.6},
                model_eff_cost={"a": 0.01, "b": 0.01, "c": 0.01},
                winner="b",
            ),
        })
        ws = select_per_stage_winners(
            rep, experiment_tag=ExperimentTag("t"), round_idx=1,
            candidates_out=["a", "b", "c"],
        )
        assert ws.winners["enrich"] == "a"
        assert ws.winners["translate"] == "b"
        assert ws.runner_up["enrich"] == "b"
        assert ws.runner_up["translate"] == "c"
        assert ws.stage_scores["enrich"] == pytest.approx(0.9)
        assert ws.candidates_out == ["a", "b", "c"]

    def test_agrees_with_stage_ranking_winner_on_a_score_tie(self):
        # a and b tie on score; b is cheaper — cost_tiebreak_key (what
        # compute_ranking used to pick StageRanking.winner) must pick b,
        # and select_per_stage_winners must AGREE (that's the bug this
        # fix closes: it used to re-sort by score alone and could name
        # a different winner than StageRanking.winner on exactly this
        # kind of tie).
        rep = RankingReport(experiment_tag="t", stages={
            "enrich": StageRanking(
                stage="enrich",
                model_scores={"pricey": 1.0, "cheap": 1.0},
                model_eff_cost={"pricey": 0.50, "cheap": 0.05},
                winner="cheap",
            ),
        })
        ws = select_per_stage_winners(
            rep, experiment_tag=ExperimentTag("t"), round_idx=1,
        )
        assert ws.winners["enrich"] == "cheap"
        assert ws.runner_up["enrich"] == "pricey"

    def test_skips_zero_signal_stages(self):
        rep = RankingReport(experiment_tag="t", stages={
            "ok": StageRanking(
                stage="ok", model_scores={"a": 0.5, "b": 0.3},
                model_eff_cost={"a": 0.01, "b": 0.01}, winner="a",
            ),
            "garbage": StageRanking(
                stage="garbage", model_scores={"a": 0.0, "b": 0.0},
                model_eff_cost={"a": 0.01, "b": 0.01}, winner="a",
            ),
        })
        ws = select_per_stage_winners(
            rep, experiment_tag=ExperimentTag("t"), round_idx=1,
        )
        assert "ok" in ws.winners
        assert "garbage" not in ws.winners

    def test_no_runner_up_when_only_one_candidate(self):
        rep = RankingReport(experiment_tag="t", stages={
            "solo": StageRanking(
                stage="solo", model_scores={"a": 0.5},
                model_eff_cost={"a": 0.01}, winner="a",
            ),
        })
        ws = select_per_stage_winners(
            rep, experiment_tag=ExperimentTag("t"), round_idx=1,
        )
        assert ws.winners["solo"] == "a"
        assert ws.runner_up["solo"] is None

    def test_no_winner_yields_no_entry(self):
        # StageRanking.winner is None when the stage had zero rows —
        # select_per_stage_winners must not crash or invent one.
        rep = RankingReport(experiment_tag="t", stages={
            "empty": StageRanking(stage="empty"),
        })
        ws = select_per_stage_winners(
            rep, experiment_tag=ExperimentTag("t"), round_idx=1,
        )
        assert "empty" not in ws.winners


# ──────────────────────────────────────────────────────────────────────
# load_winner_substrate against InMemoryStorage
# ──────────────────────────────────────────────────────────────────────

def _row(*, model: str, stage: str, task_unit_id: str, response: str) -> RunRow:
    return RunRow(
        composite_hash=f"h_{model}_{stage}_{task_unit_id}",
        provider="openrouter",
        model=model,
        thinking="",
        stage=stage,
        task_unit_id=task_unit_id,
        stratum="v",
        lang=None,
        parent_prompt_hash=None,
        experiment_tag="run_x",
        response=response,
        error_class=None,
        error_message=None,
        cost_usd=0.001,
        effective_cost_usd=0.001,
        input_tokens=100,
        output_tokens=50,
        reasoning_tokens=0,
        cache_hit_tokens=0,
        cache_write_tokens=0,
    )


class TestLoadWinnerSubstrate:
    async def test_returns_winner_row(self):
        store = InMemoryStorage()
        await store.initialize()
        await store.record_call(_row(
            model="winner_model", stage="enrich",
            task_unit_id="t1", response="winner_resp",
        ))
        await store.record_call(_row(
            model="other_model", stage="enrich",
            task_unit_id="t1", response="loser_resp",
        ))
        ws = WinnerSet(
            experiment_tag="run_x", round_idx=1,
            winners={"enrich": "winner_model"},
        )
        row = await load_winner_substrate(
            store, winners=ws, stage="enrich", task_unit_id="t1",
        )
        assert row is not None
        assert row.response == "winner_resp"

    async def test_returns_none_when_no_winner_for_stage(self):
        store = InMemoryStorage()
        await store.initialize()
        ws = WinnerSet(
            experiment_tag="run_x", round_idx=1, winners={},
        )
        row = await load_winner_substrate(
            store, winners=ws, stage="enrich", task_unit_id="t1",
        )
        assert row is None

    async def test_returns_none_when_winner_row_missing(self):
        store = InMemoryStorage()
        await store.initialize()
        ws = WinnerSet(
            experiment_tag="run_x", round_idx=1,
            winners={"enrich": "winner_model"},
        )
        row = await load_winner_substrate(
            store, winners=ws, stage="enrich", task_unit_id="t1",
        )
        assert row is None

    async def test_multiple_rows_for_winner_picks_latest_by_ts(self):
        # Direct regression test for _latest_by_ts's max()-with-explicit-
        # key fix (audit: 05-Medium): the previous sort(reverse=True)
        # put ts=None FIRST under reverse=True (True > False), exactly
        # backwards from "None sorts last". Two REAL, distinct
        # timestamps here isolate the ordering bug from the None-
        # handling case (covered separately below).
        store = InMemoryStorage()
        await store.initialize()
        older = replace(
            _row(model="winner_model", stage="enrich", task_unit_id="t1", response="older_resp"),
            composite_hash="h_older", ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
        newer = replace(
            _row(model="winner_model", stage="enrich", task_unit_id="t1", response="newer_resp"),
            composite_hash="h_newer", ts=datetime(2026, 6, 1, tzinfo=UTC),
        )
        await store.record_call(older)
        await store.record_call(newer)
        ws = WinnerSet(experiment_tag="run_x", round_idx=1, winners={"enrich": "winner_model"})
        row = await load_winner_substrate(store, winners=ws, stage="enrich", task_unit_id="t1")
        assert row is not None
        assert row.response == "newer_resp"

    def test_ts_none_row_always_loses_to_a_timestamped_row(self):
        # Every real storage backend auto-stamps ts on write when the
        # caller passes ts=None (see record_call), so a ts=None row
        # never actually survives a normal storage round-trip -- the
        # _latest_by_ts docstring itself notes this is "latent today
        # ... still a live bug for any row inserted outside the normal
        # record_call path (fixture/migration/future backend)". Test
        # the helper directly, bypassing storage, to exercise that path.
        undated = replace(
            _row(model="winner_model", stage="enrich", task_unit_id="t1", response="undated_resp"),
            composite_hash="h_undated", ts=None,
        )
        dated = replace(
            _row(model="winner_model", stage="enrich", task_unit_id="t1", response="dated_resp"),
            composite_hash="h_dated", ts=datetime(2020, 1, 1, tzinfo=UTC),  # even an OLD real ts wins
        )
        assert _latest_by_ts([undated, dated]).response == "dated_resp"
        assert _latest_by_ts([dated, undated]).response == "dated_resp"  # order-independent
