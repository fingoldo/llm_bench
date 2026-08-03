"""compute_ranking — per-stage scoring + winner selection.

Streams rows from a ``BenchmarkStorage`` for one ``experiment_tag``,
groups by stage, and produces a ``RankingReport`` with per-(model,
stage) score / cost / latency aggregates plus the per-stage winner
chosen via ``cost_tiebreak_key``.

The framework ranker is INTENTIONALLY minimal — it leaves the
row-level scoring to a pluggable ``RowScorer`` Protocol. The default
scorer treats every successful row (non-error, non-empty response) as
1.0 and every failure as 0.0; that's enough to drive the Halving
cascade based on success-rate. Consumers with richer scoring needs
(VocabApp: TaskScores via richness_accuracy_combo; JobApp: keyword
recall) inject their own ``RowScorer`` that reads ``RunRow.response``
and produces a float.

Optional ``GoldChecker``: when supplied, each row is also scored
against ground truth and the gold accuracy is blended into the
per-stage model score with weight ``gold_blend_weight`` (default 0.3,
matching VocabApp's relative-vs-gold split).
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from collections.abc import Awaitable, Callable

from llm_bench.core.predicates import is_row_usable
from llm_bench.core.scoring import cost_tiebreak_key
from llm_bench.core.types import ExperimentTag, RunRow
from llm_bench.stage.base import GoldChecker
from llm_bench.storage.base import BenchmarkStorage

logger = logging.getLogger(__name__)


def _clamp_score(value: float, *, source: str, model: str, stage: str) -> float:
    """Clamp a RowScorer/GoldChecker return value into ``[0, 1]``.

    Both Protocols document their return value as "a float in [0, 1]",
    but nothing enforced that contract — a buggy plugin returning
    ``nan``/``inf``/a value outside the range used to flow straight
    into ``statistics.fmean``, then into ``cost_tiebreak_key``'s sort
    key. Python happily sorts with ``nan`` present, but the result is
    non-deterministic (``nan < x`` is always False), silently
    scrambling the halving-cut/winner order instead of raising or
    logging anything (audit: 03-Medium — custom RowScorer/GoldChecker
    are the framework's main extension point, so a plugin bug here is
    a realistic failure mode, not a hypothetical one).
    """
    if math.isnan(value):
        logger.warning(
            "[%s] model=%s stage=%s returned nan - clamping to 0.0. "
            "The %s Protocol contract requires a float in [0, 1].",
            source, model, stage, source,
        )
        return 0.0
    clamped = max(0.0, min(1.0, value))
    if clamped != value:
        logger.warning(
            "[%s] model=%s stage=%s returned %r, outside [0, 1] - " "clamping to %r.",
            source, model, stage, value, clamped,
        )
    return clamped


# ──────────────────────────────────────────────────────────────────────
# Row scorer Protocol
# ──────────────────────────────────────────────────────────────────────

@runtime_checkable
class RowScorer(Protocol):
    """Score a single benchmark row.

    Returns a float in ``[0, 1]`` — 0 means "complete failure", 1 means
    "perfect output for this stage". Anything in between is consumer-
    defined (VocabApp: structural correctness + richness; JobApp: keyword
    coverage of the cover letter).

    Async-aware: returning an Awaitable is OK; the ranker awaits it.
    """

    def __call__(self, row: RunRow) -> float | Awaitable[float]: ...


def default_row_scorer(row: RunRow) -> float:
    """Generic fallback scorer: 1.0 on success, 0.0 on error.

    Success = usable per ``core.predicates.is_row_usable`` (no
    error_class, no parse_failure_prefix, response >= the shared
    min-length threshold) — the SAME definition every storage backend
    uses for resume-cache eligibility, so "cacheable" and "scores as
    1.0" can never silently drift apart (audit: 02-High). Previously
    this only checked ``error_class`` + length, so an HTTP-level
    success whose response the stage parser rejected (malformed JSON,
    a plain-text refusal, ...) scored a perfect 1.0 here even after the
    parse-failure fix started recording it with ``parse_failure_prefix``
    set (audit: Critical, HTTP/API report finding #3).
    """
    return 1.0 if is_row_usable(response=row.response, error_class=row.error_class, parse_failure_prefix=row.parse_failure_prefix) else 0.0


# ──────────────────────────────────────────────────────────────────────
# Ranking-report dataclasses
# ──────────────────────────────────────────────────────────────────────

@dataclass
class StageRanking:
    """Per-stage ranking aggregates for one experiment_tag."""
    stage: str
    rows: list[RunRow] = field(default_factory=list)
    model_scores: dict[str, float] = field(default_factory=dict)
    model_cost: dict[str, float] = field(default_factory=dict)
    """Raw per-model spend on this stage (sum of cost_usd)."""
    model_eff_cost: dict[str, float] = field(default_factory=dict)
    """Average effective_cost_usd per call (cost-aware: uses
    cache discounts when present)."""
    model_avg_input_tokens: dict[str, float] = field(default_factory=dict)
    model_avg_output_tokens: dict[str, float] = field(default_factory=dict)
    model_avg_reasoning_tokens: dict[str, float] = field(default_factory=dict)
    model_avg_latency_sec: dict[str, float] = field(default_factory=dict)
    model_task_unit_scores: dict[str, dict[str, float]] = field(default_factory=dict)
    """``{model: {task_unit_id: score}}`` — the same per-row scores that
    get averaged into ``model_scores``, broken out by task unit BEFORE
    averaging (pre-gold-blend: this is the raw RowScorer signal per
    unit, not adjusted by any GoldChecker blend, which only applies to
    the stage-level aggregate). Consumed by round_runner.py to build
    the cross-stage ``per_task_unit_scores`` that activates
    ``mad_bootstrap_prune``'s paired-bootstrap variance/cost penalty
    (audit: 02-High — that penalty existed but was never fed real data,
    so it silently never fired)."""
    winner: str | None = None
    """Highest-scoring model with cost_tiebreak_key applied. None
    when the stage had zero rows."""


@dataclass
class RankingReport:
    """Top-level ranking report — per-stage aggregates + cross-stage
    summaries.

    Consumed by the round driver (to feed Halving.promote) and by the
    CLI ranker-print command.
    """
    experiment_tag: str
    stages: dict[str, StageRanking] = field(default_factory=dict)
    per_model_summary: dict[str, dict[str, float]] = field(default_factory=dict)
    """``{model: {stage: score}}`` — flat view useful for
    ``Halving.promote(scores=...)``."""
    gold_accuracy: dict[tuple[str, str], tuple[int, float]] = field(default_factory=dict)
    """``{(model, stage): (n_rows, accuracy_0_to_1)}`` when a
    GoldChecker was supplied to compute_ranking."""
    n_rows: int = 0


# ──────────────────────────────────────────────────────────────────────
# compute_ranking
# ──────────────────────────────────────────────────────────────────────

async def compute_ranking(
    storage: BenchmarkStorage,
    experiment_tag: ExperimentTag,
    *,
    row_scorer: RowScorer | None = None,
    gold_checker: GoldChecker | None = None,
    gold_blend_weight: float = 0.3,
    task_unit_lookup: Callable[[str], Any] | None = None,
) -> RankingReport:
    """Stream rows for ``experiment_tag``, aggregate per stage, return
    a ``RankingReport``.

    Args:
        storage: backend to stream rows from.
        experiment_tag: filters which rows are considered (cross-tag
            scope is the consumer's responsibility — pass each tag
            and merge if needed).
        row_scorer: maps each ``RunRow`` to a ``[0, 1]`` quality score.
            Defaults to ``default_row_scorer`` (1.0 on success / 0.0
            on error).
        gold_checker: optional ground-truth checker. When supplied,
            each row is scored against gold and the per-(model, stage)
            gold accuracy gets blended into the model score with
            weight ``gold_blend_weight``.
        gold_blend_weight: 0..1, fraction of the score that comes from
            gold (the rest comes from the relative ``row_scorer``).
            Default 0.3 mirrors VocabApp's 70/30 split.
        task_unit_lookup: optional callback ``task_unit_id -> TaskUnit``
            used to enrich gold scoring. The framework doesn't carry
            task-unit metadata in the row directly (only the id), so a
            consumer with stratification / metadata-dependent gold
            checks needs this hook.

    Returns:
        ``RankingReport`` — caller persists / hands to Halving.

    Known perf trade-off (audit: 06-High #1, deliberately NOT fixed in
    this pass): ``round_runner.py`` calls this once per round, over ALL
    rows recorded so far for the tag (not just the new round's rows —
    ``RunRow`` carries no ``round_idx`` to filter on), and
    ``Benchmark.run_phase`` calls it again at the end — so the final
    round's rows get scored twice, and earlier rounds' rows get
    re-streamed + re-scored on every subsequent round. A row-content
    memoization cache was considered, but a row's content isn't
    actually stable within a run: the resume/retry fix elsewhere in
    this framework lets a later successful attempt OVERWRITE an earlier
    failed row at the same PK, so a naive PK-keyed cache would risk
    silently serving a stale pre-overwrite score. A correct fix (an
    explicit ``round_idx`` on ``RunRow`` + incremental per-round
    aggregation, or a content-hash-keyed cache) is a real schema/API
    change, not a drop-in optimization — left for a follow-up pass
    rather than rushed in alongside everything else here.

    Separately (audit: 04-Low): the per-stage scoring loop below scores
    rows strictly sequentially even when ``row_scorer``/``gold_checker``
    are async, rather than batching independent rows through
    ``asyncio.gather`` under a semaphore. Not fixed here — no measured
    bottleneck to justify the added complexity (``file.py``'s own
    docstring cites a ~50K-row scale where this could start to matter;
    batch it then, against real profiling data, not preemptively).
    """
    scorer = row_scorer or default_row_scorer

    # Group rows by stage as we stream.
    by_stage: dict[str, list[RunRow]] = defaultdict(list)
    n_rows = 0
    async for row in storage.query_rows(experiment_tag=experiment_tag):
        by_stage[row.stage].append(row)
        n_rows += 1

    report = RankingReport(experiment_tag=str(experiment_tag), n_rows=n_rows)

    # Per-stage aggregation.
    for stage, rows in by_stage.items():
        sr = StageRanking(stage=stage, rows=rows)

        # Per-(model) accumulators.
        scores_by_model: dict[str, list[float]] = defaultdict(list)
        scores_by_model_unit: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        cost_by_model: dict[str, list[float]] = defaultdict(list)
        eff_cost_by_model: dict[str, list[float]] = defaultdict(list)
        in_tok_by_model: dict[str, list[float]] = defaultdict(list)
        out_tok_by_model: dict[str, list[float]] = defaultdict(list)
        rsn_tok_by_model: dict[str, list[float]] = defaultdict(list)
        lat_by_model: dict[str, list[float]] = defaultdict(list)

        for row in rows:
            score_or_aw = scorer(row)
            if isinstance(score_or_aw, (int, float)):
                row_score = float(score_or_aw)
            else:
                row_score = float(await score_or_aw)
            row_score = _clamp_score(row_score, source="RowScorer", model=row.model, stage=stage)
            scores_by_model[row.model].append(row_score)
            if row.task_unit_id is not None:
                scores_by_model_unit[row.model][row.task_unit_id].append(row_score)
            if row.cost_usd is not None:
                cost_by_model[row.model].append(row.cost_usd)
            if row.effective_cost_usd is not None:
                eff_cost_by_model[row.model].append(row.effective_cost_usd)
            if row.input_tokens is not None:
                in_tok_by_model[row.model].append(row.input_tokens)
            if row.output_tokens is not None:
                out_tok_by_model[row.model].append(row.output_tokens)
            if row.reasoning_tokens is not None:
                rsn_tok_by_model[row.model].append(row.reasoning_tokens)
            if row.mean_attempt_duration_sec is not None:
                lat_by_model[row.model].append(row.mean_attempt_duration_sec)

        # Roll up the relative score (mean per model).
        relative_scores = {m: statistics.fmean(vals) if vals else 0.0 for m, vals in scores_by_model.items()}
        sr.model_task_unit_scores = {m: {tu: statistics.fmean(vals) for tu, vals in unit_map.items()} for m, unit_map in scores_by_model_unit.items()}

        # Optional gold blend.
        if gold_checker is not None:
            gold_scores: dict[str, list[float]] = defaultdict(list)
            for row in rows:
                if row.task_unit_id is None:
                    continue
                # The gold checker takes a TaskUnit; if the consumer
                # didn't supply a lookup, we synthesise a minimal one
                # with just the id (gold checkers that need metadata
                # MUST provide a lookup — silently failing here would
                # mask a misconfiguration).
                if task_unit_lookup is not None:
                    task_unit = task_unit_lookup(row.task_unit_id)
                else:
                    from llm_bench.core.types import TaskUnit
                    task_unit = TaskUnit(
                        id=row.task_unit_id,
                        metadata={"stratum": row.stratum} if row.stratum else {},
                        stratum=row.stratum,
                    )
                gold_score = await gold_checker.score(
                    task_unit=task_unit, stage_id=row.stage,
                    response=row.response,
                )
                gold_score = _clamp_score(float(gold_score), source="GoldChecker", model=row.model, stage=row.stage)
                gold_scores[row.model].append(gold_score)
            for m, gold_vals in gold_scores.items():
                if not gold_vals:
                    continue
                gold_acc = statistics.fmean(gold_vals)
                report.gold_accuracy[(m, stage)] = (
                    len(gold_vals), gold_acc,
                )
                # Blend.
                rel = relative_scores.get(m, 0.0)
                relative_scores[m] = (1.0 - gold_blend_weight) * rel + gold_blend_weight * gold_acc

        sr.model_scores = relative_scores
        sr.model_cost = {m: sum(v) for m, v in cost_by_model.items()}
        sr.model_eff_cost = {m: statistics.fmean(v) for m, v in eff_cost_by_model.items() if v}
        sr.model_avg_input_tokens = {m: statistics.fmean(v) for m, v in in_tok_by_model.items() if v}
        sr.model_avg_output_tokens = {m: statistics.fmean(v) for m, v in out_tok_by_model.items() if v}
        sr.model_avg_reasoning_tokens = {m: statistics.fmean(v) for m, v in rsn_tok_by_model.items() if v}
        sr.model_avg_latency_sec = {m: statistics.fmean(v) for m, v in lat_by_model.items() if v}

        # Winner: cost_tiebreak_key on (score desc, cost asc).
        if relative_scores:
            sorted_models = sorted(
                relative_scores.items(),
                key=lambda kv: cost_tiebreak_key(
                    kv[0], kv[1],
                    eff_cost=sr.model_eff_cost,
                    mean_latency_sec=sr.model_avg_latency_sec,
                ),
            )
            sr.winner = sorted_models[0][0]

        report.stages[stage] = sr

    # Per-model summary: flatten {(model, stage) -> score} to
    # {model: {stage: score}}.
    for stage, sr in report.stages.items():
        for m, s in sr.model_scores.items():
            report.per_model_summary.setdefault(m, {})[stage] = s

    return report
