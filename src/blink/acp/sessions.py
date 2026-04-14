"""ACP Session Management.

Maps ACP sessions to Blink terminal sessions and maintains context
(cwd, env, history, large output references) across multiple agent runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from blink.daemon.app import BlinkDaemon
from blink.security.redaction import redact_env
from blink.storage import Storage

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


# Compaction limits
CONTEXT_HISTORY_LIMIT: int = 50
CONTEXT_LARGE_OUTPUT_LIMIT: int = 20


class SessionContext(BaseModel):
    """Context maintained across runs in a session."""

    cwd: str = ""
    """Current working directory."""

    env: dict[str, str] = Field(default_factory=dict)
    """Redacted environment variables (secrets replaced with [REDACTED])."""

    history: list[str] = Field(default_factory=list)
    """Recent shell commands (newest first, capped at 50)."""

    large_outputs: list[str] = Field(default_factory=list)
    """MCP resource URIs for large outputs moved out of context."""


class ACPSession(BaseModel):
    """Maps an ACP session to a Blink terminal session."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    blink_session_id: str
    """Foreign key into the daemon's sessions table."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    context: SessionContext = Field(default_factory=SessionContext)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS acp_sessions (
    id                TEXT PRIMARY KEY,
    blink_session_id  TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    context_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_acp_sessions_blink ON acp_sessions(blink_session_id);
"""


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Manages ACP sessions and their terminal context.

    Sessions are keyed by *blink_session_id* — there is at most one ACP
    session per Blink terminal session.
    """

    def __init__(self, storage: Storage, daemon: BlinkDaemon) -> None:
        self._storage = storage
        self._daemon = daemon
        self._sessions: dict[str, ACPSession] = {}  # keyed by ACP session id
        self._by_blink: dict[str, str] = {}  # blink_session_id -> ACP session id
        self._schema_ready = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_schema(self) -> None:
        if not self._schema_ready:
            db = self._storage._ensure_open()
            await db.executescript(_SESSIONS_SCHEMA)
            await db.commit()
            self._schema_ready = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_session(self, blink_session_id: str) -> ACPSession:
        """Create a new ACP session for the given Blink session.

        Hydrates context from the daemon (cwd, history, env).
        """
        await self._ensure_schema()
        context = await self._build_context(blink_session_id)
        session = ACPSession(blink_session_id=blink_session_id, context=context)
        self._sessions[session.id] = session
        self._by_blink[blink_session_id] = session.id
        await self._persist(session)
        return session

    async def get_or_create(self, blink_session_id: str) -> ACPSession:
        """Return the existing ACP session for a Blink session, or create one."""
        if blink_session_id in self._by_blink:
            sid = self._by_blink[blink_session_id]
            return self._sessions[sid]

        await self._ensure_schema()
        # Check storage
        row = await self._storage.fetchone(
            "SELECT * FROM acp_sessions WHERE blink_session_id = ? ORDER BY created_at DESC LIMIT 1",
            (blink_session_id,),
        )
        if row is not None:
            session = self._row_to_session(row)
            self._sessions[session.id] = session
            self._by_blink[blink_session_id] = session.id
            return session

        return await self.create_session(blink_session_id)

    async def get_by_id(self, session_id: str) -> ACPSession | None:
        """Return an ACP session by its own ID."""
        if session_id in self._sessions:
            return self._sessions[session_id]
        await self._ensure_schema()
        row = await self._storage.fetchone(
            "SELECT * FROM acp_sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            return None
        session = self._row_to_session(row)
        self._sessions[session.id] = session
        self._by_blink[session.blink_session_id] = session.id
        return session

    async def update_context(self, session_id: str, context: SessionContext) -> None:
        """Replace the context for a session (e.g. after command execution)."""
        session = await self._require(session_id)
        session.context = context
        session.updated_at = datetime.now(tz=UTC)
        await self._persist(session)

    async def refresh_context(self, session_id: str) -> SessionContext:
        """Re-hydrate context from the daemon (picks up new cwd / history)."""
        session = await self._require(session_id)
        context = await self._build_context(session.blink_session_id)
        # Preserve large_outputs references accumulated over the session
        context.large_outputs = list(session.context.large_outputs)
        await self.update_context(session_id, context)
        return context

    async def compact_context(self, session_id: str) -> None:
        """Move large outputs to MCP resource URIs to keep context size bounded.

        Any ``large_outputs`` entry that was previously stored in history is
        replaced with a ``terminal://`` resource reference. The context's
        ``history`` list is trimmed to the most recent 50 entries.
        """
        session = await self._require(session_id)
        ctx = session.context

        # Trim history
        ctx.history = ctx.history[: CONTEXT_HISTORY_LIMIT]

        # Cap large output references
        ctx.large_outputs = ctx.large_outputs[: CONTEXT_LARGE_OUTPUT_LIMIT]

        await self.update_context(session_id, ctx)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _require(self, session_id: str) -> ACPSession:
        session = await self.get_by_id(session_id)
        if session is None:
            raise KeyError(f"ACP session {session_id!r} not found.")
        return session

    async def _build_context(self, blink_session_id: str) -> SessionContext:
        """Build a SessionContext by querying the daemon for cwd, env, history."""
        import os

        # CWD from daemon sessions table
        row = await self._daemon._storage.fetchone(
            "SELECT cwd FROM sessions WHERE id = ?", (blink_session_id,)
        )
        cwd = row.get("cwd", "") if row else ""

        # Recent history (commands only, newest first)
        history_rows = await self._daemon._storage.fetchall(
            """
            SELECT command FROM history
            ORDER BY executed_at DESC
            LIMIT ?
            """,
            (CONTEXT_HISTORY_LIMIT,),
        )
        history = [r["command"] for r in history_rows if r.get("command")]

        # Redacted environment
        env = redact_env(dict(os.environ))

        return SessionContext(cwd=cwd, env=env, history=history)

    async def _persist(self, session: ACPSession) -> None:
        await self._storage.execute(
            """
            INSERT INTO acp_sessions (id, blink_session_id, created_at, updated_at, context_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at   = excluded.updated_at,
                context_json = excluded.context_json
            """,
            (
                session.id,
                session.blink_session_id,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                session.context.model_dump_json(),
            ),
        )

    @staticmethod
    def _row_to_session(row: dict[str, Any]) -> ACPSession:
        ctx: SessionContext
        try:
            ctx = SessionContext.model_validate_json(row.get("context_json") or "{}")
        except Exception:  # noqa: BLE001
            ctx = SessionContext()

        return ACPSession(
            id=row["id"],
            blink_session_id=row["blink_session_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            context=ctx,
        )


__all__ = ["ACPSession", "SessionContext", "SessionManager"]
