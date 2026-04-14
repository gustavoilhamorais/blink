"""Blink background daemon.

The daemon runs as a long-lived process that:
- Maintains a Unix socket for IPC with shell integrations / kittens
- Persists session and block data to SQLite via the Storage module
- Publishes an in-process event bus for future extensions
- Handles IPC commands: register_session, record_block, get_recent_blocks, get_history
"""

from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import anyio.abc
from anyio import create_unix_listener

from blink.storage import Storage

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BLINK_DIR = Path(os.environ.get("BLINK_DIR", Path.home() / ".blink"))
_SOCKET_PATH = _BLINK_DIR / "blink.sock"
_DB_PATH = _BLINK_DIR / "blink.db"
_PID_PATH = _BLINK_DIR / "blink.pid"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Event bus (simple in-process pub/sub)
# ---------------------------------------------------------------------------


class EventBus:
    """Minimal synchronous pub/sub for daemon internals."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def subscribe(self, event: str, callback: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.setdefault(event, []).append(callback)

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        for cb in self._listeners.get(event, []):
            try:
                cb(data or {})
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# BlinkDaemon
# ---------------------------------------------------------------------------


class BlinkDaemon:
    """The Blink session daemon.

    Listens on a Unix socket, manages SQLite state, and exposes a simple
    JSON-lines RPC protocol over the socket.
    """

    def __init__(
        self,
        socket_path: Path | str | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        self._socket_path = Path(socket_path or _SOCKET_PATH)
        self._db_path = Path(db_path or _DB_PATH)
        self._storage: Storage = Storage(db_path=self._db_path)
        self._bus = EventBus()
        self._sessions: dict[str, dict[str, Any]] = {}  # in-memory registry
        self._running = False

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise storage and begin serving the Unix socket."""
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove stale socket file
        if self._socket_path.exists():
            self._socket_path.unlink()

        await self._storage.init_db()
        self._running = True

        # Write PID file
        _PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PID_PATH.write_text(str(os.getpid()))

        self._bus.publish("daemon_started", {"pid": os.getpid()})

        async with await create_unix_listener(str(self._socket_path)) as listener:
            await listener.serve(self._handle_client)

    async def stop(self) -> None:
        """Gracefully stop the daemon."""
        self._running = False
        await self._storage.close()
        if self._socket_path.exists():
            self._socket_path.unlink()
        if _PID_PATH.exists():
            _PID_PATH.unlink()

    # ------------------------------------------------------------------
    # IPC protocol
    # ------------------------------------------------------------------

    async def _handle_client(self, stream: anyio.abc.ByteStream) -> None:
        """Read newline-delimited JSON commands and send JSON responses."""
        buf = b""
        try:
            async for chunk in stream:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line.decode())
                        response = await self._dispatch(request)
                    except Exception as exc:  # noqa: BLE001
                        response = {"ok": False, "error": str(exc)}
                    await stream.send((json.dumps(response) + "\n").encode())
        except Exception:  # noqa: BLE001
            pass
        finally:
            await stream.aclose()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        """Route an IPC request to the appropriate handler."""
        cmd = request.get("cmd", "")
        params: dict[str, Any] = request.get("params", {})

        handlers: dict[str, Callable[..., Any]] = {
            "register_session": self.register_session,
            "record_block": self.record_block,
            "get_recent_blocks": self.get_recent_blocks,
            "get_history": self.get_history,
            "ping": self._ping,
        }

        handler = handlers.get(cmd)
        if handler is None:
            return {"ok": False, "error": f"Unknown command: {cmd!r}"}

        result = await handler(**params)
        return {"ok": True, "data": result}

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    async def _ping(self) -> str:
        return "pong"

    async def register_session(
        self, window_id: int | None = None, cwd: str = "", session_id: str | None = None
    ) -> dict[str, Any]:
        """Register a new terminal session.

        Called by shell integration on startup.
        Returns the session dict with generated id.
        """
        import uuid

        sid = session_id or str(uuid.uuid4())
        now = _now_iso()
        await self._storage.execute(
            """
            INSERT INTO sessions (id, kitty_window_id, cwd, created_at, last_active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_active = excluded.last_active
            """,
            (sid, window_id, cwd, now, now),
        )
        session = {"id": sid, "kitty_window_id": window_id, "cwd": cwd, "created_at": now}
        self._sessions[sid] = session
        self._bus.publish("session_registered", session)
        return session

    async def record_block(
        self, session_id: str, block_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist a completed command block.

        Also writes to the history table for completion ranking.
        Called when a command finishes (OSC 133;D received).
        """
        import uuid

        block_id = block_data.get("id", str(uuid.uuid4()))
        now = _now_iso()

        await self._storage.execute(
            """
            INSERT INTO blocks
                (id, session_id, prompt, command, output, cwd, exit_code, started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                block_id,
                session_id,
                block_data.get("prompt", ""),
                block_data.get("command", ""),
                block_data.get("output", ""),
                block_data.get("cwd", ""),
                block_data.get("exit_code"),
                block_data.get("started_at"),
                block_data.get("ended_at", now),
            ),
        )

        command = block_data.get("command", "").strip()
        if command:
            await self._storage.execute(
                "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?, ?, ?, ?)",
                (command, block_data.get("cwd", ""), block_data.get("exit_code"), now),
            )

        # Update session last_active
        await self._storage.execute(
            "UPDATE sessions SET last_active = ? WHERE id = ?",
            (now, session_id),
        )

        self._bus.publish("block_recorded", {"block_id": block_id, "session_id": session_id})
        return {"block_id": block_id}

    async def get_recent_blocks(
        self, session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the most recent blocks for a session."""
        return await self._storage.fetchall(
            """
            SELECT id, session_id, prompt, command, output, cwd, exit_code, started_at, ended_at
            FROM blocks
            WHERE session_id = ?
            ORDER BY ended_at DESC
            LIMIT ?
            """,
            (session_id, limit),
        )

    async def get_history(self, cwd: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Return command history, optionally filtered by cwd.

        Commands run in *cwd* are sorted first (directory-local relevance),
        then all others by recency.
        """
        if cwd:
            return await self._storage.fetchall(
                """
                SELECT id, command, cwd, exit_code, executed_at
                FROM history
                ORDER BY (cwd = ?) DESC, executed_at DESC
                LIMIT ?
                """,
                (cwd, limit),
            )
        return await self._storage.fetchall(
            "SELECT id, command, cwd, exit_code, executed_at FROM history ORDER BY executed_at DESC LIMIT ?",
            (limit,),
        )


# ---------------------------------------------------------------------------
# Entry-point for subprocess launch
# ---------------------------------------------------------------------------


def _setup_signal_handlers(daemon: BlinkDaemon) -> None:
    """Install SIGTERM/SIGINT handlers to shut down gracefully."""

    def _handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        anyio.from_thread.run_sync(daemon.stop)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


async def run_daemon(
    socket_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Run the daemon until interrupted."""
    daemon = BlinkDaemon(socket_path=socket_path, db_path=db_path)
    try:
        await daemon.start()
    finally:
        await daemon.stop()


def main() -> None:
    """Synchronous entry point used by ``blink daemon start``."""
    anyio.run(run_daemon)


__all__ = ["BlinkDaemon", "EventBus", "run_daemon", "main"]
