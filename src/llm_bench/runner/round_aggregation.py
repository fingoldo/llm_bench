"""Post-round aggregation helpers for :func:`llm_bench.runner.round_runner.run_round`.

Carved out of ``round_runner`` to keep it inside the 1000-LOC budget; ``round_runner`` re-exports
every name here, so existing imports from it keep working.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from llm_bench.halving import Halving, RoundResult
from llm_bench.ranking import RankingReport, StageRanking

# Same logger name as before the move, so log filters and caplog assertions on round_runner still match.
logger = logging.getLogger("llm_bench.runner.round_runner")


def _aggregate_stage_metric(
    report: RankingReport, extractor: "Callable[[StageRanking], dict[str, float]]",
) -> dict[str, float]:
    """Sum ``extractor(stage_ranking)`` across all stages per model,
    then average by stage count. Shared shape behind
    aggregate_scores/aggregate_eff_cost/aggregate_latency below — one
    implementation instead of copy-pasting the same sum-then-divide
    loop three times."""
    total: dict[str, float] = {}
    for sr in report.stages.values():
        for m, v in extractor(sr).items():
            total[m] = total.get(m, 0.0) + v
    if report.stages:
        for m in total:
            total[m] /= len(report.stages)
    return total


def _apply_last_round_score_bonus(aggregate_scores: dict[str, float], halving: Halving) -> None:
    """Mixes in the multi-specialty bonus EARNED LAST ROUND (the round
    that just finished, "this round", doesn't have its own bonus yet —
    it gets computed at the end of THIS promote() call, for the round
    after). This is what makes the bonus reach a real promotion
    decision instead of sitting unread in the RoundResult promote()
    returned last time (see Halving.last_score_bonus). Iterates the
    bonus dict, not aggregate_scores itself, so this isn't a mutate-
    while-iterate pattern on the same collection."""
    for m, bonus in halving.last_score_bonus.items():
        if bonus and m in aggregate_scores:
            aggregate_scores[m] = min(1.0, aggregate_scores[m] + bonus)


def _build_per_task_unit_scores(report: RankingReport) -> dict[str, list[float]]:
    """Per-task-unit score breakdown, averaged ACROSS stages per
    (model, task_unit) — activates mad_bootstrap_prune's paired-
    bootstrap variance/cost penalty instead of the plain aggregate
    fallback (audit: 02-High — the penalty existed but nothing fed it
    data). Pre-gold-blend (see StageRanking.model_task_unit_scores'
    docstring for why)."""
    per_task_unit_scores: dict[str, list[float]] = {}
    models = {m for sr in report.stages.values() for m in sr.model_task_unit_scores}
    for m in models:
        unit_scores: dict[str, list[float]] = {}
        for sr in report.stages.values():
            for tu, score in sr.model_task_unit_scores.get(m, {}).items():
                unit_scores.setdefault(tu, []).append(score)
        if unit_scores:
            per_task_unit_scores[m] = [sum(vals) / len(vals) for vals in unit_scores.values()]
    return per_task_unit_scores


def _attach_infra_error_notes(round_result: RoundResult, infra_errors: list[str], round_idx: int) -> None:
    """Surfaces _run_pipeline's persistence failures (storage/resume-
    cache/budget-gate write errors AFTER a paid LLM call) into
    RoundResult.notes so a caller reading the RETURN VALUE (not just
    logs) can detect that this happened, per the project's own
    collect-and-report convention elsewhere in this module."""
    if not infra_errors:
        return
    round_result.notes.append(
        f"{len(infra_errors)} infrastructure failure(s) persisting an "
        f"already-attempted call this round (storage/resume-cache/"
        f"budget-gate write errors AFTER the LLM call itself completed) "
        f"- results for those specific calls may be lost even though "
        f"n_stages_attempted still counted them. See logs for the "
        f"affected (stage, model, task_unit) triples.",
    )
    logger.warning(
        "[round %d] %d infrastructure failure(s) this round - see " "RoundResult.notes and preceding ERROR-level logs",
        round_idx, len(infra_errors),
    )
