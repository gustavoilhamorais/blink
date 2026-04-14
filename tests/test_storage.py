"""Tests for the Storage module."""

from __future__ import annotations

import pytest

from blink.storage import Storage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage(tmp_path):
    """Provide an in-memory-like Storage backed by a temp file."""
    db_file = tmp_path / "test.db"
    s = Storage(db_path=db_file)
    await s.init_db()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


class TestStorageSchema:
    async def test_tables_created(self, storage: Storage) -> None:
        rows = await storage.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {r["name"] for r in rows}
        assert "sessions" in names
        assert "blocks" in names
        assert "history" in names

    async def test_indexes_created(self, storage: Storage) -> None:
        rows = await storage.fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        )
        names = {r["name"] for r in rows}
        assert "idx_blocks_session" in names
        assert "idx_history_cwd" in names
        assert "idx_history_cmd" in names


# ---------------------------------------------------------------------------
# Sessions table
# ---------------------------------------------------------------------------


class TestSessionsTable:
    async def test_insert_and_fetch_session(self, storage: Storage) -> None:
        await storage.execute(
            "INSERT INTO sessions (id, kitty_window_id, cwd, created_at, last_active) "
            "VALUES (?, ?, ?, ?, ?)",
            ("sess-1", 42, "/home/user", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
        )
        row = await storage.fetchone("SELECT * FROM sessions WHERE id = ?", ("sess-1",))
        assert row is not None
        assert row["id"] == "sess-1"
        assert row["kitty_window_id"] == 42
        assert row["cwd"] == "/home/user"

    async def test_fetchall_empty(self, storage: Storage) -> None:
        rows = await storage.fetchall("SELECT * FROM sessions")
        assert rows == []


# ---------------------------------------------------------------------------
# Blocks table
# ---------------------------------------------------------------------------


class TestBlocksTable:
    async def test_insert_block(self, storage: Storage) -> None:
        # Create parent session first
        await storage.execute(
            "INSERT INTO sessions (id, kitty_window_id, cwd, created_at, last_active) VALUES (?,?,?,?,?)",
            ("s1", None, "/tmp", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
        )
        await storage.execute(
            "INSERT INTO blocks (id, session_id, command, output, cwd, exit_code, started_at, ended_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("b1", "s1", "ls -la", "file.txt", "/tmp", 0, "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:01+00:00"),
        )
        row = await storage.fetchone("SELECT * FROM blocks WHERE id = ?", ("b1",))
        assert row is not None
        assert row["command"] == "ls -la"
        assert row["exit_code"] == 0

    async def test_block_requires_valid_fields(self, storage: Storage) -> None:
        # id is PRIMARY KEY — duplicate inserts should be a no-op via ON CONFLICT or raise
        await storage.execute(
            "INSERT INTO sessions (id, kitty_window_id, cwd, created_at, last_active) VALUES (?,?,?,?,?)",
            ("s2", None, "/", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
        )
        await storage.execute(
            "INSERT INTO blocks (id, session_id, command, output, cwd, exit_code) "
            "VALUES (?,?,?,?,?,?)",
            ("dup-id", "s2", "ls", "", "/", 0),
        )
        # Inserting the same id again should raise IntegrityError (UNIQUE constraint)
        import aiosqlite

        with pytest.raises(aiosqlite.IntegrityError):
            await storage.execute(
                "INSERT INTO blocks (id, session_id, command, output, cwd, exit_code) "
                "VALUES (?,?,?,?,?,?)",
                ("dup-id", "s2", "ls", "", "/", 0),
            )


# ---------------------------------------------------------------------------
# History table
# ---------------------------------------------------------------------------


class TestHistoryTable:
    async def test_insert_and_fetch_history(self, storage: Storage) -> None:
        await storage.execute(
            "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
            ("git status", "/repo", 0, "2024-01-01T00:00:00+00:00"),
        )
        rows = await storage.fetchall("SELECT * FROM history")
        assert len(rows) == 1
        assert rows[0]["command"] == "git status"

    async def test_history_autoincrement_id(self, storage: Storage) -> None:
        for i in range(3):
            await storage.execute(
                "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
                (f"cmd{i}", "/", 0, "2024-01-01T00:00:00+00:00"),
            )
        rows = await storage.fetchall("SELECT id FROM history ORDER BY id")
        ids = [r["id"] for r in rows]
        assert ids == [1, 2, 3]


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


class TestStorageHelpers:
    async def test_fetchone_returns_none_when_missing(self, storage: Storage) -> None:
        result = await storage.fetchone("SELECT * FROM sessions WHERE id = ?", ("nope",))
        assert result is None

    async def test_execute_raises_when_not_initialised(self) -> None:
        s = Storage(db_path=":memory:")
        with pytest.raises(RuntimeError, match="not initialised"):
            await s.execute("SELECT 1")

    async def test_context_manager(self, tmp_path) -> None:
        db_file = tmp_path / "ctx.db"
        async with Storage(db_path=db_file) as s:
            await s.execute(
                "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
                ("pwd", "/", 0, "2024-01-01T00:00:00+00:00"),
            )
            rows = await s.fetchall("SELECT * FROM history")
            assert len(rows) == 1

    async def test_executemany(self, storage: Storage) -> None:
        params_seq = [
            ("cmd1", "/a", 0, "2024-01-01T00:00:00+00:00"),
            ("cmd2", "/b", 1, "2024-01-01T00:00:01+00:00"),
            ("cmd3", "/c", 0, "2024-01-01T00:00:02+00:00"),
        ]
        await storage.executemany(
            "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
            params_seq,
        )
        rows = await storage.fetchall("SELECT * FROM history ORDER BY id")
        assert len(rows) == 3
        assert rows[1]["command"] == "cmd2"
