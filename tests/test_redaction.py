"""Tests for secret redaction utilities."""

from __future__ import annotations

from blink.security.redaction import redact_env, redact_secrets


class TestRedactSecrets:
    def test_redacts_api_key_assignment(self) -> None:
        text = "API_KEY=supersecretkey123"
        result = redact_secrets(text)
        assert "supersecretkey123" not in result
        assert "REDACTED" in result.upper()

    def test_redacts_password_colon(self) -> None:
        text = "password: MyS3cr3tP@ss"
        result = redact_secrets(text)
        assert "MyS3cr3tP@ss" not in result

    def test_redacts_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact_secrets(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "REDACTED" in result.upper()

    def test_redacts_openai_key(self) -> None:
        text = "sk-abcdefghijklmnopqrstuvwxyz1234567890ABCD"
        result = redact_secrets(text)
        assert "abcdefghijklmnopqrstuvwxyz1234567890ABCD" not in result
        assert "REDACTED" in result.upper()

    def test_redacts_github_token(self) -> None:
        text = "export TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234"
        result = redact_secrets(text)
        assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in result
        assert "REDACTED" in result.upper()

    def test_non_secret_text_unchanged(self) -> None:
        text = "ls -la /home/user"
        result = redact_secrets(text)
        assert result == text

    def test_redacts_long_hex_string(self) -> None:
        text = "0" * 32  # 32 hex chars = typical API key length
        result = redact_secrets(text)
        assert "0" * 32 not in result

    def test_short_hex_not_redacted(self) -> None:
        # Short hex values like git SHAs should not be redacted (< 32 chars)
        text = "abc123def456"  # 12 chars — not a secret
        result = redact_secrets(text)
        # 12-char hex is too short for our pattern (requires 32+)
        assert "abc123def456" in result

    def test_empty_string(self) -> None:
        assert redact_secrets("") == ""

    def test_multiline_text(self) -> None:
        text = "line1\nAPI_KEY=secret123456\nline3"
        result = redact_secrets(text)
        assert "secret123456" not in result
        assert "line1" in result
        assert "line3" in result


class TestRedactEnv:
    def test_redacts_api_key_variable(self) -> None:
        env = {"OPENAI_API_KEY": "sk-test", "HOME": "/home/user"}
        result = redact_env(env)
        assert result["OPENAI_API_KEY"] == "[REDACTED]"
        assert result["HOME"] == "/home/user"

    def test_redacts_password_variable(self) -> None:
        env = {"DB_PASSWORD": "hunter2", "PATH": "/usr/bin"}
        result = redact_env(env)
        assert result["DB_PASSWORD"] == "[REDACTED]"
        assert result["PATH"] == "/usr/bin"

    def test_redacts_token_variable(self) -> None:
        env = {"GITHUB_TOKEN": "ghp_xyz123", "SHELL": "/bin/bash"}
        result = redact_env(env)
        assert result["GITHUB_TOKEN"] == "[REDACTED]"
        assert result["SHELL"] == "/bin/bash"

    def test_redacts_secret_variable(self) -> None:
        env = {"AWS_SECRET_ACCESS_KEY": "abcd1234", "USER": "alice"}
        result = redact_env(env)
        assert result["AWS_SECRET_ACCESS_KEY"] == "[REDACTED]"
        assert result["USER"] == "alice"

    def test_preserves_all_keys(self) -> None:
        env = {"A": "1", "API_KEY": "secret", "B": "2"}
        result = redact_env(env)
        assert set(result.keys()) == {"A", "API_KEY", "B"}

    def test_empty_env(self) -> None:
        assert redact_env({}) == {}

    def test_does_not_mutate_original(self) -> None:
        env = {"API_KEY": "secret"}
        _ = redact_env(env)
        assert env["API_KEY"] == "secret"

    def test_redacts_credential_variable(self) -> None:
        env = {"AWS_CREDENTIAL": "AKIAIOSFODNN7EXAMPLE"}
        result = redact_env(env)
        assert result["AWS_CREDENTIAL"] == "[REDACTED]"

    def test_non_sensitive_values_pass_through_redact_secrets(self) -> None:
        env = {"GREETING": "hello world", "NUMBER": "42"}
        result = redact_env(env)
        assert result["GREETING"] == "hello world"
        assert result["NUMBER"] == "42"
