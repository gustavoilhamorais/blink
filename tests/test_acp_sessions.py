"""Tests for ACP session management (SessionManager + ACPSession)."""

from __future__ import annotations

import pytest

from blink.acp.sessions import (
    ACPSession,
    CONTEXT_HISTORY_LIMIT,
    CONTEXT_LARGE_OUTPUT_LIMIT,
    SessionContext,
    SessionManager,
)
from blink.daemon.app import BlinkDaemon
from blink.storage import Storage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage(tmp_path):
    db = tmp_path / "test.db"
    s = Storage(db_path=db)
    await s.init_db()
    yield s
    await s.close()


@pytest.fixture
async def daemon(tmp_path):
    db = tmp_path / "daemon.db"
    d = BlinkDaemon(socket_path=tmp_path / "test.sock", db_path=db)
    await d._storage.init_db()
    yield d
    await d._storage.close()


@pytest.fixture
async def session_manager(storage, daemon):
    return SessionManager(storage=storage, daemon=daemon)


# ---------------------------------------------------------------------------
# SessionContext model
# ---------------------------------------------------------------------------


class TestSessionContext:
    def test_default_values(self):
        ctx = SessionContext()
        assert ctx.cwd == ""
        assert ctx.env == {}
        assert ctx.history == []
        assert ctx.large_outputs == []

    def test_can_set_all_fields(self):
        ctx = SessionContext(
            cwd="/home/user",
            env={"PATH": "/usr/bin"},
            history=["ls", "cd /tmp"],
            large_outputs=["terminal://session/abc/blocks/123"],
        )
        assert ctx.cwd == "/home/user"
        assert ctx.env["PATH"] == "/usr/bin"
        assert "ls" in ctx.history
        assert len(ctx.large_outputs) == 1


# ---------------------------------------------------------------------------
# ACPSession model
# ---------------------------------------------------------------------------


class TestACPSession:
    def test_has_auto_id(self):
        session = ACPSession(blink_session_id="blink-1")
        assert session.id
        assert len(session.id) > 8

    def test_two_sessions_have_different_ids(self):
        s1 = ACPSession(blink_session_id="blink-1")
        s2 = ACPSession(blink_session_id="blink-2")
        assert s1.id != s2.id

    def test_has_default_context(self):
        session = ACPSession(blink_session_id="blink-1")
        assert isinstance(session.context, SessionContext)


# ---------------------------------------------------------------------------
# SessionManager.create_session
# ---------------------------------------------------------------------------


class TestSessionManagerCreateSession:
    async def test_creates_session(self, session_manager: SessionManager):
        session = await session_manager.create_session("blink-session-1")
        assert session.blink_session_id == "blink-session-1"

    async def test_session_has_context(self, session_manager: SessionManager):
        session = await session_manager.create_session("blink-session-1")
        assert isinstance(session.context, SessionContext)

    async def test_session_is_persisted(self, session_manager: SessionManager):
        session = await session_manager.create_session("blink-session-1")
        # Clear in-memory cache
        session_manager._sessions.clear()
        session_manager._by_blink.clear()
        fetched = await session_manager.get_by_id(session.id)
        assert fetched is not None
        assert fetched.id == session.id

    async def test_context_includes_cwd(self, session_manager: SessionManager, daemon: BlinkDaemon):
        """When a Blink session exists, context should include its cwd."""
        blink_session = await daemon.register_session(cwd="/home/user/project")
        session = await session_manager.create_session(blink_session["id"])
        assert session.context.cwd == "/home/user/project"

    async def test_context_has_history_if_available(
        self, session_manager: SessionManager, daemon: BlinkDaemon
    ):
        blink_session = await daemon.register_session(cwd="/")
        await daemon.record_block(blink_session["id"], {"command": "git status", "exit_code": 0})
        session = await session_manager.create_session(blink_session["id"])
        assert "git status" in session.context.history


# ---------------------------------------------------------------------------
# SessionManager.get_or_create
# ---------------------------------------------------------------------------


