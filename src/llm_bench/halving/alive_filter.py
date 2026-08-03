"""Alive/dead filter — drops candidates that fail too often.

The error classifier upstream (in the runner) tags every failed call
with an ``error_class`` string (``ContextOverflow``, ``ModelNotFound``,
``RateLimited``, ...). This module owns the canonical set of those
classes that count toward "this candidate is dead" and the per-class
threshold logic.

Two thresholds:

  - **Permanent classes** (``ContextOverflow``, ``ModelNotFound``,
    ``EmptyChoices``, ``JsonModeUnsupported``, ...) — 3+ in a run
    means the candidate is structurally broken. Default threshold: 3.
  - **Transient classes** (``RateLimited``, ``LLMCallTimeout``, ...) —
    upstream is healthy, just under backpressure. Default threshold:
    6 (= ``timeout_threshold * 2``) so a healthy rate-limited model
    doesn't get falsely marked dead.

``load_dead_model_ids`` reads the storage backend's row history to
identify candidates whose every recorded call returned a non-2xx HTTP
status — those land in a deny-list joined with the per-run
``error_history`` for the next discovery pass.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_bench.core.types import ExperimentTag
    from llm_bench.storage.base import BenchmarkStorage

logger = logging.getLogger(__name__)


# Error class strings that count toward "dead candidate".
# ``ContextOverflow`` is INCLUDED — the runner doesn't currently retry
# on overflow, so 3+ overflows mean the candidate's prompts genuinely
# don't fit. ``ModelNotFound`` (HTTP 404 / 410 / 405 from the upstream's
# /chat/completions) is included since pyutilz's non-retryable status
# set surfaces it on the first call already (no retry storms on dead
# models — see ``pyutilz.llm.openai_compat._NON_RETRYABLE_STATUSES``).
DEAD_ERROR_CLASSES: frozenset[str] = frozenset({
    # Network / transport timeouts.
    "LLMCallTimeout",
    "LLMProviderError",
    "ReadTimeout",
    "ConnectTimeout",
    "PoolTimeout",
    "WriteTimeout",
    "TimeoutError",
    "RateLimitError",
    "APITimeoutError",
    "APIConnectionError",
    "APIError",
    "OpenAIError",
    "InternalServerError",
    "ServiceUnavailableError",
    # Any 4xx/5xx we couldn't classify more specifically.
    "HTTPStatusError",
    # Cause-classified labels emitted by the runner's classifier.
    "EmptyChoices",
    "ProviderError",
    "JsonModeUnsupported",
    "EmptyContent",
    "TruncatedDegenerate",
    "ContextOverflow",
    # HTTP 404 / 405 / 410 from upstream — paired with pyutilz's
    # non-retryable-statuses set so a single hit is enough signal.
    "ModelNotFound",
})


# Overlaps heavily with DEAD_ERROR_CLASSES but is NOT a strict subset —
# ``RateLimited`` lives only here (see test_transient_classes_meaningful
# for why: it's a deliberate signal that the upstream is healthy but
# applying backpressure / throttling, not a broken model). These get a
# higher threshold (= ``timeout_threshold * 2`` by default) so a healthy
# rate-limited model isn't falsely dead-marked.
TRANSIENT_ERROR_CLASSES: frozenset[str] = frozenset({
    "RateLimited",
    "LLMCallTimeout",
    "ReadTimeout",
    "ConnectTimeout",
    "PoolTimeout",
    "WriteTimeout",
    "TimeoutError",
    "APITimeoutError",
    "APIConnectionError",
    "RateLimitError",
})


def is_alive_candidate(
    model: str,
    *,
    error_history: dict[str, list[str]] | None = None,
    timeout_threshold: int = 3,
    transient_threshold: int | None = None,
) -> tuple[bool, str | None]:
    """Decide if a candidate is still alive given its error history.

    ``error_history`` maps model_id to the chronological list of
    ``error_class`` strings recorded so far in this run. Permanent and
    transient classes count separately:

      - permanent classes: dead at >= ``timeout_threshold`` (default 3)
      - transient classes: dead at >= ``transient_threshold``
        (default ``timeout_threshold * 2`` = 6)

    Returns ``(alive, reason_if_dead)``.
    """
    if not error_history or model not in error_history:
        return True, None
    errs = error_history.get(model, [])
    if transient_threshold is None:
        transient_threshold = timeout_threshold * 2

    permanent_count = sum(1 for e in errs if e in DEAD_ERROR_CLASSES and e not in TRANSIENT_ERROR_CLASSES)
    transient_count = sum(1 for e in errs if e in TRANSIENT_ERROR_CLASSES)

    if permanent_count >= timeout_threshold:
        return False, (f"dead: {permanent_count} permanent-class errors in current run " f"(threshold {timeout_threshold})")
    if transient_count >= transient_threshold:
        return False, (
            f"dead: {transient_count} transient-class errors in current run "
            f"(threshold {transient_threshold} - transient-class so this "
            f"is sustained backpressure, not a one-off)"
        )
    return True, None


async def load_dead_model_ids(
    storage: "BenchmarkStorage",
    *,
    min_calls: int = 5,
    experiment_tag: ExperimentTag | None = None,
) -> set[str]:
    """Load model IDs that have proven dead across prior recorded runs.

    A model is "dead" when EVERY recorded row carries a non-empty
    ``error_class`` AND it has at least ``min_calls`` rows (so a one-off
    glitch on a single call doesn't poison the candidate pool).

    Storage-aware version of the original VocabApp function: it reads
    rows via the BenchmarkStorage Protocol instead of opening a direct
    asyncpg connection. Backends that can answer the question via SQL
    (PostgresStorage) MAY override this with a faster query; the default
    iterates rows and aggregates in Python.

    ``experiment_tag=None`` means "look across all tags" — matches the
    cross-tag spirit of the resume cache.

    Returns:
        Set of model IDs to be unioned into the discovery deny-list
        (e.g. ``{"deepseek/deepseek-v4-flash", "stepfun/step-3.5-flash"}``).
    """
    # Aggregate (n_calls, n_failures) per model across all matching rows.
    counts: dict[str, list[int]] = {}  # model -> [n_calls, n_failures]
    if experiment_tag is None:
        # Cross-tag scan: read every row in storage. Implementations
        # that don't expose a "list all tags" API can override this
        # method; the default uses the storage's own iterator.
        try:
            tags = await _all_experiment_tags(storage)
        except Exception as exc:
            logger.warning(
                "[load_dead_model_ids] failed to enumerate tags (%s): %s - " "auto-exclusion disabled for this run",
                type(exc).__name__,
                str(exc)[:200],
            )
            return set()
    else:
        tags = [experiment_tag]

    for tag in tags:
        async for row in storage.query_rows(experiment_tag=tag):
            stats = counts.setdefault(row.model, [0, 0])
            stats[0] += 1
            if row.error_class:
                stats[1] += 1

    return {m for m, (n_calls, n_fail) in counts.items() if n_calls >= min_calls and n_fail == n_calls}


async def _all_experiment_tags(storage: "BenchmarkStorage") -> list[ExperimentTag]:
    """Return every experiment_tag known to ``storage``.

    Default impl reaches through ``query_rows`` over a sentinel "all
    tags" path — but BenchmarkStorage doesn't expose that today. For
    now this is a stub; storage backends that want to support cross-tag
    ``load_dead_model_ids`` should expose ``list_experiment_tags()`` (a
    follow-up enhancement). Returning ``[]`` here makes
    ``load_dead_model_ids`` a no-op for cross-tag scans, mirroring the
    fail-safe of the VocabApp original when the DB query failed.
    """
    return []
