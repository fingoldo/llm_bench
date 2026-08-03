"""OpenRouterCatalogue — concrete ModelCatalogue impl backed by
``pyutilz.llm.list_openrouter_models``.

Default catalogue used by VocabApp + JobApp POC. Pyutilz's
two-stage health pre-flight (``return_only_healthy=True`` +
``min_uptime``) drops models with bad-uptime upstreams AND attaches
per-upstream health info (latency, throughput, true
max_completion_tokens). The OR catalogue's dual ``context_length``
fields (top-level vs. ``top_provider``) are reconciled to the more
pessimistic value.
"""

from __future__ import annotations

import logging
from typing import Any

from llm_bench.cost.catalogue import CatalogueEntry

logger = logging.getLogger(__name__)


def _per_token_to_per_m(value: float | int | str | None) -> float | None:
    """OR catalogue stores prices per-token; convert to per-1M for the
    framework's CatalogueEntry.

    Returns None when the value is missing / negative / unparseable
    (catalogue-meta-routers list ``-1000000`` for unknown — explicitly
    rejected).
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return v * 1_000_000.0


def _normalise_uptime(value: float | int | str | None) -> float | None:
    """OR's live response sometimes returns uptime as 0-100 percentage
    (e.g. 99.806) instead of the 0-1 fraction documented in pyutilz.
    Normalise both forms to a 0-1 fraction.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v > 1.0 else v


def _pick_best_upstream(endpoints: list[dict]) -> dict:
    """Pick the best upstream from a model's endpoint list.

    Selection rule (priority order): live > degraded; higher uptime;
    lower latency; higher throughput. Tie-broken by first-occurring
    endpoint.

    Endpoints list is already filtered by pyutilz's two-stage health
    pre-flight; this picks the BEST one to use as the canonical
    upstream for ranking + max_tokens lookup.
    """
    if not endpoints:
        return {}

    def _key(ep: dict) -> tuple:
        # OR's live API serves status either as legacy string ("live"/
        # "degraded") OR as a numeric code (0=live, negative=down).
        raw_status = ep.get("status")
        if isinstance(raw_status, str):
            status = raw_status.lower()
            status_rank = 0 if status in ("live", "available") else (1 if status == "degraded" else 2)
        elif isinstance(raw_status, (int, float)):
            status_rank = 0 if raw_status >= 0 else 2
        else:
            status_rank = 1  # missing => treat as degraded
        uptime = -(ep.get("uptime_30m") or ep.get("uptime_5m") or 0.0)
        raw_latency = ep.get("latency_p50_ms")
        # `or float("inf")` would treat a genuinely-fast (or reported as
        # exactly 0ms) endpoint as if it had NO latency data at all,
        # sorting the best endpoint last instead of first (default-via-or
        # trap: 0 is a legitimate falsy value here, not "missing").
        latency = float("inf") if raw_latency is None else raw_latency
        throughput = -(ep.get("throughput_p50_tps") or 0.0)
        return (status_rank, uptime, latency, throughput)

    return sorted(endpoints, key=_key)[0]


def _entry_from_or_row(row: dict) -> CatalogueEntry | None:
    """Build a CatalogueEntry from one OR catalogue row.

    Returns None when the row is unusable (no model_id, ``:free`` tier,
    missing pricing). Skips meta-router placeholders that price at
    ``-1000000``.
    """
    mid = row.get("id", "")
    if not mid or ":free" in mid.lower():
        return None
    pricing = row.get("pricing") or {}
    input_p_m = _per_token_to_per_m(pricing.get("prompt"))
    output_p_m = _per_token_to_per_m(pricing.get("completion"))
    cache_read_p_m: float | None = None
    for _k in ("input_cache_read", "cache_read", "cached_input_per_token"):
        v = _per_token_to_per_m(pricing.get(_k))
        if v is not None:
            cache_read_p_m = v
            break

    # Context length: prefer the smaller of catalogue-top-level vs.
    # top_provider (more pessimistic / accurate to upstream).
    ctx_top = row.get("context_length")
    ctx_provider = (row.get("top_provider") or {}).get("context_length")
    ctxs = [c for c in (ctx_top, ctx_provider) if isinstance(c, (int, float)) and c > 0]
    context_length = int(min(ctxs)) if ctxs else None

    # max_completion_tokens: prefer the active upstream's true value
    # (catalogue's top_provider claim is often the meta-router's
    # optimistic max).
    health = row.get("health") or {}
    endpoints = health.get("endpoints") or []
    best_ep = _pick_best_upstream(endpoints)
    upstream_mct = best_ep.get("max_completion_tokens")
    max_completion_tokens: int | None
    if isinstance(upstream_mct, (int, float)) and upstream_mct > 0:
        max_completion_tokens = int(upstream_mct)
    else:
        # Fall back to top_provider.max_completion_tokens.
        tp_mct = (row.get("top_provider") or {}).get("max_completion_tokens")
        max_completion_tokens = int(tp_mct) if isinstance(tp_mct, (int, float)) and tp_mct > 0 else None

    # Capabilities.
    supported = row.get("supported_parameters") or []
    if isinstance(supported, str):
        supported = [supported]
    supported_set = set(supported)
    supports_json = "response_format" in supported_set or "structured_outputs" in supported_set
    supports_reasoning = "reasoning" in supported_set

    is_moderated = bool((row.get("top_provider") or {}).get("is_moderated"))

    return CatalogueEntry(
        model_id=mid,
        input_price_per_m=input_p_m,
        output_price_per_m=output_p_m,
        input_cache_read_price_per_m=cache_read_p_m,
        context_length=context_length,
        max_completion_tokens=max_completion_tokens,
        supports_reasoning=supports_reasoning,
        supports_json_mode=supports_json,
        is_moderated=is_moderated,
        best_uptime_30m=_normalise_uptime(health.get("best_uptime_30m")),
        best_latency_p50_ms=health.get("best_latency_p50_ms"),
        best_throughput_p50_tps=health.get("best_throughput_p50_tps"),
        best_upstream_provider=best_ep.get("provider_name"),
    )


