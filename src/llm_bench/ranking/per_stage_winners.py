"""Per-stage M_X winner selection — picks the top scorer per stage.

The central design constraint of multi-round Halving: each stage has
its own "winner" (M_X for stage X). When candidate Y runs stage k+1,
the upstream context for Y's call is M_k's parsed output — NOT Y's
own stage k output. This decouples the chain quality from any one
candidate's stage k mistakes.

Why it matters: if candidate Y is good at stage k+1 but bad at stage
k, using Y's bad-stage-k output as upstream context masks Y's actual
stage k+1 ability. Substituting the per-stage winner's output puts
every candidate on the same upstream footing.

Persistence (``persist_winners`` / ``load_winners``) goes through
``BenchmarkStorage`` — no JSON-on-disk fallback in the framework
(that was VocabApp-specific; storage backends own their own
serialisation).
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import TYPE_CHECKING

from llm_bench.core.types import ExperimentTag, RunRow, WinnerSet

if TYPE_CHECKING:
    from llm_bench.ranking.ranker import RankingReport
    from llm_bench.storage.base import BenchmarkStorage

logger = logging.getLogger(__name__)


def select_per_stage_winners(
    report: "RankingReport",
    *,
    experiment_tag: ExperimentTag,
    round_idx: int,
    candidates_out: list[str] | None = None,
) -> WinnerSet:
    """Pick the top-scoring candidate per stage from a ``RankingReport``.

    ``StageRanking.winner`` is populated by ``compute_ranking`` via
    ``cost_tiebreak_key`` (score desc, then cheapest cost, then fastest
    latency) — this function REUSES that exact winner rather than
    resorting ``model_scores`` by score alone. The earlier version re-
    sorted independently and picked ``sorted_models[0]``, which ignores
    cost/latency entirely; on a score tie (the NORM, not an edge case,
    since ``default_row_scorer`` only ever returns 0.0 or 1.0) the two
    functions could name different winners for the identical stage of
    the identical round — Python's stable sort then broke the tie by
    dict-insertion order (row-iteration order, which the storage
    Protocol explicitly declares "unspecified") instead of by cost
    (audit: 03-High, reproduced).

    Stages where every candidate scored ``0`` (no signal — e.g. nobody
    parsed cleanly) are skipped — no winner means the next round's
    upstream substrate falls back to the candidate's own output.
    """
    winners: dict[str, str] = {}
    runner_up: dict[str, str | None] = {}
    stage_scores: dict[str, float] = {}

    for stage, sr in report.stages.items():
        if not sr.model_scores or sr.winner is None:
            continue
        top_model = sr.winner
        top_score = sr.model_scores[top_model]
        if top_score <= 0.0:
            # No signal at all — skip rather than enshrine garbage.
            continue
        winners[stage] = top_model
        stage_scores[stage] = float(top_score)
        # Runner-up: highest-scoring model other than the winner, using
        # the SAME cost_tiebreak_key ordering compute_ranking used to
        # pick the winner in the first place (not a bare score sort),
        # so the Phase 16 confirmation gate compares the winner against
        # the model that was genuinely second, not just second-by-score.
        from llm_bench.core.scoring import cost_tiebreak_key
        others = [m for m in sr.model_scores if m != top_model]
        if others:
            others.sort(
                key=lambda m: cost_tiebreak_key(
                    m, sr.model_scores[m],
                    eff_cost=sr.model_eff_cost,
                    mean_latency_sec=sr.model_avg_latency_sec,
                ),
            )
            runner_up[stage] = others[0]
        else:
            runner_up[stage] = None

    return WinnerSet(
        experiment_tag=experiment_tag,
        round_idx=round_idx,
        winners=winners,
        candidates_out=list(candidates_out or []),
        stage_scores=stage_scores,
        runner_up=runner_up,
    )


def _latest_by_ts(rows: list[RunRow]) -> RunRow:
    """Pick the most-recently-timestamped row; a ``ts=None`` row always
    loses to any timestamped one.

    Uses ``max()`` with an explicit key rather than ``sort(reverse=True)``
    + take-first: the previous ``sort(key=lambda r: (r.ts is None, r.ts),
    reverse=True)`` put ``ts is None`` (i.e. ``True``) FIRST under
    ``reverse=True`` (``True > False``), which is exactly backwards from
    the "``None`` sorts last" the accompanying comment claimed (audit:
    05-Medium, reproduced — latent today only because every backend
    always stamps ``ts`` on write, so a ``None`` row is rare in
    practice; still a live bug for any row inserted outside the normal
    ``record_call`` path, e.g. a fixture/migration/future backend).
    """
    def _sort_key(r: RunRow) -> tuple[bool, datetime]:
        if r.ts is not None:
            return (True, r.ts)
        return (False, datetime.min.replace(tzinfo=UTC))

    return max(rows, key=_sort_key)


async def load_winner_substrate(
    storage: "BenchmarkStorage",
    *,
    winners: WinnerSet,
    stage: str,
    task_unit_id: str,
    experiment_tag: ExperimentTag | None = None,
) -> RunRow | None:
    """Fetch the M_X winner's row for one (stage, task_unit) pair.

    Returns the latest matching row by timestamp, or None when the
    winner row isn't present (fresh experiment / partial round).

    The round driver invokes this BEFORE building any candidate's
    prompt for stage k+1; the returned ``response`` is parsed once
    and the parsed dict feeds every candidate's ``StageContext``.
    Without this wiring, "M_X promotion" is write-only and stage k+1
    silently uses each candidate's own upstream output.

    Single-pair convenience wrapper kept for tests/ad-hoc callers. The
    round driver itself uses ``load_winner_substrates_for_round`` (one
    query per stage for the whole round) rather than calling this once
    per (candidate, task_unit) — this function's own scan-then-filter
    (``query_rows(stage=...)`` streams every row of the stage, THEN
    filters by model/task_unit client-side) would be a textbook N+1 if
    called from that hot loop (audit: 06-Info, called out proactively so
    fixing the Critical wiring bug elsewhere didn't quietly reintroduce
    a perf regression).
    """
    winner_model = winners.get(stage)
    if winner_model is None:
        return None
    target_tag = experiment_tag if experiment_tag is not None else ExperimentTag(winners.experiment_tag)

    candidates: list[RunRow] = [
        row async for row in storage.query_rows(experiment_tag=target_tag, stage=stage) if row.model == winner_model and row.task_unit_id == task_unit_id
    ]
    if not candidates:
        return None
    return _latest_by_ts(candidates)


async def load_winner_substrates_for_round(
    storage: "BenchmarkStorage",
    *,
    winners: WinnerSet,
    stages: "set[str] | frozenset[str]",
    experiment_tag: ExperimentTag | None = None,
) -> dict[tuple[str, str], RunRow]:
    """Batched form of ``load_winner_substrate``: one ``query_rows`` call
    PER STAGE (not per candidate/task_unit), building
    ``{(stage, task_unit_id): winner_row}`` for every task unit the
    winner has a row for.

    This is what ``round_runner.py`` actually calls, once per round —
    every candidate pipeline in the round then does an O(1) dict lookup
    instead of its own storage round-trip, which is what keeps wiring
    the M_X-substrate feature from becoming an N+1 (see
    ``load_winner_substrate``'s docstring for the shape of that trap).
    """
    target_tag = experiment_tag if experiment_tag is not None else ExperimentTag(winners.experiment_tag)
    out: dict[tuple[str, str], RunRow] = {}
    for stage in stages:
        winner_model = winners.get(stage)
        if winner_model is None:
            continue
        by_task_unit: dict[str, list[RunRow]] = {}
        async for row in storage.query_rows(experiment_tag=target_tag, stage=stage):
            if row.model != winner_model:
                continue
            by_task_unit.setdefault(row.task_unit_id, []).append(row)
        for task_unit_id, rows in by_task_unit.items():
            out[(stage, task_unit_id)] = _latest_by_ts(rows)
    return out
