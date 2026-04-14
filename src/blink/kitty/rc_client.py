"""Kitty remote control client.

Communicates with Kitty via Unix socket using the JSON-based remote control protocol.
Reference: https://sw.kovidgoyal.net/kitty/rc_protocol/
"""

from __future__ import annotations

import json
import os
import struct
from types import TracebackType
from typing import Any

import anyio
import anyio.abc
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

# Magic bytes that prefix every Kitty RC message
_MAGIC = b"\x1bP@kitty-cmd"
_MAGIC_END = b"\x1b\\"


def _encode_cmd(cmd: dict[str, Any]) -> bytes:
    """Encode a command dict into Kitty's wire format."""
    payload = json.dumps(cmd).encode()
    # Kitty RC protocol: ESC P @ kitty-cmd <length> <payload> ESC \\
    length = struct.pack(">I", len(payload))
    return _MAGIC + length + payload + _MAGIC_END


def _decode_response(data: bytes) -> dict[str, Any]:
    """Decode a response from Kitty's wire format."""
    # Strip magic if present
    if data.startswith(_MAGIC):
        data = data[len(_MAGIC):]
        if data.startswith(b"\x00\x00\x00"):
            # skip 4-byte length prefix
            data = data[4:]
        if data.endswith(_MAGIC_END):
            data = data[: -len(_MAGIC_END)]
    result: dict[str, Any] = json.loads(data.decode())
    return result


class KittyRCClient:
    """Async client for Kitty remote control via Unix socket.

    Usage::

        async with KittyRCClient() as client:
            windows = await client.ls()
    """

    def __init__(self, socket_path: str | None = None) -> None:
        # Kitty exposes its RC socket at $KITTY_LISTEN_ON or a well-known path
        if socket_path is None:
            socket_path = os.environ.get("KITTY_LISTEN_ON", "")
            if socket_path.startswith("unix:"):
                socket_path = socket_path[5:]
        self._socket_path = socket_path
        self._stream: anyio.abc.ByteStream | None = None
        self._cmd_id: int = 0

    # ------------------------------------------------------------------
    # Context manager helpers
    # ------------------------------------------------------------------

    async def __aenter__(self) -> KittyRCClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the Unix socket connection to Kitty."""
        if not self._socket_path:
            raise RuntimeError(
                "No Kitty socket path configured. "
                "Set KITTY_LISTEN_ON or pass socket_path to KittyRCClient."
            )
        self._stream = await anyio.connect_unix(self._socket_path)

    async def close(self) -> None:
        """Close the connection."""
        if self._stream is not None:
            await self._stream.aclose()
            self._stream = None

    # ------------------------------------------------------------------
    # Low-level send/receive
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    async def _send_command(self, cmd: str, **kwargs: Any) -> dict[str, Any]:
        """Send a command and return the parsed response."""
        if self._stream is None:
            raise RuntimeError("Not connected — call connect() first or use async with.")

        payload: dict[str, Any] = {"cmd": cmd, "version": [0, 14, 0], "id": self._next_id()}
        if kwargs:
            payload["payload"] = kwargs

        wire = _encode_cmd(payload)
        await self._stream.send(wire)

        # Read response: accumulate until we find the end marker
        buf = b""
        while _MAGIC_END not in buf:
            chunk = await self._stream.receive(4096)
            if not chunk:
                break
            buf += chunk

        return _decode_response(buf)

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def ls(self) -> list[dict[str, Any]]:
        """List all windows and tabs.

        Returns a list of OS window dicts, each containing a ``tabs`` key.
        """
        resp = await self._send_command("ls")
        data = resp.get("data", [])
        if isinstance(data, str):
            # Kitty returns JSON-encoded string inside the data field
            parsed: list[dict[str, Any]] = json.loads(data)
            return parsed
        result: list[dict[str, Any]] = data
        return result

    async def get_text(self, window_id: int, extent: str = "screen") -> str:
        """Get text from a Kitty window.

        Args:
            window_id: The numeric Kitty window ID.
            extent: One of ``screen``, ``first_cmd_output_on_screen``,
                    ``last_cmd_output``, ``all``.

        Returns:
            The text content as a string.
        """
        resp = await self._send_command("get-text", match=f"id:{window_id}", extent=extent)
        data = resp.get("data", "")
        if isinstance(data, str):
            return data
        return str(data)

    async def send_text(self, window_id: int, text: str) -> None:
        """Send text to a Kitty window as if typed.

        Args:
            window_id: The numeric Kitty window ID.
            text: The text to send.
        """
        await self._send_command("send-text", match=f"id:{window_id}", data=text)

    async def launch(self, cmd: str | list[str], cwd: str | None = None) -> dict[str, Any]:
        """Launch a new process/window in Kitty.

        Args:
            cmd: Command to run (string or list of arguments).
            cwd: Optional working directory.

        Returns:
            Response dict from Kitty, typically containing the new window id.
        """
        kwargs: dict[str, Any] = {}
        if isinstance(cmd, list):
            kwargs["args"] = cmd
        else:
            kwargs["args"] = [cmd]
        if cwd is not None:
            kwargs["cwd"] = cwd
        return await self._send_command("launch", **kwargs)

    async def focus_window(self, window_id: int) -> None:
        """Bring a Kitty window into focus.

        Args:
            window_id: The numeric Kitty window ID.
        """
        await self._send_command("focus-window", match=f"id:{window_id}")

    async def scroll_window(self, window_id: int, amount: int) -> None:
        """Scroll a Kitty window.

        Args:
            window_id: The numeric Kitty window ID.
            amount: Number of lines to scroll (positive = down, negative = up).
        """
        await self._send_command(
            "scroll-window",
            match=f"id:{window_id}",
            amount=f"{abs(amount)}l",
            # Kitty's scroll-window uses signed amounts as strings like "5l" (lines)
        )


# ---------------------------------------------------------------------------
# Module-level helper so callers can iterate without managing a client
# ---------------------------------------------------------------------------


async def list_windows(socket_path: str | None = None) -> list[dict[str, Any]]:
    """Convenience wrapper: open a transient client, call ls(), return results."""
    async with KittyRCClient(socket_path=socket_path) as client:
        return await client.ls()


__all__ = ["KittyRCClient", "list_windows"]


# Keep a lightweight send/receive pair for tests that need to mock the stream
class _MockStream:
    """Very thin stand-in stream used by unit tests."""

    def __init__(
        self,
        send_stream: MemoryObjectSendStream[bytes],
        receive_stream: MemoryObjectReceiveStream[bytes],
    ) -> None:
        self._send = send_stream
        self._recv = receive_stream

    async def send(self, data: bytes) -> None:
        await self._send.send(data)

    async def receive(self, max_bytes: int = 4096) -> bytes:
        return await self._recv.receive()

    async def aclose(self) -> None:
        await self._send.aclose()
        await self._recv.aclose()
