"""Tests for the capability-based security model."""

from __future__ import annotations

import pytest

from blink.security.capabilities import (
    TOOL_CAPABILITY_MAP,
    Capability,
    CapabilityChecker,
    SecurityPolicy,
)


class TestCapabilityOrdering:
    def test_observe_le_observe(self) -> None:
        assert Capability.OBSERVE <= Capability.OBSERVE

    def test_observe_le_act(self) -> None:
        assert Capability.OBSERVE <= Capability.ACT

    def test_act_not_le_observe(self) -> None:
        assert not (Capability.ACT <= Capability.OBSERVE)

    def test_admin_is_greatest(self) -> None:
        for cap in [Capability.OBSERVE, Capability.SUGGEST, Capability.ACT]:
            assert cap <= Capability.ADMIN

    def test_lt_strict(self) -> None:
        assert Capability.OBSERVE < Capability.SUGGEST
        assert not (Capability.SUGGEST < Capability.SUGGEST)


class TestSecurityPolicy:
    def test_defaults(self) -> None:
        policy = SecurityPolicy()
        assert policy.default_capability == Capability.OBSERVE
        assert policy.granted_capability == Capability.OBSERVE
        assert policy.require_confirmation is True
        assert policy.auto_approve == []
        assert policy.blocked_patterns == []

    def test_serialise_roundtrip(self) -> None:
        policy = SecurityPolicy(
            granted_capability=Capability.ACT,
            auto_approve=["git status"],
            blocked_patterns=["rm -rf *"],
        )
        raw = policy.model_dump_json()
        loaded = SecurityPolicy.model_validate_json(raw)
        assert loaded.granted_capability == Capability.ACT
        assert loaded.auto_approve == ["git status"]


class TestCapabilityChecker:
    @pytest.fixture
    def observe_policy(self) -> SecurityPolicy:
        return SecurityPolicy(granted_capability=Capability.OBSERVE)

    @pytest.fixture
    def act_policy(self) -> SecurityPolicy:
        return SecurityPolicy(
            granted_capability=Capability.ACT,
            require_confirmation=False,
        )

    async def test_observe_tool_allowed_with_observe_grant(
        self, observe_policy: SecurityPolicy
    ) -> None:
        checker = CapabilityChecker(observe_policy)
        allowed, reason = await checker.check("list_sessions", {})
        assert allowed is True
        assert reason is None

    async def test_act_tool_denied_with_observe_grant(
        self, observe_policy: SecurityPolicy
    ) -> None:
        checker = CapabilityChecker(observe_policy)
        allowed, reason = await checker.check("run_command", {"cmd": "ls"})
        assert allowed is False
        assert reason is not None
        assert "capability" in reason.lower()

    async def test_act_tool_allowed_with_act_grant_no_confirm(
        self, act_policy: SecurityPolicy
    ) -> None:
        checker = CapabilityChecker(act_policy)
        allowed, reason = await checker.check("run_command", {"cmd": "echo hello"})
        assert allowed is True

    async def test_blocked_pattern_denies_even_with_act(
        self, act_policy: SecurityPolicy
    ) -> None:
        act_policy.blocked_patterns = ["rm -rf *"]
        checker = CapabilityChecker(act_policy)
        allowed, reason = await checker.check("run_command", {"cmd": "rm -rf /"})
        assert allowed is False
        assert reason is not None
        assert "blocked" in reason.lower()

    async def test_auto_approve_skips_confirmation(self) -> None:
        policy = SecurityPolicy(
            granted_capability=Capability.ACT,
            require_confirmation=True,
            auto_approve=["git status"],
        )
        checker = CapabilityChecker(policy)
        # If this required confirmation it would return False (no TTY in tests)
        allowed, _ = await checker.check("run_command", {"cmd": "git status"})
        assert allowed is True

    async def test_suggest_tool_allowed_with_suggest_grant(self) -> None:
        policy = SecurityPolicy(granted_capability=Capability.SUGGEST)
        checker = CapabilityChecker(policy)
        allowed, _ = await checker.check("replace_prompt_buffer", {"text": "ls"})
        assert allowed is True

    async def test_suggest_tool_denied_with_observe_grant(self) -> None:
        policy = SecurityPolicy(granted_capability=Capability.OBSERVE)
        checker = CapabilityChecker(policy)
        allowed, reason = await checker.check("replace_prompt_buffer", {"text": "ls"})
        assert allowed is False

    async def test_per_tool_policy_override(self) -> None:
        # Override run_command to only require OBSERVE
        policy = SecurityPolicy(
            granted_capability=Capability.OBSERVE,
            tool_capabilities={"run_command": Capability.OBSERVE},
        )
        checker = CapabilityChecker(policy)
        allowed, _ = await checker.check("run_command", {"cmd": "ls"})
        assert allowed is True

    async def test_unknown_tool_uses_default_capability(self) -> None:
        policy = SecurityPolicy(
            granted_capability=Capability.OBSERVE,
            default_capability=Capability.OBSERVE,
        )
        checker = CapabilityChecker(policy)
        # Unknown tool defaults to OBSERVE; caller has OBSERVE — should be allowed
        allowed, _ = await checker.check("some_unknown_tool", {})
        assert allowed is True

    async def test_require_confirmation_non_tty_denies(self) -> None:
        """With require_confirmation=True and no TTY, ACT tools should be denied."""
        policy = SecurityPolicy(
            granted_capability=Capability.ACT,
            require_confirmation=True,
        )
        checker = CapabilityChecker(policy)
        # sys.stdin is not a TTY in pytest — should deny
        allowed, reason = await checker.check("run_command", {"cmd": "echo hi"})
        assert allowed is False
        assert reason is not None


class TestToolCapabilityMap:
    def test_read_tools_are_observe(self) -> None:
        for tool in ["get_active_session", "list_sessions", "list_blocks", "get_block",
                     "get_visible_screen", "get_selection", "get_prompt_buffer"]:
            assert TOOL_CAPABILITY_MAP[tool] == Capability.OBSERVE

    def test_buffer_tools_are_suggest(self) -> None:
        for tool in ["replace_prompt_buffer", "insert_at_cursor", "accept_completion"]:
            assert TOOL_CAPABILITY_MAP[tool] == Capability.SUGGEST

    def test_write_tools_are_act(self) -> None:
        for tool in ["run_command", "write_stdin", "send_signal", "cancel_command"]:
            assert TOOL_CAPABILITY_MAP[tool] == Capability.ACT
