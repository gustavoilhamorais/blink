"""Terminal MCP Server.

Implements the Model Context Protocol (MCP) specification
(https://modelcontextprotocol.io/specification/2025-06-18) and exposes
Blink's terminal capabilities to AI agents via:

- **stdio** transport  — for local agents launched as sub-processes
- **HTTP/SSE** transport — for remote agents (basic implementation)

Protocol: JSON-RPC 2.0 over newline-delimited messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import anyio  # noqa: F401 — keep for potential future use in HTTP transport

from blink.daemon.app import BlinkDaemon
from blink.kitty.rc_client import KittyRCClient
from blink.mcp.tools import ALL_TOOLS, ToolExecutor
from blink.security.capabilities import CapabilityChecker, SecurityPolicy
from blink.security.redaction import redact_env, redact_secrets
from blink.storage import Storage

# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event     TEXT NOT NULL,
    tool      TEXT NOT NULL,
    arguments TEXT,
    result    TEXT,
    allowed   INTEGER NOT NULL
);
"""


class AuditLogger:
    """Append-only audit log stored in the Blink SQLite database."""

    def __init__(self, db: Storage) -> None:
        self._db = db
        self._initialized = False

    async def _ensure_table(self) -> None:
        if not self._initialized:
            await self._db.execute(_AUDIT_SCHEMA)
            self._initialized = True

    async def log(
        self,
        event: str,
        tool: str,
        arguments: dict[str, Any],
        result: str,
        allowed: bool,
    ) -> None:
        """Persist an audit entry.

        Secrets in *arguments* are redacted before storage.
        """
        await self._ensure_table()
        safe_args = {
            k: redact_secrets(str(v)) if isinstance(v, str) else v
            for k, v in arguments.items()
        }
        await self._db.execute(
            """
            INSERT INTO audit_log (timestamp, event, tool, arguments, result, allowed)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(tz=UTC).isoformat(),
                event,
                tool,
                json.dumps(safe_args),
                result[:4096] if result else "",  # truncate huge outputs
                1 if allowed else 0,
            ),
        )


# ---------------------------------------------------------------------------
# MCP resource definitions
# ---------------------------------------------------------------------------

_RESOURCES: list[dict[str, Any]] = [
    {
        "uri": "terminal://session/{id}/screen",
        "name": "Screen content",
        "description": "Current visible content of the terminal window.",
        "mimeType": "text/plain",
    },
    {
        "uri": "terminal://session/{id}/prompt",
        "name": "Current prompt",
        "description": "The active command-line buffer.",
        "mimeType": "text/plain",
    },
    {
        "uri": "terminal://session/{id}/blocks/{block_id}",
        "name": "Block content",
        "description": "A specific command block (prompt, output, exit code).",
        "mimeType": "application/json",
    },
    {
        "uri": "terminal://session/{id}/history/recent",
        "name": "Recent commands",
        "description": "The most recent shell commands executed in this session.",
        "mimeType": "application/json",
    },
    {
        "uri": "terminal://session/{id}/env/redacted",
        "name": "Environment (redacted)",
        "description": "Process environment variables with secrets replaced by [REDACTED].",
        "mimeType": "application/json",
    },
    {
        "uri": "terminal://session/{id}/cwd",
        "name": "Current directory",
        "description": "The current working directory of the session.",
        "mimeType": "text/plain",
    },
    {
        "uri": "terminal://session/{id}/git/status",
        "name": "Git status",
        "description": "Output of `git status --short` in the session CWD.",
        "mimeType": "text/plain",
    },
    {
        "uri": "terminal://session/{id}/policy",
        "name": "Current security policy",
        "description": "Active MCP security policy (capability grants, confirmation settings).",
        "mimeType": "application/json",
    },
]

# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _ok(req_id: int | str | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(
    req_id: int | str | None,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_NOT_AUTHORIZED = -32001  # custom


# ---------------------------------------------------------------------------
# MCPServer
# ---------------------------------------------------------------------------


class MCPServer:
    """MCP server exposing terminal capabilities.

    Supports both stdio (for local agents) and HTTP transports.
    """

    # Protocol version we advertise
    _PROTOCOL_VERSION = "2024-11-05"
    _SERVER_INFO = {"name": "blink-mcp", "version": "0.1.0"}

    def __init__(
        self,
        daemon: BlinkDaemon,
        kitty_client: KittyRCClient | None = None,
        policy: SecurityPolicy | None = None,
    ) -> None:
        self._daemon = daemon
        self._kitty = kitty_client
        self._policy = policy or SecurityPolicy()
        self._checker = CapabilityChecker(self._policy)
        self._executor = ToolExecutor(daemon=daemon, kitty_client=kitty_client)
        self._audit = AuditLogger(db=daemon._storage)
        self._initialized = False
        self._client_info: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, transport: str = "stdio") -> None:
        """Start the MCP server on the selected *transport*.

        Args:
            transport: ``"stdio"`` reads from stdin and writes to stdout.
                       ``"http"`` is not yet implemented (raises NotImplementedError).
        """
        if transport == "stdio":
            await self._serve_stdio()
        elif transport == "http":
            raise NotImplementedError("HTTP transport is not yet implemented.")
        else:
            raise ValueError(f"Unknown transport: {transport!r}")

    async def stop(self) -> None:
        """Gracefully stop the server (no-op for stdio)."""

    # ------------------------------------------------------------------
    # stdio transport
    # ------------------------------------------------------------------

    async def _serve_stdio(self) -> None:
        """Read newline-delimited JSON-RPC messages from stdin, respond to stdout."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_event_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

        writer_transport, writer_protocol = await loop.connect_write_pipe(
            asyncio.BaseProtocol, sys.stdout.buffer
        )

        async def write_msg(msg: dict[str, Any]) -> None:
            line = (json.dumps(msg) + "\n").encode()
            writer_transport.write(line)

        buf = b""
        while True:
            try:
                chunk = await reader.read(4096)
            except Exception:  # noqa: BLE001
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                response = await self._handle_message(line)
                if response is not None:
                    await write_msg(response)

    # ------------------------------------------------------------------
    # Message dispatcher
    # ------------------------------------------------------------------

    async def _handle_message(self, raw: bytes) -> dict[str, Any] | None:
        """Parse a raw JSON-RPC message and dispatch to the right handler."""
        try:
            msg = json.loads(raw.decode())
        except json.JSONDecodeError as exc:
            return _error(None, _PARSE_ERROR, f"Parse error: {exc}")

        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        # Notifications (no id) are fire-and-forget
        is_notification = "id" not in msg

        try:
            result = await self._dispatch(method, params)
        except PermissionError as exc:
            resp = _error(req_id, _NOT_AUTHORIZED, str(exc))
            return None if is_notification else resp
        except ValueError as exc:
            resp = _error(req_id, _INVALID_PARAMS, str(exc))
            return None if is_notification else resp
        except Exception as exc:  # noqa: BLE001
            resp = _error(req_id, _INTERNAL_ERROR, f"Internal error: {exc}")
            return None if is_notification else resp

        if is_notification:
            return None
        return _ok(req_id, result)

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Route a JSON-RPC method to its handler."""
        if method == "initialize":
            return await self.handle_initialize(params)
        if method == "initialized":
            # Client notification — nothing to do
            return None
        if method == "tools/list":
            return await self.handle_tools_list()
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            return await self.handle_tools_call(name, arguments)
        if method == "resources/list":
            return await self.handle_resources_list()
        if method == "resources/read":
            uri = params.get("uri", "")
            return await self.handle_resources_read(uri)
        if method == "ping":
            return {}
        raise ValueError(f"Method not found: '{method}'")

    # ------------------------------------------------------------------
    # MCP protocol handlers
    # ------------------------------------------------------------------

    async def handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ``initialize`` — exchange capabilities with the client."""
        self._client_info = params.get("clientInfo") or {}
        self._initialized = True
        return {
            "protocolVersion": self._PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "logging": {},
            },
            "serverInfo": self._SERVER_INFO,
            "instructions": (
                "This MCP server exposes Blink terminal capabilities. "
                "Read tools observe terminal state; write tools execute commands "
                "and require the ACT capability. "
                "All tool calls are audit-logged."
            ),
        }

    async def handle_tools_list(self) -> dict[str, Any]:
        """Return the list of all available tools."""
        return {"tools": ALL_TOOLS}

    async def handle_tools_call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a tool call, enforcing capability policy and audit logging.

        Returns a dict with a ``content`` list as required by the MCP spec.
        """
        # Security check
        allowed, reason = await self._checker.check(name, arguments)
        result_str = ""
        try:
            if not allowed:
                await self._audit.log(
                    event="tool_call_denied",
                    tool=name,
                    arguments=arguments,
                    result=reason or "denied",
                    allowed=False,
                )
                raise PermissionError(reason or f"Tool '{name}' is not allowed.")

            # Execute the tool
            result = await self._executor.execute(name, arguments)
            result_str = json.dumps(result) if not isinstance(result, str) else result

            await self._audit.log(
                event="tool_call_success",
                tool=name,
                arguments=arguments,
                result=result_str,
                allowed=True,
            )

            return {
                "content": [
                    {
                        "type": "text",
                        "text": result_str,
                    }
                ],
                "isError": False,
            }
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc)
            await self._audit.log(
                event="tool_call_error",
                tool=name,
                arguments=arguments,
                result=error_msg,
                allowed=True,
            )
            return {
                "content": [{"type": "text", "text": error_msg}],
                "isError": True,
            }

    async def handle_resources_list(self) -> dict[str, Any]:
        """Return the list of available resources (with concrete session IDs if known)."""
        resources: list[dict[str, Any]] = []

        # Fetch all sessions to produce concrete URIs where possible
        sessions = await self._daemon._storage.fetchall(
            "SELECT id FROM sessions ORDER BY last_active DESC"
        )

        if sessions:
            for session in sessions:
                sid = session["id"]
                for template in _RESOURCES:
                    uri = template["uri"].replace("{id}", sid)
                    # Leave {block_id} as a literal placeholder
                    resources.append({**template, "uri": uri})
        else:
            # No sessions yet — return templates as-is
            resources = list(_RESOURCES)

        return {"resources": resources}

    async def handle_resources_read(self, uri: str) -> dict[str, Any]:
        """Fetch the content of a resource identified by *uri*."""
        content = await self._resolve_resource(uri)
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": content.get("mimeType", "text/plain"),
                    "text": content.get("text", ""),
                }
            ]
        }

    # ------------------------------------------------------------------
    # Resource resolvers
    # ------------------------------------------------------------------

    async def _resolve_resource(self, uri: str) -> dict[str, Any]:
        """Dispatch resource URI to the correct resolver."""
        # terminal://session/{id}/screen
        if uri.endswith("/screen"):
            session_id = self._extract_session_id(uri, "/screen")
            return await self._resource_screen(session_id)

        # terminal://session/{id}/prompt
        if uri.endswith("/prompt"):
            session_id = self._extract_session_id(uri, "/prompt")
            return await self._resource_prompt(session_id)

        # terminal://session/{id}/blocks/{block_id}
        if "/blocks/" in uri:
            parts = uri.rstrip("/").split("/")
            block_id = parts[-1]
            session_id = self._extract_session_id(uri, f"/blocks/{block_id}")
            return await self._resource_block(session_id, block_id)

        # terminal://session/{id}/history/recent
        if uri.endswith("/history/recent"):
            session_id = self._extract_session_id(uri, "/history/recent")
            return await self._resource_history(session_id)

        # terminal://session/{id}/env/redacted
        if uri.endswith("/env/redacted"):
            session_id = self._extract_session_id(uri, "/env/redacted")
            return await self._resource_env(session_id)

        # terminal://session/{id}/cwd
        if uri.endswith("/cwd"):
            session_id = self._extract_session_id(uri, "/cwd")
            return await self._resource_cwd(session_id)

        # terminal://session/{id}/git/status
        if uri.endswith("/git/status"):
            session_id = self._extract_session_id(uri, "/git/status")
            return await self._resource_git_status(session_id)

        # terminal://session/{id}/policy
        if uri.endswith("/policy"):
            return await self._resource_policy()

        raise ValueError(f"Unknown resource URI: '{uri}'")

    @staticmethod
    def _extract_session_id(uri: str, suffix: str) -> str:
        """Extract the session ID from a terminal:// URI."""
        # URI format: terminal://session/{id}<suffix>
        prefix = "terminal://session/"
        if uri.startswith(prefix):
            rest = uri[len(prefix):]
            return rest[: -len(suffix)] if suffix and rest.endswith(suffix) else rest
        return uri

    async def _resource_screen(self, session_id: str) -> dict[str, Any]:
        if self._kitty is None:
            return {"mimeType": "text/plain", "text": "[Kitty RC client not available]"}
        row = await self._daemon._storage.fetchone(
            "SELECT kitty_window_id FROM sessions WHERE id = ?", (session_id,)
        )
        wid = row.get("kitty_window_id") if row else None
        if wid is None:
            return {"mimeType": "text/plain", "text": "[No Kitty window for this session]"}
        text = await self._kitty.get_text(int(wid), extent="screen")
        return {"mimeType": "text/plain", "text": text}

    async def _resource_prompt(self, session_id: str) -> dict[str, Any]:
        result = await self._executor.get_prompt_buffer(session_id=session_id)
        return {"mimeType": "text/plain", "text": result.get("buffer") or ""}

    async def _resource_block(self, session_id: str, block_id: str) -> dict[str, Any]:
        row = await self._daemon._storage.fetchone(
            "SELECT * FROM blocks WHERE id = ? AND session_id = ?",
            (block_id, session_id),
        )
        text = json.dumps(row) if row else json.dumps({"error": "Block not found"})
        return {"mimeType": "application/json", "text": text}

    async def _resource_history(self, session_id: str) -> dict[str, Any]:
        blocks = await self._daemon.get_recent_blocks(session_id=session_id, limit=50)
        return {"mimeType": "application/json", "text": json.dumps(blocks)}

    async def _resource_env(self, _session_id: str) -> dict[str, Any]:
        # Expose a redacted view of the current process environment
        redacted = redact_env(dict(os.environ))
        return {"mimeType": "application/json", "text": json.dumps(redacted)}

    async def _resource_cwd(self, session_id: str) -> dict[str, Any]:
        row = await self._daemon._storage.fetchone(
            "SELECT cwd FROM sessions WHERE id = ?", (session_id,)
        )
        cwd = row.get("cwd", "") if row else ""
        return {"mimeType": "text/plain", "text": cwd}

    async def _resource_git_status(self, session_id: str) -> dict[str, Any]:
        row = await self._daemon._storage.fetchone(
            "SELECT cwd FROM sessions WHERE id = ?", (session_id,)
        )
        cwd = row.get("cwd", "") if row else None
        if not cwd:
            return {"mimeType": "text/plain", "text": "[No CWD for this session]"}
        try:
            result = subprocess.run(  # noqa: S603 S607
                ["git", "status", "--short"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return {"mimeType": "text/plain", "text": result.stdout or "(clean)"}
        except Exception as exc:  # noqa: BLE001
            return {"mimeType": "text/plain", "text": f"[git error: {exc}]"}

    async def _resource_policy(self) -> dict[str, Any]:
        return {
            "mimeType": "application/json",
            "text": self._policy.model_dump_json(indent=2),
        }


# ---------------------------------------------------------------------------
# Standalone run helper
# ---------------------------------------------------------------------------


async def run_server(
    transport: str = "stdio",
    socket_path: str | None = None,
    db_path: str | None = None,
    policy: SecurityPolicy | None = None,
) -> None:
    """Convenience coroutine: build a daemon + server and run until cancelled."""
    daemon = BlinkDaemon(
        socket_path=socket_path,
        db_path=db_path,
    )
    await daemon._storage.init_db()

    kitty_client: KittyRCClient | None = None
    try:
        kitty_client = KittyRCClient()
        await kitty_client.connect()
    except Exception:  # noqa: BLE001
        kitty_client = None  # Kitty not available — degrade gracefully

    server = MCPServer(daemon=daemon, kitty_client=kitty_client, policy=policy)
    try:
        await server.start(transport=transport)
    finally:
        await server.stop()
        if kitty_client is not None:
            await kitty_client.close()
        await daemon._storage.close()


__all__ = ["MCPServer", "AuditLogger", "run_server"]
