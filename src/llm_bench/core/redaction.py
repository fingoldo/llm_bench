"""Canonical secret-redaction module for llm_bench (audit: security
07-Low, 07-Low #7) — every credential-shaped regex in the codebase
belongs here; other modules import from this one instead of defining
their own (``pyutilz.dev.code_audit``'s ``duplicate_credential_regex``
scanner enforces this convention, though wiring its
``canonical_module_rel_paths`` designation through the shared
``py_ci_shared.code_audit_meta`` test harness is out of this repo's
scope — that finding is grandfathered in the baseline with this
docstring as the review record).

Two distinct use cases live here:

- ``redact_secrets``: scrubs free-form exception text before it's
  persisted (``RunRow.error_message``, indefinitely retained in
  JSONL/Postgres) or logged. Exceptions raised deep in dependencies
  (``pyutilz``/``httpx``/``asyncpg``) are not something this repo
  controls or audits — this is defense-in-depth, not a confirmed live
  leak: if a downstream library's exception text ever echoes a
  credential (a malformed-DSN error including the full connection
  string with its password, an HTTP client whose request ``repr()``
  includes an ``Authorization`` header), this scrub runs first.
- ``redact_dsn``: scrubs a KNOWN Postgres DSN string specifically
  (``PostgresStorage``'s own ``self._url``), for the narrower case of
  wrapping a ``create_pool`` connection failure without ever echoing
  the DSN's embedded password.
"""

from __future__ import annotations

import re

_BEARER_TOKEN_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_DSN_CREDENTIALS_RE = re.compile(r"://[^:/@\s]+:[^@/\s]+@")
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")


def redact_secrets(text: str) -> str:
    """Mask Bearer tokens, DSN-embedded credentials, and ``sk-*``-style
    API keys in free-form text before it's persisted or logged."""
    text = _BEARER_TOKEN_RE.sub("Bearer ***", text)
    text = _DSN_CREDENTIALS_RE.sub("://***:***@", text)
    text = _API_KEY_RE.sub("sk-***", text)
    return text


def redact_dsn(url: str) -> str:
    """Strip embedded credentials from a Postgres DSN:
    ``postgresql://user:pass@host/db`` becomes
    ``postgresql://***:***@host/db``."""
    return _DSN_CREDENTIALS_RE.sub("://***:***@", url)
