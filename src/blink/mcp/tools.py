"""MCP tool definitions and implementations.

Tools are split into three tiers that map to the security capability model:

- READ tools   (OBSERVE)  — inspect terminal state, never modify anything
- BUFFER tools (SUGGEST)  — modify the prompt / command-line buffer
- WRITE tools  (ACT)      — execute commands, send signals, write to stdin
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

from blink.daemon.app import BlinkDaemon
from blink.kitty.rc_client import KittyRCClient

# ---------------------------------------------------------------------------
# Tool schema definitions
# ---------------------------------------------------------------------------

READ_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_active_session",
        "description": "Get the current active terminal session.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_sessions",
        "description": "List all known terminal sessions.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_blocks",
        "description": "List recent command blocks for a session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID to query."},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of blocks to return (default 20).",
                    "default": 20,
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_block",
        "description": "Get a specific command block by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "block_id": {"type": "string", "description": "The block UUID."},
            },
            "required": ["block_id"],
        },
    },
    {
        "name": "get_visible_screen",
        "description": "Get the current visible content of a terminal window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "integer",
                    "description": "Kitty window ID.  Defaults to the focused window.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_selection",
        "description": "Get the currently selected text in a terminal window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "integer",
                    "description": "Kitty window ID.  Defaults to the focused window.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_prompt_buffer",
        "description": "Get the current command-line buffer (text the user is typing).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID."},
            },
            "required": [],
        },
    },
]

BUFFER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "replace_prompt_buffer",
        "description": "Replace the entire current command-line buffer with new text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "New command-line content."},
                "window_id": {
                    "type": "integer",
                    "description": "Kitty window ID.  Defaults to the focused window.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "insert_at_cursor",
        "description": "Insert text at the current cursor position in the command line.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to insert."},
                "window_id": {
                    "type": "integer",
                    "description": "Kitty window ID.  Defaults to the focused window.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "accept_completion",
        "description": "Accept a suggested shell completion (sends TAB).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "integer",
                    "description": "Kitty window ID.  Defaults to the focused window.",
                },
            },
            "required": [],
        },
    },
]

WRITE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "description": "Execute a shell command in a new subprocess.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Command string to execute."},
                "cwd": {
                    "type": "string",
                    "description": "Working directory.  Defaults to the current directory.",
                },
                "env": {
                    "type": "object",
                    "description": "Extra environment variables to set.",
                    "additionalProperties": {"type": "string"},
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds before forcibly killing the process (default 30).",
                    "default": 30,
                },
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "write_stdin",
        "description": "Write text to stdin of the running command in a terminal window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to write."},
                "window_id": {
                    "type": "integer",
                    "description": "Kitty window ID.  Defaults to the focused window.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "send_signal",
        "description": "Send a POSIX signal to a process by PID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "Target process ID."},
                "signal": {
                    "type": "string",
                    "description": "Signal name (e.g. 'SIGTERM', 'SIGKILL').  Default: SIGTERM.",
                    "default": "SIGTERM",
                },
            },
            "required": ["pid"],
        },
    },
    {
        "name": "cancel_command",
        "description": "Cancel the currently running command in a terminal (sends Ctrl+C).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_id": {
                    "type": "integer",
                    "description": "Kitty window ID.  Defaults to the focused window.",
                },
            },
            "required": [],
        },
    },
]

ALL_TOOLS: list[dict[str, Any]] = READ_TOOLS + BUFFER_TOOLS + WRITE_TOOLS


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Executes MCP tool calls, delegating to daemon / Kitty RC client."""

    def __init__(
        self,
        daemon: BlinkDaemon,
        kitty_client: KittyRCClient | None = None,
    ) -> None:
        self._daemon = daemon
        self._kitty = kitty_client

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------

    async def get_active_session(self, **_kwargs: Any) -> dict[str, Any]:
        """Return the most recently active session."""
        sessions = await self._daemon._storage.fetchall(
            "SELECT * FROM sessions ORDER BY last_active DESC LIMIT 1"
        )
        if not sessions:
            return {"session": None}
        return {"session": sessions[0]}

    async def list_sessions(self, **_kwargs: Any) -> dict[str, Any]:
        """Return all known sessions."""
        sessions = await self._daemon._storage.fetchall(
            "SELECT * FROM sessions ORDER BY last_active DESC"
        )
        return {"sessions": sessions}

    async def list_blocks(
        self,
        session_id: str,
        limit: int = 20,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Return recent blocks for *session_id*."""
        blocks = await self._daemon.get_recent_blocks(session_id=session_id, limit=limit)
        return {"blocks": blocks, "session_id": session_id}

    async def get_block(self, block_id: str, **_kwargs: Any) -> dict[str, Any]:
        """Return a single block by its UUID."""
        row = await self._daemon._storage.fetchone(
            "SELECT * FROM blocks WHERE id = ?", (block_id,)
        )
        if row is None:
            return {"block": None, "error": f"Block '{block_id}' not found."}
        return {"block": row}

    async def get_visible_screen(
        self,
        window_id: int | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Return visible screen content from Kitty."""
        if self._kitty is None:
            return {"content": None, "error": "Kitty RC client not available."}
        wid = window_id or await self._get_focused_window_id()
        if wid is None:
            return {"content": None, "error": "Could not determine Kitty window ID."}
        text = await self._kitty.get_text(wid, extent="screen")
        return {"content": text, "window_id": wid}

    async def get_selection(
        self,
        window_id: int | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Return the current selection in a Kitty window."""
        if self._kitty is None:
            return {"selection": None, "error": "Kitty RC client not available."}
        wid = window_id or await self._get_focused_window_id()
        if wid is None:
            return {"selection": None, "error": "Could not determine Kitty window ID."}
        # Kitty doesn't have a dedicated get-selection command; we approximate
        # using get-text with extent=selection if supported, else return empty.
        try:
            text = await self._kitty.get_text(wid, extent="selection")
            return {"selection": text, "window_id": wid}
        except Exception as exc:  # noqa: BLE001
            return {"selection": "", "window_id": wid, "note": str(exc)}

    async def get_prompt_buffer(
        self,
        session_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Return the current prompt buffer (last output line from visible screen)."""
        if self._kitty is None:
            return {"buffer": None, "error": "Kitty RC client not available."}

        # Attempt to resolve a window_id from the session
        wid: int | None = None
        if session_id:
            row = await self._daemon._storage.fetchone(
                "SELECT kitty_window_id FROM sessions WHERE id = ?", (session_id,)
            )
            if row and row.get("kitty_window_id"):
                wid = int(row["kitty_window_id"])

        wid = wid or await self._get_focused_window_id()
        if wid is None:
            return {"buffer": None, "error": "Could not determine Kitty window ID."}

        screen = await self._kitty.get_text(wid, extent="screen")
        # The prompt is typically on the last non-empty line
        lines = [line for line in screen.splitlines() if line.strip()]
        buffer = lines[-1].strip() if lines else ""
        return {"buffer": buffer, "window_id": wid}

    # ------------------------------------------------------------------
    # Buffer tools
    # ------------------------------------------------------------------

    async def replace_prompt_buffer(
        self,
        text: str,
        window_id: int | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Replace the entire command-line buffer.

        Implementation: Ctrl+U clears the line, then we send the new text.
        """
        if self._kitty is None:
            return {"ok": False, "error": "Kitty RC client not available."}
        wid = window_id or await self._get_focused_window_id()
        if wid is None:
            return {"ok": False, "error": "Could not determine Kitty window ID."}
        # Clear current line with Ctrl+U then type the new buffer
        await self._kitty.send_text(wid, "\x15")  # Ctrl+U
        await self._kitty.send_text(wid, text)
        return {"ok": True, "window_id": wid}

    async def insert_at_cursor(
        self,
        text: str,
        window_id: int | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Insert text at the current cursor position."""
        if self._kitty is None:
            return {"ok": False, "error": "Kitty RC client not available."}
        wid = window_id or await self._get_focused_window_id()
        if wid is None:
            return {"ok": False, "error": "Could not determine Kitty window ID."}
        await self._kitty.send_text(wid, text)
        return {"ok": True, "window_id": wid}

    async def accept_completion(
        self,
        window_id: int | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Send TAB to accept the current shell completion."""
        if self._kitty is None:
            return {"ok": False, "error": "Kitty RC client not available."}
        wid = window_id or await self._get_focused_window_id()
        if wid is None:
            return {"ok": False, "error": "Could not determine Kitty window ID."}
        await self._kitty.send_text(wid, "\t")
        return {"ok": True, "window_id": wid}

    # ------------------------------------------------------------------
    # Write tools
    # ------------------------------------------------------------------

    async def run_command(
        self,
        cmd: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Execute *cmd* in a subprocess and return stdout/stderr/exit_code."""
        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                shell=True,  # noqa: S602
                cwd=cwd,
                env=run_env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "cmd": cmd,
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "error": f"Command timed out after {timeout}s.",
                "cmd": cmd,
            }
        except Exception as exc:  # noqa: BLE001
            return {"exit_code": -1, "error": str(exc), "cmd": cmd}

    async def write_stdin(
        self,
        text: str,
        window_id: int | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Send *text* to the running process's stdin via Kitty send-text."""
        if self._kitty is None:
            return {"ok": False, "error": "Kitty RC client not available."}
        wid = window_id or await self._get_focused_window_id()
        if wid is None:
            return {"ok": False, "error": "Could not determine Kitty window ID."}
        await self._kitty.send_text(wid, text)
        return {"ok": True, "window_id": wid}

    async def send_signal(
        self,
        pid: int,
        signal_name: str = "SIGTERM",
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Send a POSIX signal to *pid*."""
        try:
            sig = getattr(signal, signal_name.upper(), None)
            if sig is None:
                return {"ok": False, "error": f"Unknown signal: '{signal_name}'."}
            os.kill(pid, sig)
            return {"ok": True, "pid": pid, "signal": signal_name}
        except ProcessLookupError:
            return {"ok": False, "error": f"No process with PID {pid}."}
        except PermissionError:
            return {"ok": False, "error": f"Permission denied to signal PID {pid}."}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    async def cancel_command(
        self,
        window_id: int | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Send Ctrl+C to the terminal window to cancel the running command."""
        if self._kitty is None:
            return {"ok": False, "error": "Kitty RC client not available."}
        wid = window_id or await self._get_focused_window_id()
        if wid is None:
            return {"ok": False, "error": "Could not determine Kitty window ID."}
        await self._kitty.send_text(wid, "\x03")  # ETX = Ctrl+C
        return {"ok": True, "window_id": wid}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_focused_window_id(self) -> int | None:
        """Try to determine the ID of the currently focused Kitty window."""
        if self._kitty is None:
            return None
        try:
            windows = await self._kitty.ls()
            for os_win in windows:
                for tab in os_win.get("tabs", []):
                    for win in tab.get("windows", []):
                        if win.get("is_focused"):
                            return int(win["id"])
        except Exception:  # noqa: BLE001
            pass
        return None

    # ------------------------------------------------------------------
    # Dispatch entry point
    # ------------------------------------------------------------------

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch a tool call by *name* with *arguments*."""
        method = getattr(self, name, None)
        if method is None:
            raise ValueError(f"Unknown tool: '{name}'")
        return await method(**arguments)


__all__ = ["ALL_TOOLS", "READ_TOOLS", "BUFFER_TOOLS", "WRITE_TOOLS", "ToolExecutor"]
