"""MAD-bootstrap pruner — eliminates statistical outliers below the
``median - threshold * spread`` line.

Used by ``Halving.promote()`` BEFORE the cost-tiebreak halving cut.
Two penalty terms blend into the comparison value:

  - **Variance penalty**: a candidate scoring {0.95, 0.10, 0.95, 0.10}
    averages 0.525 but is far riskier than {0.50, 0.55, 0.52, 0.55}
    at the same mean. When per-task-unit breakdowns are supplied, each
    candidate's comparison value drops by
    ``variance_penalty_factor * coefficient_of_variation`` so spiky
    scorers lose ties to stable peers.

  - **Cost penalty**: an expensive candidate just below threshold is
    pruned more aggressively than a cheap one at the same point. When
    ``eff_cost`` is supplied, each candidate's comparison value drops
    by ``cost_penalty_factor * (cost / cheapest - 1.0)`` so a 2x more
    expensive model needs 2x the headroom over threshold.

Edge cases:

  - n <= 3: MAD-bootstrap statistical power is essentially nil
    (only 2-3 unique resample medians possible). Returns everyone and
    lets the halving cut do the work.
  - 4 <= n <= 9: weak-to-moderate power, only extreme outliers caught.
    Logs an INFO advisory.
  - mad == 0 (constant cluster + outlier): falls back to ``stdev`` as
    the dispersion proxy with a tighter threshold (1.5x rather than
    2.5x) to avoid the outlier-inflates-stdev problem.
"""

from __future__ import annotations

import logging
import math
import random
import statistics

logger = logging.getLogger(__name__)


