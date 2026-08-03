"""Validator-pair selection — Layer-1 cross-family independence.

When the framework runs validator-style stages (``Stage.is_validator``),
self-judgment is forbidden: model M cannot validate its own output.
This module owns the validator-pair selection logic with two
independence layers:

  - **Layer 1 (HARD)**: validator's model_id != producer's model_id.
    No exceptions.
  - **Layer 2 (SOFT)**: prefer a validator from a DIFFERENT model
    family than the producer. Same-family validation is more likely
    to share training-data biases. Tiebreaker only — when no
    cross-family validator is available, same-family still goes
    through.

At small candidate counts independence weakens:

  - N == 1: no validation possible (return [])
  - N == 2: mutual A<->B blame — independence broken (return []
    + WARNING; caller should skip validator stages)
  - N == 3: cyclic A->B->C->A — weak independence (INFO warning;
    bias can still cycle)
  - N >= 4: full random non-self / non-symmetric assignment
"""

from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)


# Vendor prefix -> family name. Two models share a "family" when
# they're built/trained by the same parent vendor, so a same-family
# validator is more likely to share training-data biases with the
# producer.
_VENDOR_TO_FAMILY: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "x-ai": "x-ai",
    "deepseek": "deepseek",
    "mistralai": "mistral",
    "cohere": "cohere",
    "perplexity": "perplexity",
    # Open-weight families - many share underlying base-model lineage.
    "meta-llama": "meta",
    "qwen": "qwen",
    "microsoft": "microsoft",
    "nvidia": "nvidia",
    "amazon": "amazon",
    "01-ai": "01-ai",
    "ai21": "ai21",
    "moonshotai": "moonshot",
    "minimax": "minimax",
    "z-ai": "z-ai",
    "nousresearch": "nous",
    "inflection": "inflection",
    "alpindale": "alpindale",
    "neversleep": "neversleep",
    "thedrummer": "thedrummer",
    "ibm-granite": "ibm",
    "bytedance-seed": "bytedance",
    "bytedance": "bytedance",
    "arcee-ai": "arcee",
    "allenai": "allenai",
    "upstage": "upstage",
    "xiaomi": "xiaomi",
    "tencent": "tencent",
    "baidu": "baidu",
    "rekaai": "reka",
    "stepfun": "stepfun",
    "alibaba": "alibaba",
    "inclusionai": "inclusion",
    "essentialai": "essentialai",
    "liquid": "liquid",
    "cognitivecomputations": "cognitive",
    "gryphe": "gryphe",
    "sao10k": "sao10k",
    "nex-agi": "nex-agi",
}


_unmapped_vendors_logged: set[str] = set()
"""Vendors we've already logged as missing from ``_VENDOR_TO_FAMILY``,
so a long run doesn't repeat the same INFO line on every call."""


def _model_family(model_id: str) -> str:
    """Resolve a model ID to its vendor family for cross-family
    validator preference.

    Falls back to the vendor prefix when the prefix isn't in the map -
    preserves "different prefix = different family" for any vendor we
    haven't classified yet.
    """
    vendor = model_id.split("/", 1)[0] if "/" in model_id else model_id
    family = _VENDOR_TO_FAMILY.get(vendor)
    if family is None:
        # Falls back to the raw vendor prefix as its own family — still
        # correct (different prefix = different family), but flagged
        # (once per vendor) so an operator scanning logs notices the
        # hand-maintained map is missing a newly-appeared OpenRouter
        # vendor (audit: 02-Low).
        if vendor not in _unmapped_vendors_logged:
            _unmapped_vendors_logged.add(vendor)
            logger.info(
                "[_model_family] vendor %r not in _VENDOR_TO_FAMILY - using "
                "the raw vendor prefix as its family. Consider adding it "
                "to the map if it shares lineage with an existing entry.",
                vendor,
            )
        family = vendor
    return family


def allowed_validator_pairs(
    candidates: list[str],
    *,
    rng_seed: int = 0,
) -> list[tuple[str, str]]:
    """Return ``(producer, validator)`` pairs for validator stages.

    Returns empty list when independence cannot be guaranteed - callers
    should NOT silently substitute the producer as its own validator.
    Skip the validator stage entirely instead.
    """
    n = len(candidates)
    if n < 2:
        return []
    if n == 2:
        logger.warning(
            "[allowed_validator_pairs] N=2 candidates cannot achieve "
            "validator independence (would require mutual A<->B blame). "
            "Returning empty pair list - validator stages should be "
            "skipped at this scale."
        )
        return []
    if n == 3:
        # Cyclic round-robin: A->B, B->C, C->A. Weak but better than self.
        logger.info("[allowed_validator_pairs] N=3 - cyclic round-robin " "(weak independence; bias may cycle).")
        return [(candidates[i], candidates[(i + 1) % n]) for i in range(n)]
    # N >= 4: random non-self, non-symmetric assignment with cross-
    # family preference.
    rng = random.Random(rng_seed)
    pairs: list[tuple[str, str]] = []
    used_validators_for: dict[str, set[str]] = {c: set() for c in candidates}
    used_validators_overall: dict[str, int] = {c: 0 for c in candidates}
    for producer in candidates:
        # Pick a validator that:
        #   1. is not the producer
        #   2. doesn't yet have producer's reverse pair (avoid (A,B)+(B,A))
        eligible = [v for v in candidates if v != producer and producer not in used_validators_for.get(v, set())]
        if not eligible:
            eligible = [v for v in candidates if v != producer]
        producer_family = _model_family(producer)
        # (No pre-shuffle sort here: any ordering set before rng.shuffle
        # is immediately destroyed by it — the real tie-break sort runs
        # after the shuffle, below. A pre-shuffle `.sort()` used to sit
        # here; it was dead code with zero effect on the result, removed
        # per audit finding 02/03/09-Low.)
        rng.shuffle(eligible)
        eligible.sort(
            key=lambda v: (
                used_validators_overall.get(v, 0),
                # 0 if cross-family (preferred), 1 if same-family
                0 if _model_family(v) != producer_family else 1,
            ),
        )
        validator = eligible[0]
        pairs.append((producer, validator))
        used_validators_for.setdefault(producer, set()).add(validator)
        used_validators_overall[validator] += 1
    return pairs


def select_validator_for_producer(
    producer_model: str,
    candidate_pool: list[str],
    *,
    eff_cost: dict[str, float] | None = None,
    rng_seed: int = 0,
) -> str | None:
    """Pick a validator for ``producer_model`` from ``candidate_pool``.

    Layer 1 (HARD): validator != producer (returns None if pool == {producer}).
    Layer 2 (SOFT): prefer cross-family. Within the same eligibility
    class, prefer cheapest by ``eff_cost``.

    Pure deterministic given ``rng_seed`` - the runner uses this on
    every validator-stage call so the routing is reproducible.
    """
    eff_cost = eff_cost or {}
    eligible = [m for m in candidate_pool if m != producer_model]
    if not eligible:
        return None
    producer_family = _model_family(producer_model)
    rng = random.Random(rng_seed)
    rng.shuffle(eligible)
    eligible.sort(
        key=lambda m: (
            # 0 = cross-family (preferred), 1 = same-family
            0 if _model_family(m) != producer_family else 1,
            # Cheaper effective cost wins ties.
            eff_cost.get(m, float("inf")),
            m,
        ),
    )
    return eligible[0]
