"""LLMProvider Protocol — structural contract for ``provider_factory(model_id)``.

Every other extension point in the framework (``TaskPool``, ``PromptBuilder``
/ ``ResponseParser`` / ``GoldChecker``, ``ModelCatalogue``, ``BenchmarkStorage``,
``RowScorer``) is a formal ``@runtime_checkable Protocol``. The provider
contract used to exist only as free-text in ``round_runner.py``'s
docstrings and a bare ``getattr(provider, attr, None)`` loop — this
module gives it the same standing as the rest (audit: 01-Medium).

Only ``generate()`` is REQUIRED. Everything else is a best-effort
telemetry attribute the runner reads via ``getattr(provider, attr,
None)`` after each call; a provider that doesn't set a given attribute
just leaves the corresponding ``RunRow`` field empty — no error, no
Protocol violation. Declaring these as regular (non-Optional) Protocol
members would make ``isinstance(provider, LLMProvider)`` require ALL of
them to be set at all times, which is stricter than the runner actually
enforces — so they're documented here rather than declared as members.

Recognised optional telemetry attributes (all read via ``getattr(...,
None)``, mirroring pyutilz's Phase-4 OpenRouter fields):
    last_input_tokens, last_output_tokens, last_reasoning_tokens,
    last_cost_usd, last_effective_cost_usd, last_cache_hit_tokens,
    last_cache_write_tokens, last_cache_discount_usd, last_is_byok,
    last_web_search_citations, last_upstream_resolved_model,
    last_upstream_provider, last_upstream_model,
    last_native_finish_reason, last_generation_id
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Structural contract for the object ``RoundConfig.provider_factory(model_id)``
    returns. Default factory: ``pyutilz.llm.get_llm_provider("openrouter", model=...)``.
    """

    async def generate(
        self, *, prompt: str, system: str, max_tokens: int | None = None,
    ) -> str:
        """Issue one LLM call and return the response text.

        Telemetry from the call (tokens, cost, provenance) is exposed
        via the ``last_*`` instance attributes documented on this
        module, not via the return value — the runner reads them off
        ``self`` immediately after ``await``ing this coroutine.
        """
        ...
