"""Tests for the Blink daemon."""

from __future__ import annotations

import json

import anyio
import pytest

from blink.daemon.app import BlinkDaemon

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def daemon(tmp_path):
    """Start a daemon on a temp socket / db for the duration of a test."""
    sock = tmp_path / "test.sock"
    db = tmp_path / "test.db"
    d = BlinkDaemon(socket_path=sock, db_path=db)
    # Initialise storage only (don't start the socket server for unit tests)
    await d._storage.init_db()
    yield d
    await d._storage.close()


# ---------------------------------------------------------------------------
# Session registration
# ---------------------------------------------------------------------------


class TestRegisterSession:
    async def test_register_returns_session(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(window_id=1, cwd="/home/user")
        assert "id" in sess
        assert sess["kitty_window_id"] == 1
        assert sess["cwd"] == "/home/user"

    async def test_register_persists_to_db(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(window_id=2, cwd="/tmp")
        row = await daemon._storage.fetchone(
            "SELECT * FROM sessions WHERE id = ?", (sess["id"],)
        )
        assert row is not None
        assert row["cwd"] == "/tmp"

    async def test_register_populates_in_memory_registry(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/opt")
        assert sess["id"] in daemon._sessions

    async def test_register_uses_provided_session_id(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(session_id="fixed-id-123", cwd="/")
        assert sess["id"] == "fixed-id-123"


# ---------------------------------------------------------------------------
# Record block
# ---------------------------------------------------------------------------


class TestRecordBlock:
    async def test_record_block_saved(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/project")
        block = {
            "command": "make build",
            "output": "Build OK",
            "cwd": "/project",
            "exit_code": 0,
            "started_at": "2024-01-01T00:00:00+00:00",
            "ended_at": "2024-01-01T00:00:05+00:00",
        }
        result = await daemon.record_block(sess["id"], block)
        assert "block_id" in result

        row = await daemon._storage.fetchone(
            "SELECT * FROM blocks WHERE id = ?", (result["block_id"],)
        )
        assert row is not None
        assert row["command"] == "make build"
        assert row["exit_code"] == 0

    async def test_record_block_writes_history(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/repo")
        await daemon.record_block(
            sess["id"], {"command": "git log", "cwd": "/repo", "exit_code": 0}
        )
        rows = await daemon._storage.fetchall("SELECT * FROM history WHERE command = ?", ("git log",))
        assert len(rows) == 1

    async def test_record_empty_command_skips_history(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/")
        await daemon.record_block(sess["id"], {"command": "", "cwd": "/", "exit_code": 0})
        rows = await daemon._storage.fetchall("SELECT * FROM history")
        assert len(rows) == 0

    async def test_record_block_preserves_id(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/")
        block = {"id": "my-block-id", "command": "ls", "cwd": "/", "exit_code": 0}
        result = await daemon.record_block(sess["id"], block)
        assert result["block_id"] == "my-block-id"


# ---------------------------------------------------------------------------
# Get recent blocks
# ---------------------------------------------------------------------------


class TestGetRecentBlocks:
    async def test_returns_blocks_for_session(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/")
        for i in range(5):
            await daemon.record_block(
                sess["id"], {"command": f"cmd{i}", "cwd": "/", "exit_code": 0}
            )
        blocks = await daemon.get_recent_blocks(sess["id"], limit=10)
        assert len(blocks) == 5

    async def test_limit_is_respected(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/")
        for i in range(10):
            await daemon.record_block(
                sess["id"], {"command": f"cmd{i}", "cwd": "/", "exit_code": 0}
            )
        blocks = await daemon.get_recent_blocks(sess["id"], limit=3)
        assert len(blocks) == 3

    async def test_returns_empty_for_unknown_session(self, daemon: BlinkDaemon) -> None:
        blocks = await daemon.get_recent_blocks("nonexistent", limit=10)
        assert blocks == []


# ---------------------------------------------------------------------------
# Get history
# ---------------------------------------------------------------------------


class TestGetHistory:
    async def test_get_history_all(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/a")
        await daemon.record_block(sess["id"], {"command": "ls", "cwd": "/a", "exit_code": 0})
        await daemon.record_block(sess["id"], {"command": "pwd", "cwd": "/b", "exit_code": 0})
        history = await daemon.get_history(limit=10)
        cmds = [h["command"] for h in history]
        assert "ls" in cmds
        assert "pwd" in cmds

    async def test_get_history_cwd_sorted_first(self, daemon: BlinkDaemon) -> None:
        sess = await daemon.register_session(cwd="/project")
        await daemon.record_block(sess["id"], {"command": "make", "cwd": "/project", "exit_code": 0})
        await daemon.record_block(sess["id"], {"command": "ls", "cwd": "/tmp", "exit_code": 0})
        history = await daemon.get_history(cwd="/project", limit=10)
        # "make" (run in /project) should sort before "ls" (run in /tmp)
        assert history[0]["command"] == "make"


# ---------------------------------------------------------------------------
# IPC dispatch
# ---------------------------------------------------------------------------


class TestIPCDispatch:
    async def test_ping(self, daemon: BlinkDaemon) -> None:
        resp = await daemon._dispatch({"cmd": "ping", "params": {}})
        assert resp["ok"] is True
        assert resp["data"] == "pong"

    async def test_unknown_command(self, daemon: BlinkDaemon) -> None:
        resp = await daemon._dispatch({"cmd": "foobar", "params": {}})
        assert resp["ok"] is False
        assert "Unknown command" in resp["error"]

    async def test_dispatch_register_session(self, daemon: BlinkDaemon) -> None:
        resp = await daemon._dispatch(
            {"cmd": "register_session", "params": {"cwd": "/home/user"}}
        )
        assert resp["ok"] is True
        assert "id" in resp["data"]


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------


class TestEventBus:
    def test_subscribe_and_publish(self) -> None:
        from blink.daemon.app import EventBus

        bus = EventBus()
        received: list[dict] = []
        bus.subscribe("test_event", received.append)
        bus.publish("test_event", {"key": "value"})
        assert received == [{"key": "value"}]

    def test_unsubscribed_event_no_error(self) -> None:
        from blink.daemon.app import EventBus

        bus = EventBus()
        # Should not raise
        bus.publish("nonexistent_event", {})

    def test_multiple_subscribers(self) -> None:
        from blink.daemon.app import EventBus

        bus = EventBus()
        log1: list[dict] = []
        log2: list[dict] = []
        bus.subscribe("ev", log1.append)
        bus.subscribe("ev", log2.append)
        bus.publish("ev", {"x": 1})
        assert log1 == [{"x": 1}]
        assert log2 == [{"x": 1}]

    def test_callback_exception_does_not_propagate(self) -> None:
        from blink.daemon.app import EventBus

        bus = EventBus()

        def bad_cb(data: dict) -> None:
            raise RuntimeError("boom")

        bus.subscribe("ev", bad_cb)
        # Should not raise
        bus.publish("ev", {})


# ---------------------------------------------------------------------------
# Socket server integration test
# ---------------------------------------------------------------------------


class TestDaemonSocket:
    async def test_start_stop_server(self, tmp_path) -> None:
        """Start a real daemon socket, send a ping, verify response."""
        sock = tmp_path / "integration.sock"
        db = tmp_path / "integration.db"
        daemon = BlinkDaemon(socket_path=sock, db_path=db)

        received: list[str] = []

        async def run_server_and_client() -> None:
            async with anyio.create_task_group() as tg:
                # Start daemon in the background
                tg.start_soon(daemon.start)

                # Wait for socket to appear
                for _ in range(40):
                    if sock.exists():
                        break
                    await anyio.sleep(0.05)

                # Connect and send ping
                try:
                    async with await anyio.connect_unix(str(sock)) as stream:
                        await stream.send(b'{"cmd":"ping","params":{}}\n')
                        buf = b""
                        while b"\n" not in buf:
                            chunk = await stream.receive(256)
                            if not chunk:
                                break
                            buf += chunk
                        received.append(buf.split(b"\n")[0].decode())
                finally:
                    # Cancel the server task group
                    tg.cancel_scope.cancel()

        await run_server_and_client()
        await daemon.stop()

        assert len(received) >= 1
        resp = json.loads(received[0])
        assert resp["ok"] is True
        assert resp["data"] == "pong"
