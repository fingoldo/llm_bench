"""Sort keys + scoring helpers shared across halving and ranking.

Sits at the bottom of the layer stack so both ``halving/`` and
``ranking/`` can import without cycles.
"""

from __future__ import annotations


def cost_tiebreak_key(
    model: str,
    score: float,
    *,
    eff_cost: dict[str, float],
    higher_score_better: bool = True,
    mean_latency_sec: dict[str, float] | None = None,
) -> tuple[float, float, str]:
    """Single source of truth for the (score, cost, model) sort key.

    Use as a ``sorted(key=...)`` callable to break ties among same-score
    candidates by cheapest effective cost. Models missing from
    ``eff_cost`` get ``+inf`` (worst), so they sort to the end of any
    tie group. Including ``model`` as the third tie-breaker keeps order
    stable across runs.

    Both halving's ``promote()`` and the ranker's winner-pick MUST go
    through this — duplicating the sort lambda diverges over time
    (different fallback behaviour for missing cost, etc.).

    When ``mean_latency_sec`` is supplied, the cost key becomes
    ``eff_cost * latency`` — Pareto-style throughput-aware ranking. Two
    models at the same $/call but one 2x faster: the faster one wins
    ties. Latency is NOT a hard filter; only a multiplier on the cost
    key, so a fast-but-broken model cannot beat a slow-but-correct one
    (the score field guards that). Models missing latency fall back to
    plain eff_cost (treated as latency=1.0).
    """
    sgn = -1.0 if higher_score_better else 1.0
    cost = eff_cost.get(model, float("inf"))
    if mean_latency_sec is not None:
        lat = mean_latency_sec.get(model)
        if lat is not None and lat > 0:
            cost = cost * lat
    return (sgn * score, cost, model)
