"""Cost-only TOP-N model ranker.

Estimates per-call cost for each catalogue entry assuming PERFECT
quality (no penalty for accuracy / coverage / consensus) — purely
input/output/reasoning-token economics with cache-aware discount.

Useful for:
  - "What's the cheapest possible Phase 1 if every candidate were
    flawless?" budget projection.
  - Sanity-check the discovery filter — are we passing on the
    cheapest-OK options?
  - Pre-flight estimate of a new model's cost before adding to the
    candidate pool.

The framework is provider-agnostic via the ``ModelCatalogue`` Protocol:
glossum / JobApp etc. plug in their preferred catalogue source. The
default (``OpenRouterCatalogue``) wraps pyutilz's
``list_openrouter_models``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from llm_bench.cost.catalogue import CatalogueEntry, ModelCatalogue
from llm_bench.cost.filter import CostFilter

if TYPE_CHECKING:
    pass


# ──────────────────────────────────────────────────────────────────────
# Profile + estimate dataclasses
# ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CostProfile:
    """Prompt-size assumptions used when projecting per-call cost.

    Defaults are tuned from VocabApp's smoke-multi observations:
      - avg system prompt: ~10K tokens (cacheable across calls)
      - avg user prompt: ~1K tokens (per-task variable, never cached)
      - avg output: ~2K tokens
      - avg reasoning (when present): ~1K tokens
      - cache_hit_ratio: 0.7 — system part stays identical across
        calls of the same task; first call cold, subsequent warm.

    Consumers with very different prompt shapes (e.g. JobApp with
    longer system instructions and shorter outputs) should pass an
    overridden profile to ``estimate_top_n_by_cost``.
    """
    system_tokens: int = 10000
    user_tokens: int = 1000
    output_tokens: int = 2000
    reasoning_tokens: int = 1000
    cache_hit_ratio: float = 0.7


@dataclass(frozen=True)
class CostEstimate:
    """Per-model cost projection for one synthetic call.

    Wraps a ``CatalogueEntry`` with the projected cost breakdown so the
    sort + filter logic has all the data it needs in one place.
    """
    entry: CatalogueEntry
    cost_usd: float
    fresh_input_cost: float
    cached_input_cost: float
    output_cost: float
    per_request_fee: float
    has_cache_pricing: bool

    @property
    def model_id(self) -> str:
        return self.entry.model_id

    @property
    def context_length(self) -> int | None:
        return self.entry.context_length

    @property
    def supports_reasoning(self) -> bool:
        return self.entry.supports_reasoning

    @property
    def best_uptime_30m(self) -> float | None:
        return self.entry.best_uptime_30m

    @property
    def best_latency_p50_ms(self) -> float | None:
        return self.entry.best_latency_p50_ms


# ──────────────────────────────────────────────────────────────────────
# Per-call cost computation
# ──────────────────────────────────────────────────────────────────────

def estimate_call_cost(
    *,
    input_price_per_m: float | None,
    output_price_per_m: float | None,
    cache_read_price_per_m: float | None,
    request_fee: float = 0.0,
    profile: CostProfile,
) -> tuple[float, float, float, float, bool] | None:
    """Compute the expected per-call cost given catalogue rates +
    a prompt-size profile.

    Returns ``(total, fresh_input, cached_input, output, has_cache)`` or
    ``None`` when rates are unusable (missing / negative). An exact
    ``0.0`` price is treated as a legitimately free model, not unusable
    — see the inline comment below.

    Cost components:
      1. ``cached_input  = cache_hit_ratio * system_tokens`` × cache rate
         (falls back to full prompt rate when no cache discount).
      2. ``fresh_input   = (1 - cache_hit_ratio) * system_tokens
                          + user_tokens`` × prompt rate. User prompt is
         always fresh (per-call variable).
      3. ``output_cost   = (output_tokens + reasoning_tokens)`` ×
         completion rate. Reasoning is billed as output by every
         provider we care about.
      4. ``per_request_fee`` flat fee, when present.
    """
    if input_price_per_m is None or output_price_per_m is None:
        return None
    # Only negative prices are excluded (meta-router sentinel values like
    # -1000000 — already filtered upstream in cost/openrouter.py's
    # ``_per_token_to_per_m``, this is defence-in-depth for any other
    # ModelCatalogue implementation). An exact 0.0 is a legitimately
    # free model (e.g. a vendor promo) that isn't tagged ``:free`` in
    # the catalogue id — treating it as unusable and dropping it from
    # TOP-N entirely (rather than pricing it as the cheapest option)
    # was a bug, not a filter (audit: 03-Medium).
    if input_price_per_m < 0 or output_price_per_m < 0:
        return None

    # Catalogue stores per-1M; convert to per-token for arithmetic.
    p_prompt = input_price_per_m / 1_000_000.0
    p_completion = output_price_per_m / 1_000_000.0
    if cache_read_price_per_m is not None and cache_read_price_per_m > 0:
        p_cache_read = cache_read_price_per_m / 1_000_000.0
        has_cache_pricing = True
    else:
        p_cache_read = p_prompt  # no discount
        has_cache_pricing = False

    cache_hit_ratio = max(0.0, min(1.0, profile.cache_hit_ratio))
    cached_input_tokens = cache_hit_ratio * profile.system_tokens
    fresh_input_tokens = (1.0 - cache_hit_ratio) * profile.system_tokens + profile.user_tokens
    out_total = profile.output_tokens + max(0, profile.reasoning_tokens)

    cached_input_cost = cached_input_tokens * p_cache_read
    fresh_input_cost = fresh_input_tokens * p_prompt
    output_cost = out_total * p_completion
    total = cached_input_cost + fresh_input_cost + output_cost + request_fee
    return total, fresh_input_cost, cached_input_cost, output_cost, has_cache_pricing


def _provider_of(model_id: str) -> str:
    """Vendor prefix of the model id, e.g. ``openai/gpt-5`` -> ``openai``."""
    return model_id.split("/", 1)[0] if "/" in model_id else model_id


# ──────────────────────────────────────────────────────────────────────
# TOP-N estimator
# ──────────────────────────────────────────────────────────────────────

def estimate_top_n_by_cost(
    catalogue: ModelCatalogue,
    *,
    n: int = 50,
    profile: CostProfile | None = None,
    filter_: CostFilter | None = None,
    include_reasoning_models: bool = True,
    include_topn_best_by_provider: int = 1,
    refresh: bool = False,
    blacklist: Iterable[str] | None = None,
) -> list[CostEstimate]:
    """Return TOP-N cheapest models by expected per-call cost.

    Args:
        catalogue: source of CatalogueEntry rows. Default consumer:
            ``OpenRouterCatalogue()`` from ``llm_bench.cost.openrouter``.
        n: TOTAL output cap. Counts BOTH provider-reserved seats
            (``include_topn_best_by_provider``) AND extras-by-cost.
            Provider seats are added FIRST and are NOT truncated by n.
        profile: prompt-size assumptions for the cost projection.
            Defaults to ``CostProfile()`` which is calibrated for
            VocabApp-shaped pipelines.
        filter_: pricing / context / health constraints. Default
            ``CostFilter()`` — sub-$2/M output, >=64K context,
            JSON-mode required, healthy upstream.
        include_reasoning_models: when False, sets
            ``profile.reasoning_tokens=0`` for the cost projection
            (useful for non-reasoning stages like validate-* / IPA-only).
        include_topn_best_by_provider: reserve K seats for the cheapest
            models from EACH distinct provider regardless of where they
            sit in the global rank. Provider diversity wins: even if
            the top-3 by cost are all from one vendor, the
            second-cheapest of the second-cheapest vendor still
            survives.
        blacklist: model IDs to exclude unconditionally. Useful when a
            recent live run has identified models that 404 / TIMEOUT on
            full-size requests despite passing the catalogue's pre-flight
            ping.
        refresh: forwarded to ``catalogue.list_models(refresh=...)`` —
            when True, bypasses any TTL cache the catalogue implementation
            keeps and forces a fresh fetch (e.g. after a known pricing
            change upstream). Defaults to False (use the cached catalogue).

    Returns ``CostEstimate`` rows sorted by ``cost_usd`` ascending. Each
    estimate carries the underlying ``CatalogueEntry`` so callers can
    re-derive any field (uptime, supports_reasoning, ...).

    This function and ``catalogue.list_models(...)`` are both plain
    (synchronous, likely network-bound) ``def``s in an otherwise
    async-first framework — nothing in ``src/llm_bench`` currently
    calls this from inside a coroutine (``discovery/`` is still an
    empty stub), so it isn't a live event-loop-freeze today. If a
    future ``discovery/`` implementation calls this from async code,
    wrap the call in ``asyncio.to_thread(...)`` rather than awaiting it
    directly (audit: 04-Low).
    """
    profile = profile or CostProfile()
    if not include_reasoning_models:
        profile = replace(profile, reasoning_tokens=0)
    filter_ = filter_ or CostFilter()
    blacklist_set: set[str] = {m for m in (blacklist or []) if m}

    # Stage 1: pull the catalogue + apply the static filter.
    rows = catalogue.list_models(
        refresh=refresh,
        only_healthy=filter_.require_healthy,
        min_uptime=filter_.min_uptime,
    )

    # Stage 2: per-row filter + cost computation.
    estimates: list[CostEstimate] = []
    for entry in rows:
        if not entry.model_id or entry.model_id in blacklist_set:
            continue
        if not filter_.keep(entry):
            continue
        cost_calc = estimate_call_cost(
            input_price_per_m=entry.input_price_per_m,
            output_price_per_m=entry.output_price_per_m,
            cache_read_price_per_m=entry.input_cache_read_price_per_m,
            request_fee=0.0,
            profile=profile,
        )
        if cost_calc is None:
            continue
        total, fresh, cached, output, has_cache = cost_calc
        estimates.append(CostEstimate(
            entry=entry,
            cost_usd=total,
            fresh_input_cost=fresh,
            cached_input_cost=cached,
            output_cost=output,
            per_request_fee=0.0,
            has_cache_pricing=has_cache,
        ))

    # Composite sort key: cheap first, then capability-rich on ties.
    # Moderated providers (safety-filtered, more prone to spurious
    # refusals) rank as if they cost ``moderated_penalty``x more —
    # moderated_penalty > 1.0 makes them lose cost ties to an
    # otherwise-equal unmoderated peer. Entries only reach this sort at
    # all when exclude_moderated=False (True hard-filters them out in
    # filter_.keep() above), so no extra gating is needed here. Ranking
    # cost only — CostEstimate.cost_usd itself stays the true dollar
    # projection so callers see real numbers (audit: 05-Medium — field
    # was declared/documented but never read anywhere).
    def _sort_key(e: CostEstimate) -> tuple[float, int, int, int, float, float]:
        rank_cost = e.cost_usd * filter_.moderated_penalty if e.entry.is_moderated else e.cost_usd
        return (
            rank_cost,
            -(e.entry.context_length or 0),
            -(e.entry.max_completion_tokens or 0),
            -int(e.entry.supports_reasoning),
            -(e.entry.best_uptime_30m or 0.0),
            (e.entry.best_latency_p50_ms if e.entry.best_latency_p50_ms is not None else float("inf")),
        )
    estimates.sort(key=_sort_key)

    # Provider-diversity step: reserve K cheapest seats per provider.
    selected: list[CostEstimate] = []
    seen_ids: set[str] = set()
    if include_topn_best_by_provider > 0 and estimates:
        per_provider: dict[str, list[CostEstimate]] = {}
        for e in estimates:
            per_provider.setdefault(_provider_of(e.model_id), []).append(e)
        for models in per_provider.values():
            for e in models[:include_topn_best_by_provider]:
                if e.model_id not in seen_ids:
                    selected.append(e)
                    seen_ids.add(e.model_id)

    # Top up to ``n`` with the cheapest remaining models. Provider seats
    # are immune to n truncation — if the catalogue has 30 providers and
    # n=20, all 30 provider seats land in the output.
    remaining = max(0, n - len(selected))
    for e in estimates:
        if remaining == 0:
            break
        if e.model_id in seen_ids:
            continue
        selected.append(e)
        seen_ids.add(e.model_id)
        remaining -= 1

    selected.sort(key=_sort_key)
    return selected
