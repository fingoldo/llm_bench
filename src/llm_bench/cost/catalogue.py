"""ModelCatalogue Protocol — abstracts the source of model pricing/health data.

Decoupled from any single provider so non-OR consumers (Anthropic direct,
OpenAI direct) can plug in their own catalogue. The OR-backed reference
implementation lives in ``cost/openrouter.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CatalogueEntry:
    """One row from a model catalogue.

    Fields mirror what OpenRouter exposes; non-OR catalogues populate
    what they have and leave the rest as ``None``. The framework's
    ``estimate_top_n_by_cost`` consumes this shape exclusively.
    """
    model_id: str
    input_price_per_m: float | None
    output_price_per_m: float | None
    input_cache_read_price_per_m: float | None
    context_length: int | None
    max_completion_tokens: int | None
    supports_reasoning: bool
    supports_json_mode: bool
    is_moderated: bool
    # Health (populated by OR's two-stage pre-flight; non-OR sources may leave None)
    best_uptime_30m: float | None
    best_latency_p50_ms: float | None
    best_throughput_p50_tps: float | None
    best_upstream_provider: str | None


@runtime_checkable
class ModelCatalogue(Protocol):
    """Source of CatalogueEntry rows used for cost-rank discovery."""

    def list_models(
        self, *,
        refresh: bool = False,
        only_healthy: bool = True,
        min_uptime: float = 0.95,
    ) -> list[CatalogueEntry]:
        """Return the full catalogue. ``refresh=True`` bypasses any
        TTL cache. ``only_healthy=True`` drops models whose best-upstream
        30m uptime is below ``min_uptime`` (when health data is available)."""
        ...
