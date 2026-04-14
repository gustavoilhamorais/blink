"""Persistent storage layer.

Uses aiosqlite for async SQLite access. Stores:
- Shell command history with metadata
- Output blocks and their AI annotations
- AI provider configurations (encrypted credentials handled by security module)
- User preferences and session state
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import aiosqlite

# Default database location
_DEFAULT_DB_PATH = Path.home() / ".blink" / "blink.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    kitty_window_id INTEGER,
    cwd         TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    last_active TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    prompt      TEXT NOT NULL DEFAULT '',
    command     TEXT NOT NULL DEFAULT '',
    output      TEXT NOT NULL DEFAULT '',
    cwd         TEXT NOT NULL DEFAULT '',
    exit_code   INTEGER,
    started_at  TEXT,
    ended_at    TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    cwd         TEXT NOT NULL DEFAULT '',
    exit_code   INTEGER,
    executed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blocks_session ON blocks(session_id);
CREATE INDEX IF NOT EXISTS idx_history_cwd    ON history(cwd);
CREATE INDEX IF NOT EXISTS idx_history_cmd    ON history(command);
"""


class Storage:
    """Async SQLite storage wrapper built on aiosqlite.

    Typical usage::

        storage = Storage()
        await storage.init_db()
        await storage.execute("INSERT INTO history ...")
        rows = await storage.fetchall("SELECT * FROM history")
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = Path(os.environ.get("BLINK_DB_PATH", str(_DEFAULT_DB_PATH)))
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init_db(self) -> None:
        """Create tables if they do not exist, then keep connection open."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _ensure_open(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage not initialised — call await storage.init_db() first.")
        return self._db

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        """Execute a write statement (INSERT / UPDATE / DELETE)."""
        db = self._ensure_open()
        await db.execute(sql, params)
        await db.commit()

    async def executemany(
        self, sql: str, params_seq: list[tuple[Any, ...] | list[Any]]
    ) -> None:
        """Execute a write statement with multiple parameter sets."""
        db = self._ensure_open()
        await db.executemany(sql, params_seq)
        await db.commit()

    async def fetchall(
        self, sql: str, params: tuple[Any, ...] | list[Any] = ()
    ) -> list[dict[str, Any]]:
        """Execute a SELECT and return all rows as dicts."""
        db = self._ensure_open()
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def fetchone(
        self, sql: str, params: tuple[Any, ...] | list[Any] = ()
    ) -> dict[str, Any] | None:
        """Execute a SELECT and return the first row as a dict, or None."""
        db = self._ensure_open()
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Storage:
        await self.init_db()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


__all__ = ["Storage"]