class TestSessionManagerGetOrCreate:
    async def test_creates_if_not_exists(self, session_manager: SessionManager):
        session = await session_manager.get_or_create("new-blink-session")
        assert session.blink_session_id == "new-blink-session"

    async def test_returns_existing_if_already_created(self, session_manager: SessionManager):
        s1 = await session_manager.get_or_create("blink-123")
        s2 = await session_manager.get_or_create("blink-123")
        assert s1.id == s2.id

    async def test_loads_from_storage_after_cache_clear(self, session_manager: SessionManager):
        s1 = await session_manager.create_session("blink-abc")
        # Evict from in-memory cache
        session_manager._sessions.clear()
        session_manager._by_blink.clear()
        s2 = await session_manager.get_or_create("blink-abc")
        assert s1.id == s2.id


# ---------------------------------------------------------------------------
# SessionManager.update_context
# ---------------------------------------------------------------------------


class TestSessionManagerUpdateContext:
    async def test_updates_context(self, session_manager: SessionManager):
        session = await session_manager.create_session("blink-1")
        new_ctx = SessionContext(cwd="/new/dir", history=["ls -la"])
        await session_manager.update_context(session.id, new_ctx)

        fetched = await session_manager.get_by_id(session.id)
        assert fetched is not None
        assert fetched.context.cwd == "/new/dir"
        assert "ls -la" in fetched.context.history

    async def test_raises_for_unknown_session(self, session_manager: SessionManager):
        ctx = SessionContext()
        with pytest.raises(KeyError):
            await session_manager.update_context("nonexistent-id", ctx)


# ---------------------------------------------------------------------------
# SessionManager.compact_context
# ---------------------------------------------------------------------------


class TestSessionManagerCompactContext:
    async def test_trims_history_to_limit(self, session_manager: SessionManager):
        session = await session_manager.create_session("blink-1")
        # Create context with more than 50 history entries
        long_history = [f"cmd_{i}" for i in range(60)]
        ctx = SessionContext(history=long_history)
        await session_manager.update_context(session.id, ctx)

        await session_manager.compact_context(session.id)

        fetched = await session_manager.get_by_id(session.id)
        assert fetched is not None
        assert len(fetched.context.history) <= CONTEXT_HISTORY_LIMIT

    async def test_trims_large_outputs_to_limit(self, session_manager: SessionManager):
        session = await session_manager.create_session("blink-1")
        # Create context with many large output references
        many_outputs = [f"terminal://session/s/blocks/{i}" for i in range(25)]
        ctx = SessionContext(large_outputs=many_outputs)
        await session_manager.update_context(session.id, ctx)

        await session_manager.compact_context(session.id)

        fetched = await session_manager.get_by_id(session.id)
        assert fetched is not None
        assert len(fetched.context.large_outputs) <= CONTEXT_LARGE_OUTPUT_LIMIT

    async def test_compact_idempotent_for_small_context(self, session_manager: SessionManager):
        session = await session_manager.create_session("blink-1")
        ctx = SessionContext(history=["ls", "pwd"], cwd="/home")
        await session_manager.update_context(session.id, ctx)

        await session_manager.compact_context(session.id)

        fetched = await session_manager.get_by_id(session.id)
        assert fetched is not None
        assert "ls" in fetched.context.history


# ---------------------------------------------------------------------------
# SessionManager.refresh_context
# ---------------------------------------------------------------------------


class TestSessionManagerRefreshContext:
    async def test_refresh_updates_cwd(
        self, session_manager: SessionManager, daemon: BlinkDaemon
    ):
        blink_session = await daemon.register_session(cwd="/initial")
        session = await session_manager.create_session(blink_session["id"])
        assert session.context.cwd == "/initial"

        # Update cwd in daemon
        await daemon._storage.execute(
            "UPDATE sessions SET cwd = ? WHERE id = ?",
            ("/updated", blink_session["id"]),
        )

        ctx = await session_manager.refresh_context(session.id)
        assert ctx.cwd == "/updated"

    async def test_refresh_preserves_large_outputs(
        self, session_manager: SessionManager, daemon: BlinkDaemon
    ):
        blink_session = await daemon.register_session(cwd="/")
        session = await session_manager.create_session(blink_session["id"])

        # Add a large output reference
        ctx = session.context
        ctx.large_outputs = ["terminal://session/x/blocks/1"]
        await session_manager.update_context(session.id, ctx)

        # Refresh should preserve large_outputs
        refreshed_ctx = await session_manager.refresh_context(session.id)
        assert "terminal://session/x/blocks/1" in refreshed_ctx.large_outputs
