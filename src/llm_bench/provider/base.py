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

One further optional attribute, read the same way and for a different purpose:
    max_output_tokens - the largest output this provider will produce for the current model. The runner
    reads it so that a call the consumer left uncapped carries an EXPLICIT ceiling instead of the implicit
    0 that means "use my maximum". The number is usually the same either way; what changes is that it
    becomes a stated value in the request rather than an absent field whose consequence - an unbounded cost
    and an unbounded wall-clock, which on a fleet run is the difference between one slow arm and a round
    that never ends - is discovered from the invoice. A provider that does not set it keeps the old
    behaviour and the runner says so once.

    Documented here rather than declared as a Protocol member for the reason given above: `runtime_checkable`
    makes every declared member mandatory for `isinstance`, which is stricter than the runner enforces. That
    was tried and reverted - it broke `test_class_with_generate_satisfies_protocol`, whose whole point is
    that `generate()` alone is enough.
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
