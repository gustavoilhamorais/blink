"""Tests for the MCP server protocol handling."""

from __future__ import annotations

import json

import pytest

from blink.daemon.app import BlinkDaemon
from blink.mcp.server import AuditLogger, MCPServer
from blink.security.capabilities import Capability, SecurityPolicy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def daemon(tmp_path):
    """Minimal daemon with initialised storage (no socket)."""
    db = tmp_path / "test.db"
    d = BlinkDaemon(socket_path=tmp_path / "test.sock", db_path=db)
    await d._storage.init_db()
    yield d
    await d._storage.close()


@pytest.fixture
def observe_server(daemon):
    """MCPServer with OBSERVE policy (read-only)."""
    policy = SecurityPolicy(granted_capability=Capability.OBSERVE)
    return MCPServer(daemon=daemon, policy=policy)


@pytest.fixture
def act_server(daemon):
    """MCPServer with ACT policy, no confirmation required."""
    policy = SecurityPolicy(
        granted_capability=Capability.ACT,
        require_confirmation=False,
    )
    return MCPServer(daemon=daemon, policy=policy)


# ---------------------------------------------------------------------------
# handle_initialize
# ---------------------------------------------------------------------------


class TestHandleInitialize:
    async def test_returns_protocol_version(self, observe_server: MCPServer) -> None:
        result = await observe_server.handle_initialize({"clientInfo": {"name": "test"}})
        assert "protocolVersion" in result
        assert result["protocolVersion"] == MCPServer._PROTOCOL_VERSION

    async def test_returns_server_info(self, observe_server: MCPServer) -> None:
        result = await observe_server.handle_initialize({})
        assert result["serverInfo"]["name"] == "blink-mcp"

    async def test_capabilities_include_tools_and_resources(
        self, observe_server: MCPServer
    ) -> None:
        result = await observe_server.handle_initialize({})
        caps = result["capabilities"]
        assert "tools" in caps
        assert "resources" in caps

    async def test_sets_initialized_flag(self, observe_server: MCPServer) -> None:
        assert observe_server._initialized is False
        await observe_server.handle_initialize({})
        assert observe_server._initialized is True

    async def test_stores_client_info(self, observe_server: MCPServer) -> None:
        await observe_server.handle_initialize({"clientInfo": {"name": "myagent"}})
        assert observe_server._client_info["name"] == "myagent"


# ---------------------------------------------------------------------------
# handle_tools_list
# ---------------------------------------------------------------------------