class OpenRouterCatalogue:
    """Concrete ``ModelCatalogue`` (structural, not inherited — matches
    the storage backends' convention of pure duck-typing against their
    own Protocol rather than nominal inheritance, audit: 01-Low)
    backed by ``pyutilz.llm.list_openrouter_models``.

    Pyutilz handles caching (TTL on the catalogue fetch),
    health pre-flight (per-model /endpoints query), and uptime
    normalisation. This wrapper just maps OR-shaped rows to
    framework-shaped ``CatalogueEntry`` objects.

    Optional ``max_output_per_1m_prefilter`` is forwarded to pyutilz
    so Stage 2 health-check doesn't waste 200+ HTTP calls on models
    that the framework's CostFilter would discard anyway.

    ``list_models`` is a plain synchronous (network-bound) call in an
    otherwise async-first framework — see ``estimate_top_n_by_cost``'s
    docstring in ``cost/estimator.py`` for why this isn't a live
    event-loop-freeze today and what a future async caller should do
    about it (``asyncio.to_thread``) (audit: 04-Low).
    """

    def __init__(
        self,
        *,
        sort_by: str = "input_price",
        max_output_per_1m_prefilter: float | None = None,
    ) -> None:
        self._sort_by = sort_by
        self._max_output_per_1m_prefilter = max_output_per_1m_prefilter

    def list_models(
        self,
        *,
        refresh: bool = False,
        only_healthy: bool = True,
        min_uptime: float = 0.95,
    ) -> list[CatalogueEntry]:
        from pyutilz.llm import list_openrouter_models

        kwargs: dict[str, Any] = {
            "refresh": refresh,
            "sort_by": self._sort_by,
            "return_only_healthy": only_healthy,
            "min_uptime": min_uptime,
        }
        if self._max_output_per_1m_prefilter is not None:
            kwargs["max_output_per_1m"] = self._max_output_per_1m_prefilter

        try:
            rows = list_openrouter_models(**kwargs)
        except Exception as exc:
            # pyutilz owns retry/TTL-cache for this fetch; if it still
            # raises (network down, OR API 5xx, malformed catalogue
            # response), surface a clear, catalogue-scoped error instead
            # of letting an opaque exception from a nested dependency
            # abort discovery/cost-estimation with no indication of
            # where it came from (audit: 09-Medium — this repo's own
            # code had zero resilience around the fetch boundary).
            logger.exception(
                "[OpenRouterCatalogue] list_openrouter_models(%s) failed - " "discovery/cost-estimation cannot proceed without a " "catalogue snapshot",
                {k: v for k, v in kwargs.items() if k != "refresh"},
            )
            raise RuntimeError(
                "OpenRouterCatalogue.list_models(): failed to fetch the "
                "OpenRouter model catalogue via pyutilz.llm.list_openrouter_models "
                "(network error, OR API error, or malformed response). See the "
                "chained exception for the underlying cause.",
            ) from exc

        out: list[CatalogueEntry] = []
        for row in rows:
            entry = _entry_from_or_row(row)
            if entry is not None:
                out.append(entry)
        return out