def mad_bootstrap_prune(
    scores: dict[str, float],
    *,
    n_bootstrap: int = 1000,
    mad_threshold: float = 2.5,
    rng_seed: int = 0,
    per_task_unit_scores: dict[str, list[float]] | None = None,
    eff_cost: dict[str, float] | None = None,
    cost_penalty_factor: float = 0.10,
    variance_penalty_factor: float = 0.20,
) -> tuple[set[str], dict[str, float]]:
    """Removes a candidate when its 95% lower CI is below
    ``median - mad_threshold * spread`` where spread = MAD if MAD > 0
    else stdev.

    Returns ``(survivors_set, lower_ci_per_candidate)``.

    Note: ``per_task_unit_scores`` was named ``per_synset_scores`` in
    the original VocabApp implementation. The math is identical — paired
    bootstrap sampling over the per-task-unit dimension when supplied.
    """
    if not scores:
        return set(), {}
    if len(scores) == 1:
        only = next(iter(scores))
        return {only}, {only: scores[only]}
    if len(scores) <= 3:
        logger.warning(
            "[mad_bootstrap_prune] n=%d candidates: MAD-prune has no " "statistical power below n>=4 - returning all survivors",
            len(scores),
        )
        return set(scores), {m: scores[m] for m in scores}

    if 4 <= len(scores) <= 9:
        logger.info(
            "[mad_bootstrap_prune] n=%d candidates: bootstrap power " "is moderate-to-weak; only extreme outliers will be pruned",
            len(scores),
        )

    rng = random.Random(rng_seed)
    items = list(scores.items())

    # Variance penalty per candidate from per-task-unit scores.
    variance_penalty: dict[str, float] = {}
    if per_task_unit_scores:
        for m, syn_scores in per_task_unit_scores.items():
            if len(syn_scores) < 2:
                variance_penalty[m] = 0.0
                continue
            try:
                m_mean = statistics.fmean(syn_scores)
                m_sd = statistics.pstdev(syn_scores)
            except statistics.StatisticsError:
                variance_penalty[m] = 0.0
                continue
            denom = abs(m_mean) if abs(m_mean) > 1e-9 else 1.0
            cv = m_sd / denom
            variance_penalty[m] = variance_penalty_factor * cv

    # Cost penalty (relative to cheapest).
    cost_penalty: dict[str, float] = {}
    if eff_cost:
        positive = [c for c in eff_cost.values() if c is not None and c > 0]
        if positive:
            cheapest = min(positive)
            for m in scores:
                c = eff_cost.get(m)
                if c is not None and c > 0 and cheapest > 0:
                    ratio = c / cheapest
                    cost_penalty[m] = cost_penalty_factor * (ratio - 1.0)
                else:
                    cost_penalty[m] = 0.0

    def _eff(model: str) -> float:
        return scores[model] - variance_penalty.get(model, 0.0) - cost_penalty.get(model, 0.0)

    eff_items = [(m, _eff(m)) for m, _ in items]
    values = [v for _, v in eff_items]
    median = statistics.median(values)
    abs_dev = [abs(v - median) for v in values]
    mad = statistics.median(abs_dev) if abs_dev else 0.0

    # mad == 0: fall back to stdev with a tighter threshold to avoid
    # the outlier-inflates-stdev problem. Uses math.isclose rather than
    # an exact == 0.0 comparison — ``values`` come from upstream float
    # accumulation (mean-of-mean divisions in round_runner.py), so a
    # "conceptually constant" cluster can land a few ULPs off zero
    # instead of exactly 0.0, which would otherwise skip this
    # more-forgiving fallback and fall through to an absurdly tight
    # threshold built from a near-zero MAD (audit: 03-Medium).
    if math.isclose(mad, 0.0, abs_tol=1e-9):
        try:
            spread = statistics.pstdev(values)
        except statistics.StatisticsError:
            spread = 0.0
        if math.isclose(spread, 0.0, abs_tol=1e-9):
            return set(scores), {m: scores[m] for m in scores}
        constant_threshold_factor = 1.5
        threshold = median - constant_threshold_factor * spread
        survivors_const: set[str] = set()
        lower_ci_const: dict[str, float] = {}
        for model, v_eff in eff_items:
            lower_ci_const[model] = v_eff
            if v_eff >= threshold:
                survivors_const.add(model)
        return survivors_const, lower_ci_const

    spread = mad
    threshold = median - mad_threshold * spread

    # Paired-bootstrap LB per candidate when per_task_unit_scores is
    # available — sample TASK UNITS (paired across all candidates) so
    # each iteration's resample-median uses each candidate's score AT
    # THE SAME unit. Respects Sequential Halving's apples-to-apples
    # assumption (every candidate sees the same pool[:n_per_arm]).
    lower_ci: dict[str, float] = {}
    survivors: set[str] = set()
    n = len(values)
    use_paired = per_task_unit_scores is not None and all(m in per_task_unit_scores and per_task_unit_scores[m] for m in scores)
    if use_paired:
        assert per_task_unit_scores is not None  # narrowed by use_paired above
        unit_count = min(len(per_task_unit_scores[m]) for m in scores)
        if unit_count >= 2:
            penalty_of: dict[str, float] = {m: variance_penalty.get(m, 0.0) + cost_penalty.get(m, 0.0) for m in scores}
            per_unit = {m: per_task_unit_scores[m][:unit_count] for m in scores}
            model_names = [m for m, _ in eff_items]
            # One resample-index draw + one full-peer-set aggregation per
            # bootstrap ITERATION, not per (candidate, iteration) pair.
            # The peer means for a given resample don't depend on which
            # candidate the caller is currently scoring, so the original
            # per-candidate loop redundantly recomputed the same O(n)
            # peer aggregation n times per iteration — O(n^2 * n_bootstrap)
            # total. Hoisting it here drops that to O(n * n_bootstrap),
            # and additionally makes every candidate's deviation for
            # iteration i drawn against the SAME resample (true pairing
            # across candidates, not just within one candidate's own
            # iteration stream) (audit: 06-High #2, measured 986ms @
            # n=52 / 6.4s @ n=100 on the sibling unpaired branch below —
            # same quadratic shape applies here).
            deviations_by_model: dict[str, list[float]] = {m: [] for m in model_names}
            for _ in range(n_bootstrap):
                idx = [rng.randrange(unit_count) for _ in range(unit_count)]
                means_eff = {m: statistics.fmean(per_unit[m][i] for i in idx) - penalty_of[m] for m in model_names}
                iter_median = statistics.median(means_eff.values())
                for m in model_names:
                    deviations_by_model[m].append(means_eff[m] - iter_median)
            for model, _v_eff in eff_items:
                deviations = sorted(deviations_by_model[model])
                lb_deviation = deviations[int(0.025 * n_bootstrap)]
                lb = median + lb_deviation
                lower_ci[model] = lb
                if lb >= threshold:
                    survivors.add(model)
            return survivors, lower_ci

    # Fallback: legacy unpaired bootstrap on aggregated effective scores.
    # The resample (and its median) don't depend on which candidate is
    # being scored — only ``values`` (the pooled scores of ALL
    # candidates) does. The original loop regenerated + re-sorted an
    # identical n_bootstrap-sized resample-median list once per
    # candidate (O(n^2 * n_bootstrap), measured 6.4s @ n=100 candidates
    # — audit: 06-High #2); precomputing it once also means every
    # candidate is compared against the same reference distribution
    # instead of an independently-noisy one per candidate.
    resample_medians = [statistics.median(rng.choice(values) for _ in range(n)) for _ in range(n_bootstrap)]
    for model, v in eff_items:
        fallback_deviations = sorted(v - m for m in resample_medians)
        lb_deviation = fallback_deviations[int(0.025 * n_bootstrap)]
        lb = median + lb_deviation
        lower_ci[model] = lb
        if lb >= threshold:
            survivors.add(model)
    return survivors, lower_ci
