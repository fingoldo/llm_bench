"""Shared row-usability predicate — single source of truth.

``storage/{memory,file,postgres}.py`` and ``ranking/ranker.py`` each
independently re-implemented "is this row usable / cache-eligible" with
a hand-copied ``len(response) >= 20`` threshold (audit finding: 02-High,
8 duplicated call sites). Centralised here so a future threshold change
can't silently drift between the cache-eligibility check and the
default scorer's success check.
"""

from __future__ import annotations

MIN_USABLE_RESPONSE_LEN: int = 20
"""Responses shorter than this are treated as garbage (EmptyContent-like
rows that slipped past ``error_class``) — excluded from resume cache and
scored as failure by ``default_row_scorer``."""


def is_row_usable(
    *,
    response: str | None,
    error_class: str | None,
    parse_failure_prefix: str | None = None,
    min_len: int = MIN_USABLE_RESPONSE_LEN,
) -> bool:
    """True when a row counts as a usable success: no ``error_class``, a
    response of at least ``min_len`` characters, AND (when the caller
    tracks it) no ``parse_failure_prefix``.

    ``parse_failure_prefix`` covers the case ``error_class`` can't: an
    HTTP-level SUCCESS whose response text was non-empty and long
    enough to pass the length check, but that the stage's parser
    rejected (malformed JSON, wrong shape, a plain-text refusal, ...).
    Before this flag existed, such a row looked identical to a real
    success to every cache-eligibility check in the codebase — it got
    cached and reused forever, and re-scored as a 1.0 by
    ``default_row_scorer`` (audit: Critical — parse failures and model
    refusals were silently treated as success and cached permanently).
    """
    if error_class:
        return False
    if parse_failure_prefix:
        return False
    if response is None or len(response) < min_len:
        return False
    return True
