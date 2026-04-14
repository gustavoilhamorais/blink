"""Rate limiting for MCP tool calls.

Prevents abuse by limiting how many times a specific tool can be invoked
within a rolling time window, on a per-(tool, session) basis.

Usage::

    limiter = RateLimiter(max_calls=100, window_seconds=60)
    allowed = await limiter.check("run_command", session_id)
    if not allowed:
        raise PermissionError("Rate limit exceeded for run_command")

The in-memory store is intentionally simple: a deque of timestamps per key.
For multi-process deployments, replace :class:`_InMemoryStore` with a Redis
backend or shared-memory equivalent.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any


class RateLimiter:
    """Sliding-window rate limiter for MCP tool calls.

    Tracks call timestamps in a per-``(tool, session_id)`` deque and evicts
    entries older than *window_seconds*.  Thread-safe via an asyncio lock.

    Args:
        max_calls: Maximum number of calls allowed per window per key.
        window_seconds: Length of the sliding window in seconds.
        per_tool_limits: Optional override map of ``tool_name → max_calls``
            for tools that warrant tighter (or looser) limits.

    Example::

        limiter = RateLimiter(
            max_calls=50,
            window_seconds=60,
            per_tool_limits={"run_command": 10, "get_visible_screen": 200},
        )
    """

    def __init__(
        self,
        max_calls: int = 100,
        window_seconds: int = 60,
        per_tool_limits: dict[str, int] | None = None,
    ) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be a positive integer")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be a positive integer")

        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._per_tool_limits: dict[str, int] = per_tool_limits or {}
        # key → deque of UNIX timestamps
        self._timestamps: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(self, tool: str, session_id: str) -> bool:
        """Return ``True`` if the call is within the rate limit, ``False`` otherwise.

        Calling this method *records* the attempt — a denied call still
        counts toward the window so that a burst of denied calls does not
        reset the window.

        Args:
            tool: Name of the MCP tool being called.
            session_id: Blink session ID (or any caller identifier).
        """
        key = f"{tool}:{session_id}"
        limit = self._per_tool_limits.get(tool, self._max_calls)
        now = time.monotonic()
        cutoff = now - self._window_seconds

        async with self._lock:
            dq = self._timestamps[key]

            # Evict expired timestamps
            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= limit:
                return False

            dq.append(now)
            return True

    async def remaining(self, tool: str, session_id: str) -> int:
        """Return the number of calls still allowed in the current window."""
        key = f"{tool}:{session_id}"
        limit = self._per_tool_limits.get(tool, self._max_calls)
        now = time.monotonic()
        cutoff = now - self._window_seconds

        async with self._lock:
            dq = self._timestamps[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            return max(0, limit - len(dq))

    async def reset(self, tool: str, session_id: str) -> None:
        """Clear the call history for a specific ``(tool, session_id)`` pair.

        Useful in tests or after a session is explicitly terminated.
        """
        key = f"{tool}:{session_id}"
        async with self._lock:
            self._timestamps.pop(key, None)

    async def reset_all(self) -> None:
        """Clear all rate-limit state (use with care in production)."""
        async with self._lock:
            self._timestamps.clear()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """Return a snapshot of current rate-limit counters.

        Useful for monitoring and debugging.

        Returns:
            A dict mapping ``"tool:session_id"`` keys to call counts within
            the current window.
        """
        now = time.monotonic()
        cutoff = now - self._window_seconds
        snapshot: dict[str, Any] = {}

        async with self._lock:
            for key, dq in self._timestamps.items():
                # Count non-expired entries without modifying the deque
                count = sum(1 for ts in dq if ts > cutoff)
                if count > 0:
                    snapshot[key] = count

        return snapshot

    @property
    def max_calls(self) -> int:
        """Default maximum calls per window."""
        return self._max_calls

    @property
    def window_seconds(self) -> int:
        """Length of the sliding window in seconds."""
        return self._window_seconds


__all__ = ["RateLimiter"]
