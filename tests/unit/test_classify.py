"""classify_provider_error unit tests — table-driven, all 6 branches.

Previously had ZERO test coverage anywhere in the repository (audit:
05/09-High) despite the function's own docstring documenting a real
historical mis-classification incident ("lesson from the live run on
2026-05-05") — i.e. a regression here has already happened once in
production. Each branch gets a positive case; the priority-order test
is a direct regression test for that incident.
"""

from __future__ import annotations

import pytest

from llm_bench import classify_provider_error


class TestEachBranch:
    @pytest.mark.parametrize(
        "message",
        [
            "OpenRouter API error 404: No endpoints found for x/y.",
            "OpenRouter API error 405: Method Not Allowed",
            "OpenRouter API error 410: Gone",
            "upstream returned method not allowed",
            "no endpoints found for this model",
            "model x/y not found",
        ],
    )
    def test_model_not_found(self, message):
        assert classify_provider_error("HTTPStatusError", message) == "ModelNotFound"

    @pytest.mark.parametrize(
        "message",
        [
            "This model's maximum context length is 128000 tokens.",
            "context length is 8192 and you requested 20000",
        ],
    )
    def test_context_overflow(self, message):
        assert classify_provider_error("BadRequestError", message) == "ContextOverflow"

    @pytest.mark.parametrize(
        "message",
        [
            "The model returned no choices in the response.",
            "empty choices array from upstream",
        ],
    )
    def test_empty_choices(self, message):
        assert classify_provider_error("ValueError", message) == "EmptyChoices"

    @pytest.mark.parametrize(
        "message",
        [
            "response_format json_object is not supported for this model",
            "this model does not support json mode",
            "json response_format is unsupported for this model",
        ],
    )
    def test_json_mode_unsupported(self, message):
        assert classify_provider_error("BadRequestError", message) == "JsonModeUnsupported"

    @pytest.mark.parametrize(
        "message",
        [
            "429 Too Many Requests",
            "rate limit exceeded, please retry later",
            "too many requests, slow down",
            "quota exceeded for this billing period",
            "monthly quota exhausted",
        ],
    )
    def test_rate_limited(self, message):
        assert classify_provider_error("RateLimitError", message) == "RateLimited"

    def test_provider_returned_error_fallback(self):
        assert classify_provider_error("RuntimeError", "provider returned error: internal issue") == "ProviderError"

    def test_unrecognised_message_falls_back_to_exception_name(self):
        assert classify_provider_error("ConnectionResetError", "totally unrecognised text") == "ConnectionResetError"

    def test_empty_message_falls_back_to_exception_name(self):
        assert classify_provider_error("TimeoutError", "") == "TimeoutError"

    def test_none_message_falls_back_to_exception_name(self):
        assert classify_provider_error("TimeoutError", None) == "TimeoutError"


class TestBranchPriorityOrder:
    """Direct regression test for the documented 2026-05-05 incident:
    ModelNotFound must be checked BEFORE ProviderError, so a 405 whose
    message ALSO happens to contain "provider returned error" wording
    still classifies as ModelNotFound, not ProviderError."""

    def test_405_with_provider_returned_error_wording_is_model_not_found(self):
        msg = "OpenRouter API error 405: Method Not Allowed - provider returned error upstream"
        assert classify_provider_error("HTTPStatusError", msg) == "ModelNotFound"

    def test_message_is_case_insensitive(self):
        assert classify_provider_error("HTTPStatusError", "API ERROR 404: NOT FOUND") == "ModelNotFound"
        assert classify_provider_error("RateLimitError", "RATE LIMIT EXCEEDED") == "RateLimited"
