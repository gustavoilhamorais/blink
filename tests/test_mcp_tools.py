"""Tests for MCP tool implementations (ToolExecutor)."""

from __future__ import annotations

import pytest

from blink.daemon.app import BlinkDaemon
from blink.mcp.tools import ALL_TOOLS, BUFFER_TOOLS, READ_TOOLS, WRITE_TOOLS, ToolExecutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def daemon(tmp_path):
    db = tmp_path / "test.db"
    d = BlinkDaemon(socket_path=tmp_path / "test.sock", db_path=db)
    await d._storage.init_db()
    yield d
    await d._storage.close()


@pytest.fixture
def executor(daemon):
    """ToolExecutor without a Kitty client (Kitty tests are skipped)."""
    return ToolExecutor(daemon=daemon, kitty_client=None)


# ---------------------------------------------------------------------------
# Tool schema sanity
# ---------------------------------------------------------------------------


class TestToolSchemas:
    def test_all_tools_have_name(self) -> None:
        for tool in ALL_TOOLS:
            assert "name" in tool and tool["name"]

    def test_all_tools_have_description(self) -> None:
        for tool in ALL_TOOLS:
            assert "description" in tool and tool["description"]

    def test_all_tools_have_input_schema(self) -> None:
        for tool in ALL_TOOLS:
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_read_tools_count(self) -> None:
        assert len(READ_TOOLS) == 7

    def test_buffer_tools_count(self) -> None:
        assert len(BUFFER_TOOLS) == 3

    def test_write_tools_count(self) -> None:
        assert len(WRITE_TOOLS) == 4

    def test_tool_names_are_unique(self) -> None:
        names = [t["name"] for t in ALL_TOOLS]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Read tool implementations
# ---------------------------------------------------------------------------


class TestGetActiveSession:
    async def test_no_sessions(self, executor: ToolExecutor) -> None:
        result = await executor.get_active_session()
        assert result["session"] is None

    async def test_returns_most_recent_session(
        self, executor: ToolExecutor, daemon: BlinkDaemon
    ) -> None:
        await daemon.register_session(cwd="/old")
        await daemon.register_session(cwd="/new")
        result = await executor.get_active_session()
        assert result["session"] is not None
        # Most recently active
        assert result["session"]["cwd"] in ("/old", "/new")


class TestListSessions:
    async def test_empty(self, executor: ToolExecutor) -> None:
        result = await executor.list_sessions()
        assert result["sessions"] == []

    async def test_lists_all_sessions(
        self, executor: ToolExecutor, daemon: BlinkDaemon
    ) -> None:
        await daemon.register_session(cwd="/a")
        await daemon.register_session(cwd="/b")
        result = await executor.list_sessions()
        assert len(result["sessions"]) == 2


