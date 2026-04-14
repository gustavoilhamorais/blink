"""History-based completion ranker.

Queries the SQLite history table and scores each candidate command using a
composite signal:

    score = prefix_match × cwd_similarity × repo_similarity × recency × prior_acceptance

All factors are in [0, 1]. Only commands whose score exceeds *min_score* are
returned, capped at *max_results* entries sorted by descending score.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from blink.completions.context import CompletionContext
from blink.storage import Storage

# ---------------------------------------------------------------------------
# Completion model (also used by broker / provider)
# ---------------------------------------------------------------------------


class Completion(BaseModel):
    """A single completion candidate."""

    text: str
    """The full command text to insert."""

    display: str
    """What to show in the UI (may be abbreviated)."""

    confidence: float
    """Confidence score in [0, 1]."""

    source: str
    """Origin: 'history' | 'llm' | 'filesystem'."""

    metadata: dict[str, Any] = {}
    """Arbitrary extra data (e.g. exit_code, cwd, row_id)."""


# ---------------------------------------------------------------------------
# HistoryRanker
# ---------------------------------------------------------------------------

# How many seconds in a "day" for the recency decay formula.
_SECONDS_PER_DAY = 86_400.0
# Recency half-life in days — a command run this many days ago gets 0.5 recency.
_RECENCY_HALF_LIFE_DAYS = 7.0


def _recency_score(executed_at_iso: str) -> float:
    """Return a recency score in (0, 1] using exponential decay."""
    try:
        executed_at = datetime.fromisoformat(executed_at_iso)
        if executed_at.tzinfo is None:
            executed_at = executed_at.replace(tzinfo=UTC)
        age_seconds = (datetime.now(tz=UTC) - executed_at).total_seconds()
        age_days = max(age_seconds, 0) / _SECONDS_PER_DAY
        # e^(-λt) where λ = ln(2) / half_life
        lam = math.log(2) / _RECENCY_HALF_LIFE_DAYS
        return math.exp(-lam * age_days)
    except (ValueError, OSError):
        return 0.1  # fallback for bad timestamps


def _prefix_match_score(command: str, buffer: str) -> float:
    """Return 1.0 for an exact prefix match, 0.0 if no match, else partial."""
    if not buffer:
        return 0.5  # empty buffer: everything is vaguely relevant
    buf_lower = buffer.lower()
    cmd_lower = command.lower()
    if cmd_lower.startswith(buf_lower):
        # Reward longer matches more strongly.
        return min(1.0, 0.7 + 0.3 * (len(buffer) / max(len(command), 1)))
    # No prefix match — hard disqualifier.
    return 0.0


def _cwd_similarity(cmd_cwd: str, ctx_cwd: str) -> float:
    """Score based on shared path prefix length."""
    if not ctx_cwd or not cmd_cwd:
        return 0.5
    if cmd_cwd == ctx_cwd:
        return 1.0
    # Shared prefix segments
    ctx_parts = ctx_cwd.rstrip("/").split("/")
    cmd_parts = cmd_cwd.rstrip("/").split("/")
    common = 0
    for a, b in zip(ctx_parts, cmd_parts, strict=False):
        if a == b:
            common += 1
        else:
            break
    return common / max(len(ctx_parts), len(cmd_parts), 1)


def _repo_similarity(cmd_cwd: str, repo_root: str | None) -> float:
    """1.0 if cmd was run inside the same repo, 0.5 otherwise."""
    if repo_root is None:
        return 0.5
    if cmd_cwd.startswith(repo_root):
        return 1.0
    return 0.3


def _prior_acceptance_score(exit_code: int | None) -> float:
    """Penalise commands that previously failed."""
    if exit_code is None:
        return 0.7
    return 1.0 if exit_code == 0 else 0.3


class HistoryRanker:
    """Rank completion candidates from SQLite command history."""

    def __init__(
        self,
        storage: Storage,
        max_results: int = 10,
        min_score: float = 0.1,
        history_limit: int = 500,
    ) -> None:
        self._storage = storage
        self._max_results = max_results
        self._min_score = min_score
        self._history_limit = history_limit

    async def rank(self, context: CompletionContext) -> list[Completion]:
        """Query history and return scored completion candidates."""
        rows = await self._fetch_history(context)

        scored: list[tuple[float, dict[str, Any]]] = []
        buffer = context.buffer[: context.cursor_position]

        for row in rows:
            command: str = row["command"]
            if not command.strip():
                continue

            prefix = _prefix_match_score(command, buffer)
            if prefix == 0.0:
                continue  # fast path: skip non-matching commands

            cwd_sim = _cwd_similarity(row.get("cwd", ""), context.cwd)
            repo_sim = _repo_similarity(row.get("cwd", ""), context.repo_root)
            recency = _recency_score(row.get("executed_at", ""))
            acceptance = _prior_acceptance_score(row.get("exit_code"))

            score = prefix * cwd_sim * repo_sim * recency * acceptance

            if score >= self._min_score:
                scored.append((score, row))

        # De-duplicate by command text, keeping highest score per command.
        best: dict[str, tuple[float, dict[str, Any]]] = {}
        for score, row in scored:
            cmd = row["command"]
            if cmd not in best or score > best[cmd][0]:
                best[cmd] = (score, row)

        sorted_results = sorted(best.values(), key=lambda t: t[0], reverse=True)

        return [
            Completion(
                text=row["command"],
                display=self._abbreviate(row["command"]),
                confidence=min(score, 1.0),
                source="history",
                metadata={
                    "cwd": row.get("cwd", ""),
                    "exit_code": row.get("exit_code"),
                    "executed_at": row.get("executed_at", ""),
                    "row_id": row.get("id"),
                },
            )
            for score, row in sorted_results[: self._max_results]
        ]

    async def _fetch_history(self, context: CompletionContext) -> list[dict[str, Any]]:
        """Fetch candidate history rows from SQLite."""
        buffer = context.buffer[: context.cursor_position]
        if buffer.strip():
            # Use LIKE for a quick DB-level pre-filter (case-insensitive via COLLATE NOCASE).
            pattern = buffer.replace("%", r"\%").replace("_", r"\_") + "%"
            return await self._storage.fetchall(
                """
                SELECT id, command, cwd, exit_code, executed_at
                FROM history
                WHERE command LIKE ? ESCAPE '\\'
                ORDER BY executed_at DESC
                LIMIT ?
                """,
                (pattern, self._history_limit),
            )
        # Empty buffer: return recent history for ranking
        return await self._storage.fetchall(
            """
            SELECT id, command, cwd, exit_code, executed_at
            FROM history
            ORDER BY executed_at DESC
            LIMIT ?
            """,
            (self._history_limit,),
        )

    @staticmethod
    def _abbreviate(command: str, max_len: int = 80) -> str:
        """Shorten a command for display if needed."""
        if len(command) <= max_len:
            return command
        return command[: max_len - 1] + "…"


__all__ = ["Completion", "HistoryRanker"]
