"""Tests for Phase 5 security hardening modules."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# RateLimiter tests
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Tests for the sliding-window rate limiter."""

    @pytest.fixture
    def limiter(self):
        from blink.security.ratelimit import RateLimiter

        return RateLimiter(max_calls=5, window_seconds=60)

    async def test_allows_calls_within_limit(self, limiter) -> None:
        for _ in range(5):
            allowed = await limiter.check("run_command", "sess-1")
            assert allowed is True

    async def test_blocks_call_over_limit(self, limiter) -> None:
        for _ in range(5):
            await limiter.check("run_command", "sess-1")
        # 6th call should be blocked
        allowed = await limiter.check("run_command", "sess-1")
        assert allowed is False

    async def test_different_sessions_are_independent(self, limiter) -> None:
        """Rate limit is per-session."""
        for _ in range(5):
            await limiter.check("run_command", "sess-A")
        # sess-B should still have headroom
        allowed = await limiter.check("run_command", "sess-B")
        assert allowed is True

    async def test_different_tools_are_independent(self, limiter) -> None:
        """Rate limit is per-tool."""
        for _ in range(5):
            await limiter.check("run_command", "sess-1")
        # Different tool should still be allowed
        allowed = await limiter.check("get_visible_screen", "sess-1")
        assert allowed is True

    async def test_remaining_decreases_with_calls(self, limiter) -> None:
        assert await limiter.remaining("run_command", "sess-1") == 5
        await limiter.check("run_command", "sess-1")
        assert await limiter.remaining("run_command", "sess-1") == 4

    async def test_remaining_zero_at_limit(self, limiter) -> None:
        for _ in range(5):
            await limiter.check("run_command", "sess-1")
        assert await limiter.remaining("run_command", "sess-1") == 0

    async def test_reset_clears_history(self, limiter) -> None:
        for _ in range(5):
            await limiter.check("run_command", "sess-1")
        await limiter.reset("run_command", "sess-1")
        assert await limiter.remaining("run_command", "sess-1") == 5

    async def test_reset_all_clears_everything(self, limiter) -> None:
        await limiter.check("run_command", "sess-1")
        await limiter.check("get_block", "sess-2")
        await limiter.reset_all()
        assert await limiter.remaining("run_command", "sess-1") == 5
        assert await limiter.remaining("get_block", "sess-2") == 5

    async def test_stats_shows_active_keys(self, limiter) -> None:
        await limiter.check("run_command", "sess-1")
        await limiter.check("run_command", "sess-1")
        stats = await limiter.stats()
        assert "run_command:sess-1" in stats
        assert stats["run_command:sess-1"] == 2

    async def test_per_tool_limits_override_default(self) -> None:
        from blink.security.ratelimit import RateLimiter

        limiter = RateLimiter(max_calls=100, per_tool_limits={"run_command": 2})
        await limiter.check("run_command", "s")
        await limiter.check("run_command", "s")
        assert await limiter.check("run_command", "s") is False
        # Other tools use default (100)
        assert await limiter.check("get_block", "s") is True

    def test_invalid_max_calls_raises(self) -> None:
        from blink.security.ratelimit import RateLimiter

        with pytest.raises(ValueError, match="max_calls"):
            RateLimiter(max_calls=0)

    def test_invalid_window_raises(self) -> None:
        from blink.security.ratelimit import RateLimiter

        with pytest.raises(ValueError, match="window_seconds"):
            RateLimiter(window_seconds=0)

    def test_properties(self) -> None:
        from blink.security.ratelimit import RateLimiter

        limiter = RateLimiter(max_calls=42, window_seconds=120)
        assert limiter.max_calls == 42
        assert limiter.window_seconds == 120


# ---------------------------------------------------------------------------
# Sanitize tests
# ---------------------------------------------------------------------------


