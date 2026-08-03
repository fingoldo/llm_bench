"""Before/after benchmark for the ``mad_bootstrap_prune`` O(n^2 * n_bootstrap)
-> O(n * n_bootstrap) fix (audit: 06-High #2).

The "before" functions below are a faithful reconstruction of the
pre-fix code: both the paired and unpaired bootstrap branches
regenerated (and, for the unpaired branch, re-sorted) the SAME
resample distribution once PER CANDIDATE instead of once per
bootstrap run, even though the resample draw/peer-aggregation for a
given iteration does not depend on which candidate is currently being
scored. The current `mad_bootstrap_prune` (imported from the package)
hoists that generation outside the per-candidate loop.

Run:
    python benchmarks/bench_mad_bootstrap_prune.py
"""

from __future__ import annotations

import random
import statistics
import time

from llm_bench import mad_bootstrap_prune

N_BOOTSTRAP = 1000


# ──────────────────────────────────────────────────────────────────────
# Pre-fix reconstructions (O(n^2 * n_bootstrap))
# ──────────────────────────────────────────────────────────────────────

def _old_unpaired(scores: dict[str, float], *, rng_seed: int = 0, mad_threshold: float = 2.5) -> tuple[set[str], dict[str, float]]:
    rng = random.Random(rng_seed)
    values = list(scores.values())
    n = len(values)
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    threshold = median - mad_threshold * mad

    survivors: set[str] = set()
    lower_ci: dict[str, float] = {}
    for model, v in scores.items():
        # Regenerated PER CANDIDATE — this is the O(n) redundant part.
        resample_medians = [statistics.median(rng.choice(values) for _ in range(n)) for _ in range(N_BOOTSTRAP)]
        deviations = sorted(v - m for m in resample_medians)
        lb = median + deviations[int(0.025 * N_BOOTSTRAP)]
        lower_ci[model] = lb
        if lb >= threshold:
            survivors.add(model)
    return survivors, lower_ci


def _old_paired(scores: dict[str, float], per_task_unit_scores: dict[str, list[float]], *, rng_seed: int = 0, mad_threshold: float = 2.5) -> tuple[set[str], dict[str, float]]:
    rng = random.Random(rng_seed)
    values = list(scores.values())
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    threshold = median - mad_threshold * mad
    model_names = list(scores)
    unit_count = min(len(per_task_unit_scores[m]) for m in model_names)

    survivors: set[str] = set()
    lower_ci: dict[str, float] = {}
    for model in model_names:
        # Full peer-aggregation recomputed PER CANDIDATE, per iteration.
        deviations = []
        for _ in range(N_BOOTSTRAP):
            idx = [rng.randrange(unit_count) for _ in range(unit_count)]
            means = {m: statistics.fmean(per_task_unit_scores[m][i] for i in idx) for m in model_names}
            iter_median = statistics.median(means.values())
            deviations.append(means[model] - iter_median)
        deviations.sort()
        lb = median + deviations[int(0.025 * N_BOOTSTRAP)]
        lower_ci[model] = lb
        if lb >= threshold:
            survivors.add(model)
    return survivors, lower_ci


# ──────────────────────────────────────────────────────────────────────
# Harness
# ──────────────────────────────────────────────────────────────────────

def _synthetic(n: int) -> tuple[dict[str, float], dict[str, list[float]]]:
    rng = random.Random(42)
    per_unit = {f"m{i}": [rng.gauss(0.5, 0.1) for _ in range(8)] for i in range(n)}
    scores = {m: statistics.fmean(v) for m, v in per_unit.items()}
    return scores, per_unit


def _time(fn, *args) -> float:
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


def main() -> None:
    print(f"mad_bootstrap_prune before/after (n_bootstrap={N_BOOTSTRAP})\n")
    print(f"{'n':>5} {'branch':<10} {'before (s)':>12} {'after (s)':>12} {'speedup':>10}")
    for n in (12, 26, 52, 100):
        scores, per_unit = _synthetic(n)

        before_unpaired = _time(_old_unpaired, scores)
        after_unpaired = _time(mad_bootstrap_prune, scores)
        print(f"{n:>5} {'unpaired':<10} {before_unpaired:>12.3f} {after_unpaired:>12.3f} {before_unpaired / after_unpaired:>9.1f}x")

        before_paired = _time(_old_paired, scores, per_unit)
        start = time.perf_counter()
        mad_bootstrap_prune(scores, per_task_unit_scores=per_unit)
        after_paired = time.perf_counter() - start
        print(f"{n:>5} {'paired':<10} {before_paired:>12.3f} {after_paired:>12.3f} {before_paired / after_paired:>9.1f}x")


if __name__ == "__main__":
    main()
