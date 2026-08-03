"""CostFilter — declarative cost-rank pool restriction."""

from __future__ import annotations

from dataclasses import dataclass

from llm_bench.cost.catalogue import CatalogueEntry


@dataclass(frozen=True)
class CostFilter:
    """Declarative restriction applied to a ModelCatalogue listing.

    The framework calls ``estimate_top_n_by_cost(filter=CostFilter(...))``
    and gets back the cheapest ``n`` candidates that satisfy these
    constraints. All bounds are optional.

    Defaults (``max_output_price_per_m=2.0``, ``min_context_length=64000``)
    cover the cheap-to-mid pool used by VocabApp; JobApp bumps
    ``max_output_price_per_m`` since cover-letter quality requires
    frontier-tier models.
    """
    max_output_price_per_m: float | None = 2.0
    """USD per 1M output tokens. None disables the cap."""

    max_input_price_per_m: float | None = None

    min_context_length: int | None = 64000
    """Discard models whose context window is below this. Defaults to
    64K (covers VocabApp's worst-case validate_translate_ru ~33K-token
    prompt with reasoning headroom). 0 disables the filter."""

    require_json_mode: bool = True
    """When True, drop models that don't support ``response_format`` /
    ``structured_outputs`` (matches OR's supported_parameters check)."""

    require_healthy: bool = True
    min_uptime: float = 0.95
    """Health gate: best-upstream 30m uptime must clear ``min_uptime``.

    NO-OP for any ``ModelCatalogue`` that leaves ``best_uptime_30m`` as
    ``None`` (any non-OpenRouter catalogue, per ``cost/catalogue.py``'s
    "non-OR sources may leave None" contract) — ``keep()`` below treats
    a missing uptime as "nothing to filter on" and passes every entry
    through regardless of this flag. Set to ``True`` expecting a real
    gate against a catalogue that doesn't populate uptime silently does
    nothing (audit: 09-Low)."""

    moderated_penalty: float = 1.15
    """Multiplier applied to the cost rank of moderated providers when
    ``exclude_moderated=False``. Higher = moderated peers lose ties."""

    exclude_moderated: bool = False
    """Hard-filter out models flagged ``top_provider.is_moderated=True``.
    Use for runs that include vulgar / taboo content where
    safety-blocking is the dominant failure mode."""

    def keep(self, entry: CatalogueEntry) -> bool:
        """Apply the filter to a single catalogue entry. Returns True
        when the entry survives."""
        if self.max_output_price_per_m is not None:
            if entry.output_price_per_m is None:
                return False
            if entry.output_price_per_m > self.max_output_price_per_m:
                return False
        if self.max_input_price_per_m is not None:
            if entry.input_price_per_m is None:
                return False
            if entry.input_price_per_m > self.max_input_price_per_m:
                return False
        if self.min_context_length and self.min_context_length > 0:
            if entry.context_length is None or entry.context_length < self.min_context_length:
                return False
        if self.require_json_mode and not entry.supports_json_mode:
            return False
        if self.require_healthy:
            if entry.best_uptime_30m is None or entry.best_uptime_30m < self.min_uptime:
                # When uptime is unavailable (non-OR catalogue), treat
                # as opaque-pass — we have nothing to filter on.
                if entry.best_uptime_30m is not None:
                    return False
        if self.exclude_moderated and entry.is_moderated:
            return False
        return True