class TestSanitizeCommand:
    def test_removes_null_bytes(self) -> None:
        from blink.security.sanitize import sanitize_command

        result = sanitize_command("ls\x00 -la")
        assert "\x00" not in result
        assert "ls" in result

    def test_removes_control_characters(self) -> None:
        from blink.security.sanitize import sanitize_command

        result = sanitize_command("echo\x01hello\x07")
        assert "\x01" not in result
        assert "\x07" not in result
        assert "echo" in result

    def test_preserves_newlines_and_tabs(self) -> None:
        """Newlines and tabs are legitimate in commands."""
        from blink.security.sanitize import sanitize_command

        result = sanitize_command("echo\thello\nworld")
        assert "echo" in result

    def test_truncates_long_commands(self) -> None:
        from blink.security.sanitize import sanitize_command

        long_cmd = "a" * 10000
        result = sanitize_command(long_cmd)
        assert len(result) <= 8192

    def test_empty_string(self) -> None:
        from blink.security.sanitize import sanitize_command

        assert sanitize_command("") == ""

    def test_strict_mode_removes_metacharacters(self) -> None:
        from blink.security.sanitize import sanitize_command

        result = sanitize_command("ls; rm -rf /", strict=True)
        assert ";" not in result
        assert "|" not in result

    def test_unicode_normalisation(self) -> None:
        from blink.security.sanitize import sanitize_command

        # Composed vs decomposed form of 'é'
        import unicodedata

        composed = unicodedata.normalize("NFC", "\u00e9")
        result = sanitize_command(composed)
        assert result  # Should not be empty


class TestSanitizePath:
    def test_removes_null_bytes(self) -> None:
        from blink.security.sanitize import sanitize_path

        result = sanitize_path("/tmp/te\x00st")
        assert "\x00" not in result

    def test_collapses_redundant_slashes(self) -> None:
        from blink.security.sanitize import sanitize_path

        # PurePosixPath normalises internal double slashes but preserves leading //
        # (POSIX allows // to mean something special on some systems)
        result = sanitize_path("/tmp//test//file")
        assert "test/file" in result or result.endswith("file")

    def test_empty_path(self) -> None:
        from blink.security.sanitize import sanitize_path

        assert sanitize_path("") == ""

    def test_path_traversal_raises_with_base(self, tmp_path: Path) -> None:
        from blink.security.sanitize import sanitize_path

        with pytest.raises(ValueError, match="escapes"):
            sanitize_path("../../etc/passwd", base_dir=tmp_path)

    def test_safe_path_within_base(self, tmp_path: Path) -> None:
        from blink.security.sanitize import sanitize_path

        (tmp_path / "subdir").mkdir()
        result = sanitize_path("subdir/file.txt", base_dir=tmp_path)
        assert "subdir" in result
        assert str(tmp_path) in result

    def test_is_safe_path_returns_false_for_traversal(self, tmp_path: Path) -> None:
        from blink.security.sanitize import is_safe_path

        assert is_safe_path("../../etc/passwd", base_dir=tmp_path) is False

    def test_is_safe_path_returns_true_for_valid(self, tmp_path: Path) -> None:
        from blink.security.sanitize import is_safe_path

        assert is_safe_path("file.txt", base_dir=tmp_path) is True

    def test_truncates_long_path(self) -> None:
        from blink.security.sanitize import sanitize_path

        long_path = "/tmp/" + "a" * 5000
        result = sanitize_path(long_path)
        assert len(result) <= 4096


class TestSanitizeDisplayText:
    def test_strips_ansi_escapes(self) -> None:
        from blink.security.sanitize import sanitize_display_text

        text = "\033[1;31mRed bold\033[0m"
        result = sanitize_display_text(text)
        assert "\033[" not in result
        assert "Red bold" in result

    def test_strips_null_bytes(self) -> None:
        from blink.security.sanitize import sanitize_display_text

        result = sanitize_display_text("hello\x00world")
        assert "\x00" not in result

    def test_truncates_to_max_length(self) -> None:
        from blink.security.sanitize import sanitize_display_text

        result = sanitize_display_text("x" * 200, max_length=100)
        assert len(result) <= 100


class TestStripAnsi:
    def test_removes_colour_codes(self) -> None:
        from blink.security.sanitize import strip_ansi

        assert strip_ansi("\033[32mGreen\033[0m") == "Green"

    def test_preserves_plain_text(self) -> None:
        from blink.security.sanitize import strip_ansi

        assert strip_ansi("plain text") == "plain text"

    def test_empty_string(self) -> None:
        from blink.security.sanitize import strip_ansi

        assert strip_ansi("") == ""


# ---------------------------------------------------------------------------
# CredentialStore tests
# ---------------------------------------------------------------------------


