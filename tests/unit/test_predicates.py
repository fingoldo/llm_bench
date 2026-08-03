"""is_row_usable / MIN_USABLE_RESPONSE_LEN unit tests.

Previously had ZERO direct test coverage anywhere in the repository
(surfaced by ``tests/test_meta/test_test_source_parity.py``) despite
consolidating a Critical audit finding: before this predicate existed,
a parse failure or model refusal (HTTP success, non-empty response,
but rejected by the stage's own parser) looked identical to a real
success to every cache-eligibility check in the codebase, got cached
forever, and was re-scored as 1.0 by ``default_row_scorer``. Every
consumer (``storage/{memory,file,postgres}.py``, ``ranking/ranker.py``)
now defers to this single function -- these tests pin its three gates
(``error_class``, ``parse_failure_prefix``, ``min_len``) directly, so a
future edit that reopens the gap fails here first.
"""

from __future__ import annotations

from llm_bench.core.predicates import MIN_USABLE_RESPONSE_LEN, is_row_usable

_LONG_ENOUGH = "x" * MIN_USABLE_RESPONSE_LEN


class TestIsRowUsable:
    def test_usable_row_is_true(self):
        assert is_row_usable(response=_LONG_ENOUGH, error_class=None) is True

    def test_error_class_makes_it_unusable_even_with_a_good_response(self):
        assert is_row_usable(response=_LONG_ENOUGH, error_class="RateLimited") is False

    def test_parse_failure_prefix_makes_it_unusable_despite_success_response(self):
        # The exact shape the Critical finding was about: HTTP success,
        # non-empty response, no error_class -- but the stage's parser
        # rejected it. Without this gate it would look like a real success.
        assert is_row_usable(response=_LONG_ENOUGH, error_class=None, parse_failure_prefix="bad_json") is False

    def test_none_response_is_unusable(self):
        assert is_row_usable(response=None, error_class=None) is False

    def test_response_shorter_than_min_len_is_unusable(self):
        short = "x" * (MIN_USABLE_RESPONSE_LEN - 1)
        assert is_row_usable(response=short, error_class=None) is False

    def test_response_exactly_at_min_len_is_usable(self):
        exact = "x" * MIN_USABLE_RESPONSE_LEN
        assert is_row_usable(response=exact, error_class=None) is True

    def test_empty_string_response_is_unusable(self):
        assert is_row_usable(response="", error_class=None) is False

    def test_custom_min_len_override(self):
        # Caller-supplied min_len (e.g. FileStorage.query_rows(min_response_len=...))
        # overrides the module default, not just a fallback path.
        assert is_row_usable(response="short", error_class=None, min_len=5) is True
        assert is_row_usable(response="shor", error_class=None, min_len=5) is False

    def test_error_class_checked_before_response_length(self):
        # error_class alone is enough to reject, even with no response at all.
        assert is_row_usable(response=None, error_class="ContextOverflow") is False

    def test_empty_string_parse_failure_prefix_does_not_reject(self):
        # An empty string is falsy in Python -- ``if parse_failure_prefix:``
        # must treat "" the same as None (no parse failure recorded), not
        # as a truthy failure marker.
        assert is_row_usable(response=_LONG_ENOUGH, error_class=None, parse_failure_prefix="") is True


class TestMinUsableResponseLenConstant:
    def test_value_is_twenty(self):
        # Pins the actual threshold value -- storage/{memory,file,postgres}.py
        # and ranking/ranker.py all key their defaults off this constant, so
        # an accidental edit here silently shifts cache-eligibility everywhere.
        assert MIN_USABLE_RESPONSE_LEN == 20
