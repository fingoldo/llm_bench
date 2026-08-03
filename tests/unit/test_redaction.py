"""redact_secrets unit tests (audit: security 07-Low).

Direct coverage of the three patterns the finding specifically named:
Bearer tokens, DSN-embedded credentials, and sk-*-style API keys. See
tests/unit/test_round_runner.py::TestErrorMessageRedaction for the
end-to-end proof that a provider exception's text actually gets
scrubbed before it reaches a persisted RunRow.
"""

from __future__ import annotations

from llm_bench.core.redaction import redact_dsn, redact_secrets


class TestRedactSecrets:
    def test_bearer_token_masked(self):
        out = redact_secrets("request failed: Authorization: Bearer sk-abcdef1234567890")
        assert "sk-abcdef1234567890" not in out
        assert "Bearer ***" in out

    def test_bearer_token_case_insensitive(self):
        out = redact_secrets("bearer XYZ123SECRET")
        assert "XYZ123SECRET" not in out

    def test_dsn_credentials_masked(self):
        out = redact_secrets("connection failed: postgresql://myuser:hunter2@db.example.com:5432/mydb")
        assert "hunter2" not in out
        assert "myuser" not in out
        assert "://***:***@" in out
        # Host/db (not secret) survive, so the message is still useful for debugging.
        assert "db.example.com" in out

    def test_api_key_masked(self):
        out = redact_secrets("invalid key sk-proj-abcdefghij1234567890")
        assert "sk-proj-abcdefghij1234567890" not in out
        assert "sk-***" in out

    def test_short_sk_prefix_not_falsely_matched(self):
        # Below the 10-char threshold -- avoids masking innocuous text
        # that merely starts with "sk-" by coincidence.
        out = redact_secrets("sk-x")
        assert out == "sk-x"

    def test_plain_text_unchanged(self):
        text = "TimeoutError: request to https://openrouter.ai/api/v1/chat timed out after 30s"
        assert redact_secrets(text) == text

    def test_multiple_patterns_in_one_message(self):
        text = "auth failed: Bearer sk-liveTOKEN1234567890, dsn=postgresql://u:p@host/db"
        out = redact_secrets(text)
        assert "sk-liveTOKEN1234567890" not in out
        assert "Bearer sk-liveTOKEN1234567890" not in out
        assert "://***:***@" in out


class TestRedactDsn:
    def test_strips_credentials(self):
        assert redact_dsn("postgresql://user:secretpass@localhost:5432/db") == "postgresql://***:***@localhost:5432/db"

    def test_leaves_credential_free_dsn_unchanged(self):
        url = "postgresql://localhost:5432/db"
        assert redact_dsn(url) == url

    def test_password_never_appears_in_output(self):
        out = redact_dsn("postgresql://user:secretpass@localhost:5432/db")
        assert "secretpass" not in out
        assert "user" not in out
