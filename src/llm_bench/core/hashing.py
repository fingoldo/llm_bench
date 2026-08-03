"""Composite-hash helpers — single source of truth for prompt fingerprinting.

The framework's resume cache key is ``(composite_hash, provider, model,
thinking)``. ``composite_hash`` is deterministic from prompt text only —
provider/model/thinking are NOT folded in (different routes for the same
prompt are separate cache entries).
"""

from __future__ import annotations

import hashlib


def hash_text(text: str) -> str:
    """SHA256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def composite_hash(system_text: str, user_text: str) -> str:
    """Deterministic hash of the (system, user) prompt pair.

    The system/user split matters here — Anthropic prompt-cache discounts
    apply to the system block, so two prompts with identical concatenated
    text but different splits would NOT cache-hit on the provider side.

    Length-prefixes ``system_text`` rather than joining with a ``\\x00``
    separator: a bare separator byte is ambiguous whenever the prompt
    text itself contains a literal NUL byte (e.g. ``composite_hash("",
    "\\x00")`` and ``composite_hash("\\x00", "")`` both joined to the
    same ``b"\\x00\\x00"``), which would let two genuinely different
    prompt pairs collide onto the same resume-cache key and silently
    serve one prompt's cached response for the other. A fixed-width
    length prefix makes the system/user split point unambiguous
    regardless of byte content.
    """
    sys_bytes = system_text.encode("utf-8")
    usr_bytes = user_text.encode("utf-8")
    return hashlib.sha256(len(sys_bytes).to_bytes(8, "big") + sys_bytes + usr_bytes).hexdigest()


def prompt_hashes(system_text: str, user_text: str) -> tuple[str, str, str]:
    """Return ``(system_hash, user_hash, composite_hash)``."""
    return hash_text(system_text), hash_text(user_text), composite_hash(system_text, user_text)
