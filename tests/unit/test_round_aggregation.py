"""Post-round aggregation helpers, tested at their own module and through round_runner's re-export."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from llm_bench.runner import round_aggregation as agg
from llm_bench.runner import round_runner


def _report(**stages: dict[str, Any]) -> Any:
    return SimpleNamespace(stages={name: SimpleNamespace(**fields) for name, fields in stages.items()})


def test_round_runner_reexports_the_same_objects() -> None:
    for name in ("_aggregate_stage_metric", "_apply_last_round_score_bonus", "_build_per_task_unit_scores", "_attach_infra_error_notes"):
        assert getattr(round_runner, name) is getattr(agg, name)


class TestAggregateStageMetric:
    def test_sums_per_model_then_divides_by_stage_count(self) -> None:
        report = _report(a={"model_scores": {"m1": 0.2, "m2": 1.0}}, b={"model_scores": {"m1": 0.6}})
        # m2 appears in one stage of two: its mean is over ALL stages, not over the stages it appears in.
        assert agg._aggregate_stage_metric(report, lambda sr: sr.model_scores) == pytest.approx({"m1": 0.4, "m2": 0.5})

    def test_no_stages_gives_empty_result(self) -> None:
        assert agg._aggregate_stage_metric(_report(), lambda sr: sr.model_scores) == {}


class TestApplyLastRoundScoreBonus:
    def test_adds_bonus_caps_at_one_and_ignores_unknown_or_zero(self) -> None:
        scores = {"m1": 0.5, "m2": 0.95, "m3": 0.3}
        halving = SimpleNamespace(last_score_bonus={"m1": 0.1, "m2": 0.1, "m3": 0.0, "absent": 0.4})
        agg._apply_last_round_score_bonus(scores, halving)  # type: ignore[arg-type]
        assert scores == pytest.approx({"m1": 0.6, "m2": 1.0, "m3": 0.3})


class TestBuildPerTaskUnitScores:
    def test_averages_each_task_unit_across_stages(self) -> None:
        report = _report(
            a={"model_task_unit_scores": {"m1": {"t1": 0.0, "t2": 1.0}}},
            b={"model_task_unit_scores": {"m1": {"t1": 1.0}, "m2": {}}},
        )
        out = agg._build_per_task_unit_scores(report)
        assert sorted(out) == ["m1"]  # m2 has no unit scores at all, so it gets no entry
        assert sorted(out["m1"]) == pytest.approx([0.5, 1.0])


class TestAttachInfraErrorNotes:
    def test_no_errors_leaves_notes_untouched(self, caplog: pytest.LogCaptureFixture) -> None:
        rr = SimpleNamespace(notes=[])
        with caplog.at_level(logging.WARNING, logger="llm_bench.runner.round_runner"):
            agg._attach_infra_error_notes(rr, [], 3)  # type: ignore[arg-type]
        assert rr.notes == [] and not caplog.records

    def test_errors_are_counted_into_one_note_and_logged_under_round_runner(self, caplog: pytest.LogCaptureFixture) -> None:
        rr = SimpleNamespace(notes=[])
        with caplog.at_level(logging.WARNING, logger="llm_bench.runner.round_runner"):
            agg._attach_infra_error_notes(rr, ["e1", "e2"], 7)  # type: ignore[arg-type]
        assert len(rr.notes) == 1 and rr.notes[0].startswith("2 infrastructure failure(s)")
        assert [r.name for r in caplog.records] == ["llm_bench.runner.round_runner"]
        assert "[round 7] 2 infrastructure" in caplog.records[0].getMessage()