class TestHandleToolsList:
    async def test_returns_tools_key(self, observe_server: MCPServer) -> None:
        result = await observe_server.handle_tools_list()
        assert "tools" in result

    async def test_all_tools_have_name_and_description(
        self, observe_server: MCPServer
    ) -> None:
        result = await observe_server.handle_tools_list()
        for tool in result["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool

    async def test_includes_read_tools(self, observe_server: MCPServer) -> None:
        result = await observe_server.handle_tools_list()
        names = {t["name"] for t in result["tools"]}
        assert "get_active_session" in names
        assert "list_blocks" in names

    async def test_includes_write_tools(self, observe_server: MCPServer) -> None:
        result = await observe_server.handle_tools_list()
        names = {t["name"] for t in result["tools"]}
        assert "run_command" in names
        assert "cancel_command" in names

    async def test_includes_buffer_tools(self, observe_server: MCPServer) -> None:
        result = await observe_server.handle_tools_list()
        names = {t["name"] for t in result["tools"]}
        assert "replace_prompt_buffer" in names
        assert "insert_at_cursor" in names


# ---------------------------------------------------------------------------
# handle_tools_call (observe-level tools only, no Kitty)
# ---------------------------------------------------------------------------


class TestHandleToolsCall:
    async def test_get_active_session_no_sessions(
        self, observe_server: MCPServer
    ) -> None:
        result = await observe_server.handle_tools_call("get_active_session", {})
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert content["session"] is None

    async def test_get_active_session_returns_session(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        await daemon.register_session(cwd="/home/user")
        result = await observe_server.handle_tools_call("get_active_session", {})
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert content["session"] is not None
        assert content["session"]["cwd"] == "/home/user"

    async def test_list_sessions(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        await daemon.register_session(cwd="/a")
        await daemon.register_session(cwd="/b")
        result = await observe_server.handle_tools_call("list_sessions", {})
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert len(content["sessions"]) == 2

    async def test_list_blocks(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/project")
        await daemon.record_block(sess["id"], {"command": "make", "exit_code": 0})
        result = await observe_server.handle_tools_call(
            "list_blocks", {"session_id": sess["id"], "limit": 10}
        )
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert len(content["blocks"]) == 1
        assert content["blocks"][0]["command"] == "make"

    async def test_get_block_found(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/")
        await daemon.record_block(
            sess["id"], {"id": "block-abc", "command": "ls", "exit_code": 0}
        )
        result = await observe_server.handle_tools_call(
            "get_block", {"block_id": "block-abc"}
        )
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert content["block"]["command"] == "ls"

    async def test_get_block_not_found(self, observe_server: MCPServer) -> None:
        result = await observe_server.handle_tools_call(
            "get_block", {"block_id": "nonexistent"}
        )
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert content["block"] is None

    async def test_act_tool_denied_with_observe_policy(
        self, observe_server: MCPServer
    ) -> None:
        with pytest.raises(PermissionError):
            await observe_server.handle_tools_call("run_command", {"cmd": "ls"})

    async def test_run_command_allowed_with_act_policy(
        self, act_server: MCPServer
    ) -> None:
        result = await act_server.handle_tools_call("run_command", {"cmd": "echo hello"})
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert "hello" in content["stdout"]
        assert content["exit_code"] == 0

    async def test_run_command_captures_stderr(self, act_server: MCPServer) -> None:
        result = await act_server.handle_tools_call(
            "run_command", {"cmd": "echo error >&2"}
        )
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert content["exit_code"] == 0

    async def test_run_command_nonzero_exit(self, act_server: MCPServer) -> None:
        result = await act_server.handle_tools_call(
            "run_command", {"cmd": "exit 42"}
        )
        assert result["isError"] is False
        content = json.loads(result["content"][0]["text"])
        assert content["exit_code"] == 42

    async def test_unknown_tool_returns_error(self, act_server: MCPServer) -> None:
        result = await act_server.handle_tools_call("nonexistent_tool", {})
        assert result["isError"] is True
        assert "Unknown tool" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# handle_resources_list
# ---------------------------------------------------------------------------


class TestHandleResourcesList:
    async def test_returns_resources_key(self, observe_server: MCPServer) -> None:
        result = await observe_server.handle_resources_list()
        assert "resources" in result

    async def test_resources_have_required_fields(
        self, observe_server: MCPServer
    ) -> None:
        result = await observe_server.handle_resources_list()
        for r in result["resources"]:
            assert "uri" in r
            assert "name" in r

    async def test_concrete_uris_when_sessions_exist(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/home/user")
        result = await observe_server.handle_resources_list()
        uris = [r["uri"] for r in result["resources"]]
        # At least one URI should contain the session ID
        assert any(sess["id"] in uri for uri in uris)


# ---------------------------------------------------------------------------
# handle_resources_read
# ---------------------------------------------------------------------------


class TestHandleResourcesRead:
    async def test_cwd_resource(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/home/user")
        uri = f"terminal://session/{sess['id']}/cwd"
        result = await observe_server.handle_resources_read(uri)
        assert result["contents"][0]["text"] == "/home/user"

    async def test_history_resource(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/project")
        await daemon.record_block(
            sess["id"], {"command": "make test", "exit_code": 0}
        )
        uri = f"terminal://session/{sess['id']}/history/recent"
        result = await observe_server.handle_resources_read(uri)
        blocks = json.loads(result["contents"][0]["text"])
        assert any(b["command"] == "make test" for b in blocks)

    async def test_env_resource_redacts_secrets(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        import os

        sess = await daemon.register_session(cwd="/")
        uri = f"terminal://session/{sess['id']}/env/redacted"
        # Temporarily inject a fake secret into the environment
        os.environ["__BLINK_TEST_API_KEY"] = "supersecret123"
        try:
            result = await observe_server.handle_resources_read(uri)
            env = json.loads(result["contents"][0]["text"])
            assert env.get("__BLINK_TEST_API_KEY") == "[REDACTED]"
        finally:
            del os.environ["__BLINK_TEST_API_KEY"]

    async def test_policy_resource(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/")
        uri = f"terminal://session/{sess['id']}/policy"
        result = await observe_server.handle_resources_read(uri)
        policy_data = json.loads(result["contents"][0]["text"])
        assert "granted_capability" in policy_data

    async def test_block_resource_not_found(
        self, observe_server: MCPServer, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/")
        uri = f"terminal://session/{sess['id']}/blocks/nonexistent"
        result = await observe_server.handle_resources_read(uri)
        content = json.loads(result["contents"][0]["text"])
        assert "error" in content

    async def test_unknown_resource_raises(self, observe_server: MCPServer) -> None:
        with pytest.raises(ValueError, match="Unknown resource URI"):
            await observe_server.handle_resources_read("terminal://session/abc/unknown")


# ---------------------------------------------------------------------------
# Message dispatch (JSON-RPC layer)
# ---------------------------------------------------------------------------


class TestMessageDispatch:
    async def test_initialize_method(self, observe_server: MCPServer) -> None:
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ).encode()
        resp = await observe_server._handle_message(msg)
        assert resp is not None
        assert resp["id"] == 1
        assert "result" in resp

    async def test_tools_list_method(self, observe_server: MCPServer) -> None:
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        ).encode()
        resp = await observe_server._handle_message(msg)
        assert resp is not None
        assert "tools" in resp["result"]

    async def test_unknown_method_returns_error(
        self, observe_server: MCPServer
    ) -> None:
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "foobar"}
        ).encode()
        resp = await observe_server._handle_message(msg)
        assert resp is not None
        assert "error" in resp

    async def test_invalid_json_returns_parse_error(
        self, observe_server: MCPServer
    ) -> None:
        resp = await observe_server._handle_message(b"{invalid json}")
        assert resp is not None
        assert resp["error"]["code"] == -32700

    async def test_ping_method(self, observe_server: MCPServer) -> None:
        msg = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}).encode()
        resp = await observe_server._handle_message(msg)
        assert resp is not None
        assert resp["result"] == {}

    async def test_notification_returns_none(self, observe_server: MCPServer) -> None:
        """Notifications (no id) should return None (no response)."""
        msg = json.dumps({"jsonrpc": "2.0", "method": "initialized"}).encode()
        resp = await observe_server._handle_message(msg)
        assert resp is None


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


class TestAuditLogger:
    async def test_log_creates_entry(self, daemon: BlinkDaemon) -> None:
        logger = AuditLogger(db=daemon._storage)
        await logger.log(
            event="tool_call_success",
            tool="list_sessions",
            arguments={},
            result="[]",
            allowed=True,
        )
        rows = await daemon._storage.fetchall("SELECT * FROM audit_log")
        assert len(rows) == 1
        assert rows[0]["tool"] == "list_sessions"
        assert rows[0]["allowed"] == 1

    async def test_log_denied_entry(self, daemon: BlinkDaemon) -> None:
        logger = AuditLogger(db=daemon._storage)
        await logger.log(
            event="tool_call_denied",
            tool="run_command",
            arguments={"cmd": "rm -rf /"},
            result="denied",
            allowed=False,
        )
        rows = await daemon._storage.fetchall("SELECT * FROM audit_log WHERE allowed = 0")
        assert len(rows) == 1
        assert rows[0]["event"] == "tool_call_denied"

    async def test_log_redacts_secrets_in_arguments(
        self, daemon: BlinkDaemon
    ) -> None:
        logger = AuditLogger(db=daemon._storage)
        await logger.log(
            event="tool_call_success",
            tool="run_command",
            arguments={"cmd": "curl -H 'Authorization: Bearer secrettoken12345'"},
            result="ok",
            allowed=True,
        )
        rows = await daemon._storage.fetchall("SELECT * FROM audit_log")
        assert "secrettoken12345" not in rows[0]["arguments"]

    async def test_multiple_logs(self, daemon: BlinkDaemon) -> None:
        logger = AuditLogger(db=daemon._storage)
        for i in range(5):
            await logger.log(f"event_{i}", f"tool_{i}", {}, "", True)
        rows = await daemon._storage.fetchall("SELECT * FROM audit_log")
        assert len(rows) == 5
