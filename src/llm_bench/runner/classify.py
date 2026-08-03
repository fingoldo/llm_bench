"""Provider-error classifier — turn a vague Exception into a stable label.

The output strings drive ``DEAD_ERROR_CLASSES`` membership and the
post-hoc analysis that groups failures by cause. Same labels VocabApp's
runner uses, so the resume cache / dead-model gate stay compatible
across the cutover.
"""

from __future__ import annotations


def classify_provider_error(exc_name: str, message: str | None) -> str:
    """Map an exception type + message into the canonical label.

    Order matters: ``ModelNotFound`` runs FIRST so 405s with
    "Provider returned error" wording don't get mis-classified as
    ``ProviderError`` (lesson from the live run on 2026-05-05).

    ``message`` is ``str | None`` rather than a bare ``str``: both real
    call sites always pass ``str(exc)`` (never ``None``) today, but the
    body's own ``(message or "")`` guard already tolerates ``None`` —
    the type hint was simply narrower than the actually-supported (and
    tested, see ``test_none_message_falls_back_to_exception_name``)
    contract.
    """
    msg = (message or "").lower()
    if (
        "api error 404" in msg
        or "api error 405" in msg
        or "api error 410" in msg
        or "method not allowed" in msg
        or "no endpoints found" in msg
        or " not found" in msg
    ):
        return "ModelNotFound"
    if "maximum context length" in msg or "context length is" in msg:
        return "ContextOverflow"
    if "returned no choices" in msg or "empty choices" in msg:
        return "EmptyChoices"
    if "json" in msg and ("not support" in msg or "unsupported" in msg or "response_format" in msg):
        return "JsonModeUnsupported"
    if "429" in msg or "rate limit" in msg or "too many requests" in msg or ("quota" in msg and ("exceed" in msg or "exhaust" in msg)):
        return "RateLimited"
    if "provider returned error" in msg:
        return "ProviderError"
    return exc_name