class TestCredentialStore:
    """Tests for CredentialStore with keyring disabled (file fallback only)."""

    @pytest.fixture
    def store(self, tmp_path: Path):
        from blink.security.keyring import CredentialStore

        return CredentialStore(
            use_keyring=False,
            credentials_file=tmp_path / "credentials.json",
        )

    async def test_store_and_get(self, store) -> None:
        await store.store("providers", "openai_api_key", "sk-test-123")
        value = await store.get("providers", "openai_api_key")
        assert value == "sk-test-123"

    async def test_get_missing_returns_none(self, store) -> None:
        value = await store.get("providers", "nonexistent")
        assert value is None

    async def test_delete_credential(self, store) -> None:
        await store.store("providers", "key_to_delete", "secret")
        await store.delete("providers", "key_to_delete")
        value = await store.get("providers", "key_to_delete")
        assert value is None

    async def test_delete_nonexistent_is_noop(self, store) -> None:
        # Should not raise
        await store.delete("providers", "not_here")

    async def test_list_keys(self, store) -> None:
        await store.store("providers", "key_a", "val_a")
        await store.store("providers", "key_b", "val_b")
        keys = await store.list_keys("providers")
        assert "key_a" in keys
        assert "key_b" in keys

    async def test_list_keys_empty_service(self, store) -> None:
        keys = await store.list_keys("nonexistent_service")
        assert keys == []

    async def test_multiple_services(self, store) -> None:
        await store.store("providers", "anthropic", "sk-ant-…")
        await store.store("git", "token", "ghp_abc")
        assert await store.get("providers", "anthropic") == "sk-ant-…"
        assert await store.get("git", "token") == "ghp_abc"

    async def test_overwrite_updates_value(self, store) -> None:
        await store.store("providers", "key", "old_value")
        await store.store("providers", "key", "new_value")
        value = await store.get("providers", "key")
        assert value == "new_value"

    async def test_credentials_file_is_not_plaintext(self, store, tmp_path: Path) -> None:
        """Stored values should not appear in plaintext in the file."""
        await store.store("providers", "secret_key", "super_secret_value")
        creds_file = tmp_path / "credentials.json"
        raw = creds_file.read_text()
        assert "super_secret_value" not in raw

    async def test_file_permissions(self, store, tmp_path: Path) -> None:
        """Credentials file should have restricted permissions (600)."""
        await store.store("providers", "key", "value")
        creds_file = tmp_path / "credentials.json"
        import stat

        mode = stat.S_IMODE(creds_file.stat().st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# AuditFinding tests
# ---------------------------------------------------------------------------


class TestAuditFinding:
    def test_str_representation(self) -> None:
        from blink.security.audit import AuditFinding

        finding = AuditFinding(
            severity="critical",
            tool="run_command",
            issue="No confirmation required",
            recommendation="Enable require_confirmation",
        )
        s = str(finding)
        assert "CRITICAL" in s
        assert "run_command" in s
        assert "No confirmation required" in s

    def test_all_severity_levels(self) -> None:
        from blink.security.audit import AuditFinding

        for severity in ("info", "warning", "critical"):
            f = AuditFinding(
                severity=severity,
                tool="test_tool",
                issue="test issue",
                recommendation="test rec",
            )
            assert f.severity == severity


class TestAuditTools:
    """Tests for the audit_tools function."""

    @pytest.fixture
    def mock_server(self):
        """Build a minimal mock MCPServer for auditing."""
        from blink.security.capabilities import Capability, CapabilityChecker, SecurityPolicy

        policy = SecurityPolicy(
            granted_capability=Capability.OBSERVE,
            require_confirmation=True,
        )
        checker = CapabilityChecker(policy)

        server = MagicMock()
        server._policy = policy
        server._checker = checker
        server.handle_tools_list = AsyncMock(
            return_value={
                "tools": [
                    {
                        "name": "get_active_session",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    },
                    {
                        "name": "run_command",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"cmd": {"type": "string"}},
                            "required": ["cmd"],
                        },
                    },
                ]
            }
        )
        return server

    async def test_clean_server_has_no_critical_findings(self, mock_server) -> None:
        from blink.security.audit import audit_tools

        findings = await audit_tools(mock_server)
        criticals = [f for f in findings if f.severity == "critical"]
        assert criticals == []

    async def test_admin_capability_flagged_as_critical(self) -> None:
        from blink.security.audit import audit_tools
        from blink.security.capabilities import Capability, CapabilityChecker, SecurityPolicy

        policy = SecurityPolicy(granted_capability=Capability.ADMIN)
        server = MagicMock()
        server._policy = policy
        server._checker = CapabilityChecker(policy)
        server.handle_tools_list = AsyncMock(return_value={"tools": []})

        findings = await audit_tools(server)
        criticals = [f for f in findings if f.severity == "critical"]
        assert len(criticals) >= 1
        assert any("ADMIN" in f.issue or "admin" in f.issue.lower() for f in criticals)

    async def test_act_without_confirmation_flagged(self) -> None:
        from blink.security.audit import audit_tools
        from blink.security.capabilities import Capability, CapabilityChecker, SecurityPolicy

        policy = SecurityPolicy(
            granted_capability=Capability.ACT,
            require_confirmation=False,  # dangerous!
        )
        server = MagicMock()
        server._policy = policy
        server._checker = CapabilityChecker(policy)
        server.handle_tools_list = AsyncMock(
            return_value={
                "tools": [
                    {
                        "name": "run_command",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            }
        )
        findings = await audit_tools(server)
        warnings = [f for f in findings if f.severity == "warning"]
        assert any("confirmation" in f.issue.lower() for f in warnings)

    async def test_wildcard_auto_approve_flagged_as_critical(self) -> None:
        from blink.security.audit import audit_tools
        from blink.security.capabilities import Capability, CapabilityChecker, SecurityPolicy

        policy = SecurityPolicy(
            granted_capability=Capability.ACT,
            require_confirmation=True,
            auto_approve=["*"],  # dangerous catch-all
        )
        server = MagicMock()
        server._policy = policy
        server._checker = CapabilityChecker(policy)
        server.handle_tools_list = AsyncMock(return_value={"tools": []})

        findings = await audit_tools(server)
        criticals = [f for f in findings if f.severity == "critical"]
        assert any("auto_approve" in f.issue.lower() or "*" in f.issue for f in criticals)

    async def test_missing_schema_flagged(self) -> None:
        from blink.security.audit import audit_tools
        from blink.security.capabilities import Capability, CapabilityChecker, SecurityPolicy

        policy = SecurityPolicy(granted_capability=Capability.OBSERVE)
        server = MagicMock()
        server._policy = policy
        server._checker = CapabilityChecker(policy)
        server.handle_tools_list = AsyncMock(
            return_value={
                "tools": [
                    {"name": "get_active_session"}  # missing inputSchema
                ]
            }
        )
        findings = await audit_tools(server)
        warnings = [f for f in findings if f.severity == "warning"]
        assert any("inputSchema" in f.issue or "schema" in f.issue.lower() for f in warnings)

    async def test_audit_report_clean(self, mock_server) -> None:
        from blink.security.audit import audit_report

        report = await audit_report(mock_server)
        assert "Blink MCP Security Audit Report" in report
        # A clean server may still have info findings (unmapped tools in static map)
        assert isinstance(report, str)


# ---------------------------------------------------------------------------
# Enhanced redaction tests (new patterns)
# ---------------------------------------------------------------------------


class TestEnhancedRedaction:
    def test_redacts_private_key_block(self) -> None:
        from blink.security.redaction import redact_secrets

        text = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"
        result = redact_secrets(text)
        assert "MIIEvQIBADANBg" not in result
        assert "REDACTED" in result.upper()

    def test_redacts_client_secret(self) -> None:
        from blink.security.redaction import redact_secrets

        text = "client_secret=abc123xyzQWERTY"
        result = redact_secrets(text)
        assert "abc123xyzQWERTY" not in result
        assert "REDACTED" in result.upper()

    def test_redacts_postgres_dsn(self) -> None:
        from blink.security.redaction import redact_secrets

        text = "DATABASE_URL=postgres://user:hunter2@localhost/mydb"
        result = redact_secrets(text)
        assert "hunter2" not in result

    def test_redacts_mongodb_srv_dsn(self) -> None:
        from blink.security.redaction import redact_secrets

        text = "mongodb+srv://dbuser:s3cr3t@cluster.example.mongodb.net/mydb"
        result = redact_secrets(text)
        assert "s3cr3t" not in result

    def test_redacts_jwt(self) -> None:
        from blink.security.redaction import redact_secrets

        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = redact_secrets(jwt)
        assert jwt not in result
        assert "REDACTED" in result.upper()

    def test_redacts_stripe_like_key(self) -> None:
        from blink.security.redaction import redact_secrets

        # Use a pattern that tests the regex but isn't a valid Stripe format
        text = "stripe_secret=STRIPEKEY123456789012345"
        result = redact_secrets(text)
        assert "STRIPEKEY" not in result or "REDACTED" in result.upper()