class TestListBlocks:
    async def test_returns_blocks_for_session(
        self, executor: ToolExecutor, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/project")
        await daemon.record_block(sess["id"], {"command": "make", "exit_code": 0})
        result = await executor.list_blocks(session_id=sess["id"], limit=10)
        assert len(result["blocks"]) == 1

    async def test_empty_session(
        self, executor: ToolExecutor, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/empty")
        result = await executor.list_blocks(session_id=sess["id"], limit=10)
        assert result["blocks"] == []

    async def test_limit_respected(
        self, executor: ToolExecutor, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/")
        for i in range(5):
            await daemon.record_block(sess["id"], {"command": f"cmd{i}", "exit_code": 0})
        result = await executor.list_blocks(session_id=sess["id"], limit=3)
        assert len(result["blocks"]) == 3


class TestGetBlock:
    async def test_found(
        self, executor: ToolExecutor, daemon: BlinkDaemon
    ) -> None:
        sess = await daemon.register_session(cwd="/")
        await daemon.record_block(
            sess["id"], {"id": "myblock", "command": "ls", "exit_code": 0}
        )
        result = await executor.get_block(block_id="myblock")
        assert result["block"] is not None
        assert result["block"]["command"] == "ls"

    async def test_not_found(self, executor: ToolExecutor) -> None:
        result = await executor.get_block(block_id="doesnotexist")
        assert result["block"] is None
        assert "error" in result


class TestGetVisibleScreen:
    async def test_no_kitty_client(self, executor: ToolExecutor) -> None:
        result = await executor.get_visible_screen()
        assert result["content"] is None
        assert "error" in result


class TestGetSelection:
    async def test_no_kitty_client(self, executor: ToolExecutor) -> None:
        result = await executor.get_selection()
        assert result["selection"] is None
        assert "error" in result


class TestGetPromptBuffer:
    async def test_no_kitty_client(self, executor: ToolExecutor) -> None:
        result = await executor.get_prompt_buffer()
        assert result["buffer"] is None
        assert "error" in result


# ---------------------------------------------------------------------------
# Buffer tool implementations (without Kitty — expect graceful errors)
# ---------------------------------------------------------------------------


class TestReplacePromptBuffer:
    async def test_no_kitty_client(self, executor: ToolExecutor) -> None:
        result = await executor.replace_prompt_buffer(text="ls -la")
        assert result["ok"] is False
        assert "error" in result


class TestInsertAtCursor:
    async def test_no_kitty_client(self, executor: ToolExecutor) -> None:
        result = await executor.insert_at_cursor(text=" --help")
        assert result["ok"] is False


class TestAcceptCompletion:
    async def test_no_kitty_client(self, executor: ToolExecutor) -> None:
        result = await executor.accept_completion()
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Write tool implementations
# ---------------------------------------------------------------------------


class TestRunCommand:
    async def test_simple_command(self, executor: ToolExecutor) -> None:
        result = await executor.run_command(cmd="echo hello")
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]
        assert result["stderr"] == ""

    async def test_nonzero_exit_code(self, executor: ToolExecutor) -> None:
        result = await executor.run_command(cmd="exit 7")
        assert result["exit_code"] == 7

    async def test_stderr_captured(self, executor: ToolExecutor) -> None:
        result = await executor.run_command(cmd="echo err >&2")
        assert result["exit_code"] == 0

    async def test_command_with_cwd(self, executor: ToolExecutor, tmp_path) -> None:
        result = await executor.run_command(cmd="pwd", cwd=str(tmp_path))
        assert result["exit_code"] == 0
        assert str(tmp_path) in result["stdout"]

    async def test_command_with_extra_env(self, executor: ToolExecutor) -> None:
        result = await executor.run_command(
            cmd="echo $MY_VAR",
            env={"MY_VAR": "testvalue"},
        )
        assert result["exit_code"] == 0
        assert "testvalue" in result["stdout"]

    async def test_timeout(self, executor: ToolExecutor) -> None:
        result = await executor.run_command(cmd="sleep 10", timeout=0.1)
        assert result["exit_code"] == -1
        assert "timed out" in result["error"].lower()


class TestSendSignal:
    async def test_nonexistent_pid(self, executor: ToolExecutor) -> None:
        result = await executor.send_signal(pid=999999999, signal_name="SIGTERM")
        assert result["ok"] is False
        assert "No process" in result["error"]

    async def test_unknown_signal(self, executor: ToolExecutor) -> None:
        import os

        result = await executor.send_signal(pid=os.getpid(), signal_name="SIGUNKNOWN")
        assert result["ok"] is False
        assert "Unknown signal" in result["error"]

    async def test_sends_to_self_with_safe_signal(self, executor: ToolExecutor) -> None:
        import os

        # SIGCONT is safe to send to ourselves
        result = await executor.send_signal(pid=os.getpid(), signal_name="SIGCONT")
        assert result["ok"] is True


class TestCancelCommand:
    async def test_no_kitty_client(self, executor: ToolExecutor) -> None:
        result = await executor.cancel_command()
        assert result["ok"] is False


class TestWriteStdin:
    async def test_no_kitty_client(self, executor: ToolExecutor) -> None:
        result = await executor.write_stdin(text="hello\n")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# ToolExecutor.execute dispatcher
# ---------------------------------------------------------------------------


class TestExecuteDispatcher:
    async def test_dispatches_known_tool(
        self, executor: ToolExecutor, daemon: BlinkDaemon
    ) -> None:
        result = await executor.execute("list_sessions", {})
        assert "sessions" in result

    async def test_raises_for_unknown_tool(self, executor: ToolExecutor) -> None:
        with pytest.raises(ValueError, match="Unknown tool"):
            await executor.execute("no_such_tool", {})
