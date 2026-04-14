"""Capability-based security model for MCP tool calls.

Three tiers of capability:

- OBSERVE  — read terminal state (always allowed by default)
- SUGGEST  — modify the prompt buffer (allowed by default, low risk)
- ACT      — execute commands or send signals (requires confirmation)
- ADMIN    — full unrestricted control (dangerous; disabled by default)

Policy is expressed as a :class:`SecurityPolicy` Pydantic model that
can be persisted to / loaded from JSON.  A :class:`CapabilityChecker`
evaluates tool-call requests against the active policy.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Capability levels
# ---------------------------------------------------------------------------


class Capability(StrEnum):
    """Ordered capability tiers (higher ordinal = more privileged)."""

    OBSERVE = "observe"
    SUGGEST = "suggest"
    ACT = "act"
    ADMIN = "admin"

    def _rank(self) -> int:
        return list(Capability).index(self)

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Capability):
            return NotImplemented
        return self._rank() <= other._rank()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Capability):
            return NotImplemented
        return self._rank() < other._rank()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Capability):
            return NotImplemented
        return self._rank() >= other._rank()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Capability):
            return NotImplemented
        return self._rank() > other._rank()


# ---------------------------------------------------------------------------
# Security policy
# ---------------------------------------------------------------------------


class SecurityPolicy(BaseModel):
    """Security policy governing MCP tool access.

    Attributes:
        default_capability: The minimum capability level granted to callers
            that have not been given an explicit per-tool override.
        tool_capabilities: Map of tool name → required :class:`Capability`.
            If a tool is not listed here the ``default_capability`` is used.
        auto_approve: Shell-glob patterns for commands that should be
            executed without interactive confirmation (e.g. ``"git status"``).
        blocked_patterns: Shell-glob or regex patterns for commands that
            must *always* be blocked regardless of capability level.
        require_confirmation: When ``True`` (the default), ACT-level tool
            calls that are not matched by ``auto_approve`` will pause and ask
            the user for confirmation before proceeding.
        granted_capability: The capability level currently granted to the
            calling agent session.  Defaults to OBSERVE.
    """

    default_capability: Capability = Capability.OBSERVE
    tool_capabilities: dict[str, Capability] = Field(default_factory=dict)
    auto_approve: list[str] = Field(default_factory=list)
    blocked_patterns: list[str] = Field(default_factory=list)
    require_confirmation: bool = True
    granted_capability: Capability = Capability.OBSERVE


# ---------------------------------------------------------------------------
# Default per-tool capability assignments
# ---------------------------------------------------------------------------

#: Maps every defined MCP tool to its required :class:`Capability`.
TOOL_CAPABILITY_MAP: dict[str, Capability] = {
    # ----- Observe (read-only) -----
    "get_active_session": Capability.OBSERVE,
    "list_sessions": Capability.OBSERVE,
    "list_blocks": Capability.OBSERVE,
    "get_block": Capability.OBSERVE,
    "get_visible_screen": Capability.OBSERVE,
    "get_selection": Capability.OBSERVE,
    "get_prompt_buffer": Capability.OBSERVE,
    # ----- Suggest (prompt buffer mutation) -----
    "replace_prompt_buffer": Capability.SUGGEST,
    "insert_at_cursor": Capability.SUGGEST,
    "accept_completion": Capability.SUGGEST,
    # ----- Act (executes code / sends signals) -----
    "run_command": Capability.ACT,
    "write_stdin": Capability.ACT,
    "send_signal": Capability.ACT,
    "cancel_command": Capability.ACT,
}


# ---------------------------------------------------------------------------
# CapabilityChecker
# ---------------------------------------------------------------------------


class CapabilityChecker:
    """Evaluate tool-call requests against the active :class:`SecurityPolicy`.

    Usage::

        checker = CapabilityChecker(policy)
        allowed, reason = await checker.check("run_command", {"cmd": "ls"})
        if not allowed:
            raise PermissionError(reason)
    """

    def __init__(self, policy: SecurityPolicy) -> None:
        self.policy = policy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str | None]:
        """Return ``(allowed, reason)``.

        If *allowed* is ``False``, *reason* contains a human-readable
        explanation of why the call was rejected.

        For ACT-level tools that are not auto-approved this method will
        call :meth:`request_confirmation` when ``require_confirmation``
        is enabled in the policy.
        """
        required = self._required_capability(tool)

        # 1. Check if the command matches a hard block pattern
        cmd = arguments.get("cmd", "")
        if cmd and self._is_blocked(cmd):
            return False, f"Command '{cmd}' matches a blocked pattern."

        # 2. Check that the granted capability is sufficient
        if not (self.policy.granted_capability >= required):
            return (
                False,
                f"Tool '{tool}' requires capability '{required.value}' but the current "
                f"session only has '{self.policy.granted_capability.value}'.",
            )

        # 3. For ACT-level tools, optionally ask for confirmation
        if required == Capability.ACT and self.policy.require_confirmation:
            if not self._is_auto_approved(cmd):
                confirmed = await self.request_confirmation(tool, arguments)
                if not confirmed:
                    return False, f"User did not confirm execution of tool '{tool}'."

        return True, None

    async def request_confirmation(self, tool: str, arguments: dict[str, Any]) -> bool:
        """Ask the user (via stderr) to confirm an ACT-level tool call.

        In a real deployment this would be replaced by a proper UI prompt.
        When running non-interactively (no TTY) the call is denied by default.
        """
        if not sys.stdin.isatty():
            return False

        cmd = arguments.get("cmd", "")
        prompt = (
            f"\n[blink-mcp] Tool '{tool}' wants to execute: {cmd!r}\n"
            "Allow? [y/N] "
        )
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in {"y", "yes"}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _required_capability(self, tool: str) -> Capability:
        """Return the capability required to call *tool*."""
        # Per-policy override takes precedence over the static map
        if tool in self.policy.tool_capabilities:
            return self.policy.tool_capabilities[tool]
        return TOOL_CAPABILITY_MAP.get(tool, self.policy.default_capability)

    def _is_auto_approved(self, cmd: str) -> bool:
        """Return True if *cmd* matches any auto-approve pattern."""
        for pattern in self.policy.auto_approve:
            if fnmatch.fnmatch(cmd, pattern) or re.search(pattern, cmd):
                return True
        return False

    def _is_blocked(self, cmd: str) -> bool:
        """Return True if *cmd* matches any blocked pattern."""
        for pattern in self.policy.blocked_patterns:
            if fnmatch.fnmatch(cmd, pattern) or re.search(pattern, cmd):
                return True
        return False


__all__ = ["Capability", "SecurityPolicy", "CapabilityChecker", "TOOL_CAPABILITY_MAP"]
