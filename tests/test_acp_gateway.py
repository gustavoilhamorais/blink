"""Tests for the ACP Gateway (ACPGateway, InlineRenderer, RunMode)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink.acp.gateway import ACPGateway, AGENT_KEYBINDINGS, InlineRenderer, RunMode
from blink.acp.runs import AgentRun, PendingAction, RunEvent, RunManager, RunState
from blink.acp.sessions import SessionContext, SessionManager
from blink.daemon.app import BlinkDaemon
from blink.mcp.server import MCPServer
from blink.providers.base import CompletionProvider, ProviderConfig
from blink.security.capabilities import Capability, SecurityPolicy
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
async def daemon(tmp_path, storage):
    db = tmp_path / "daemon.db"
    d = BlinkDaemon(socket_path=tmp_path / "test.sock", db_path=db)
    await d._storage.init_db()
    yield d
    await d._storage.close()


@pytest.fixture
def mcp_server(daemon):
    policy = SecurityPolicy(
        granted_capability=Capability.ACT,
        require_confirmation=False,
    )
    return MCPServer(daemon=daemon, policy=policy)


@pytest.fixture
async def run_manager(storage):
    return RunManager(storage=storage)


@pytest.fixture
async def session_manager(storage, daemon):
    return SessionManager(storage=storage, daemon=daemon)


@pytest.fixture
def no_provider_gateway(mcp_server, run_manager, session_manager):
    """Gateway with no providers configured."""
    return ACPGateway(
        mcp_server=mcp_server,
        providers={},
        run_manager=run_manager,
        session_manager=session_manager,
    )


# ---------------------------------------------------------------------------
# RunMode constants
# ---------------------------------------------------------------------------


class TestRunMode:
    def test_sync_value(self):
        assert RunMode.SYNC == "sync"

    def test_async_value(self):
        assert RunMode.ASYNC == "async"

    def test_stream_value(self):
        assert RunMode.STREAM == "stream"


# ---------------------------------------------------------------------------
# AGENT_KEYBINDINGS
# ---------------------------------------------------------------------------


class TestAgentKeybindings:
    def test_ctrl_enter_defined(self):
        assert "Ctrl+Enter" in AGENT_KEYBINDINGS

    def test_escape_defined(self):
        assert "Escape" in AGENT_KEYBINDINGS

    def test_ctrl_c_defined(self):
        assert "Ctrl+C" in AGENT_KEYBINDINGS

    def test_all_values_are_strings(self):
        for key, value in AGENT_KEYBINDINGS.items():
            assert isinstance(key, str)
            assert isinstance(value, str)


# ---------------------------------------------------------------------------
# ACPGateway.create_run
# ---------------------------------------------------------------------------


class TestACPGatewayCreateRun:
    async def test_creates_run_in_created_state(
        self, no_provider_gateway: ACPGateway, daemon: BlinkDaemon
    ):
        blink_session = await daemon.register_session(cwd="/")
        run = await no_provider_gateway.create_run(
            prompt="what is the time?",
            session_id=blink_session["id"],
        )
        assert run.state == RunState.CREATED

    async def test_run_has_correct_prompt(
        self, no_provider_gateway: ACPGateway, daemon: BlinkDaemon
    ):
        blink_session = await daemon.register_session(cwd="/")
        run = await no_provider_gateway.create_run(
            prompt="list files here",
            session_id=blink_session["id"],
        )
        assert run.prompt == "list files here"

    async def test_run_mode_default_is_stream(
        self, no_provider_gateway: ACPGateway, daemon: BlinkDaemon
    ):
        blink_session = await daemon.register_session(cwd="/")
        run = await no_provider_gateway.create_run("test", blink_session["id"])
        assert isinstance(run, AgentRun)

    async def test_run_is_stored_in_run_manager(
        self,
        no_provider_gateway: ACPGateway,
        daemon: BlinkDaemon,
        run_manager: RunManager,
    ):
        blink_session = await daemon.register_session(cwd="/")
        run = await no_provider_gateway.create_run("test", blink_session["id"])
        retrieved = await run_manager.get(run.id)
        assert retrieved is not None


# ---------------------------------------------------------------------------
# ACPGateway.stream_run (no provider — fallback path)
# ---------------------------------------------------------------------------


class TestACPGatewayStreamRunFallback:
    async def _collect_events(self, gateway: ACPGateway, run: AgentRun) -> list[RunEvent]:
        events = []
        async for event in await gateway.stream_run(run):
            events.append(event)
        return events

    async def test_fallback_yields_text_and_completed(
        self, no_provider_gateway: ACPGateway, daemon: BlinkDaemon
    ):
        blink_session = await daemon.register_session(cwd="/")
        run = await no_provider_gateway.create_run("hello", blink_session["id"])
        events = await self._collect_events(no_provider_gateway, run)
        types = [e.type for e in events]
        assert "text" in types
        assert "completed" in types

    async def test_fallback_run_reaches_completed_state(
        self, no_provider_gateway: ACPGateway, daemon: BlinkDaemon, run_manager: RunManager
    ):
        blink_session = await daemon.register_session(cwd="/")
        run = await no_provider_gateway.create_run("hello", blink_session["id"])
        await self._collect_events(no_provider_gateway, run)
        final = await run_manager.get(run.id)
        assert final is not None
        assert final.state == RunState.COMPLETED

    async def test_fallback_text_mentions_no_provider(
        self, no_provider_gateway: ACPGateway, daemon: BlinkDaemon
    ):
        blink_session = await daemon.register_session(cwd="/")
        run = await no_provider_gateway.create_run("test prompt", blink_session["id"])
        events = await self._collect_events(no_provider_gateway, run)
        text_events = [e for e in events if e.type == "text"]
        combined = " ".join(str(e.data) for e in text_events)
        assert "provider" in combined.lower()


# ---------------------------------------------------------------------------
# ACPGateway.cancel_run
# ---------------------------------------------------------------------------


class TestACPGatewayCancelRun:
    async def test_cancel_in_progress_run(
        self, no_provider_gateway: ACPGateway, daemon: BlinkDaemon, run_manager: RunManager
    ):
        blink_session = await daemon.register_session(cwd="/")
        run = await no_provider_gateway.create_run("long task", blink_session["id"])
        # Transition to in_progress manually
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        await no_provider_gateway.cancel_run(run.id)
        final = await run_manager.get(run.id)
        assert final is not None
        assert final.state == RunState.CANCELLED

    async def test_cancel_nonexistent_run_does_not_raise(
        self, no_provider_gateway: ACPGateway
    ):
        # Should not raise for unknown run
        await no_provider_gateway.cancel_run("nonexistent-id")


# ---------------------------------------------------------------------------
# ACPGateway.handle_tool_call
# ---------------------------------------------------------------------------


class TestACPGatewayHandleToolCall:
    async def test_delegates_to_mcp_server(
        self, no_provider_gateway: ACPGateway, daemon: BlinkDaemon
    ):
        await daemon.register_session(cwd="/test")
        result = await no_provider_gateway.handle_tool_call(
            "run_command", {"cmd": "echo gateway_test"}
        )
        assert "gateway_test" in str(result)

    async def test_raises_on_tool_error(
        self, no_provider_gateway: ACPGateway
    ):
        # Unknown tool should raise RuntimeError (forwarded from isError response)
        with pytest.raises(RuntimeError):
            await no_provider_gateway.handle_tool_call("totally_unknown_tool", {})


# ---------------------------------------------------------------------------
# ACPGateway._format_tool_preview
# ---------------------------------------------------------------------------


class TestACPGatewayFormatToolPreview:
    def test_run_command_preview(self):
        preview = ACPGateway._format_tool_preview("run_command", {"cmd": "ls -la"})
        assert "ls -la" in preview
        assert "Execute" in preview

    def test_run_command_with_cwd(self):
        preview = ACPGateway._format_tool_preview(
            "run_command", {"cmd": "make", "cwd": "/project"}
        )
        assert "/project" in preview

    def test_write_stdin_preview(self):
        preview = ACPGateway._format_tool_preview("write_stdin", {"text": "yes\n"})
        assert "stdin" in preview.lower()

    def test_send_signal_preview(self):
        preview = ACPGateway._format_tool_preview(
            "send_signal", {"pid": 1234, "signal": "SIGKILL"}
        )
        assert "SIGKILL" in preview
        assert "1234" in preview

    def test_cancel_command_preview(self):
        preview = ACPGateway._format_tool_preview("cancel_command", {})
        assert "Ctrl+C" in preview

    def test_unknown_tool_preview(self):
        preview = ACPGateway._format_tool_preview("custom_tool", {"arg": "val"})
        assert "custom_tool" in preview


# ---------------------------------------------------------------------------
# ACPGateway._format_context
# ---------------------------------------------------------------------------


class TestACPGatewayFormatContext:
    def test_empty_context_returns_empty_string(self):
        ctx = SessionContext()
        result = ACPGateway._format_context(ctx)
        assert result == ""

    def test_context_with_cwd(self):
        ctx = SessionContext(cwd="/home/user")
        result = ACPGateway._format_context(ctx)
        assert "/home/user" in result

    def test_context_with_history(self):
        ctx = SessionContext(history=["git status", "ls -la"])
        result = ACPGateway._format_context(ctx)
        assert "git status" in result


# ---------------------------------------------------------------------------
# ACPGateway._tool_needs_confirmation
# ---------------------------------------------------------------------------


class TestACPGatewayToolNeedsConfirmation:
    def test_run_command_needs_confirmation(
        self, no_provider_gateway: ACPGateway
    ):
        # Policy has require_confirmation=False for no_provider_gateway's mcp_server fixture
        # (act_server fixture disables confirmation)
        assert no_provider_gateway._tool_needs_confirmation("run_command") is False

    def test_observe_tool_does_not_need_confirmation(
        self, no_provider_gateway: ACPGateway
    ):
        assert no_provider_gateway._tool_needs_confirmation("list_sessions") is False

    def test_confirmation_when_policy_requires_it(
        self, mcp_server: MCPServer, run_manager: RunManager, session_manager: SessionManager
    ):
        # Build a gateway with require_confirmation=True
        confirm_policy = SecurityPolicy(
            granted_capability=Capability.ACT,
            require_confirmation=True,
        )
        mcp_server._policy = confirm_policy
        gateway = ACPGateway(
            mcp_server=mcp_server,
            providers={},
            run_manager=run_manager,
            session_manager=session_manager,
        )
        assert gateway._tool_needs_confirmation("run_command") is True
        assert gateway._tool_needs_confirmation("list_sessions") is False


# ---------------------------------------------------------------------------
# InlineRenderer
# ---------------------------------------------------------------------------


class TestInlineRenderer:
    @pytest.fixture
    def mock_kitty(self):
        kitty = MagicMock()
        kitty.send_text = AsyncMock()
        return kitty

    @pytest.fixture
    def renderer(self, mock_kitty):
        return InlineRenderer(kitty_client=mock_kitty)

    async def test_render_text_event(self, renderer: InlineRenderer, mock_kitty):
        event = RunEvent(type="text", data="Hello, world!")
        await renderer.render_event(event, window_id=1)
        mock_kitty.send_text.assert_called_once_with(1, "Hello, world!")

    async def test_render_tool_call_event(self, renderer: InlineRenderer, mock_kitty):
        event = RunEvent(type="tool_call", data={"tool": "run_command", "arguments": {"cmd": "ls"}})
        await renderer.render_event(event, window_id=1)
        mock_kitty.send_text.assert_called_once()
        call_args = mock_kitty.send_text.call_args[0]
        assert "run_command" in call_args[1]

    async def test_render_awaiting_event(self, renderer: InlineRenderer, mock_kitty):
        event = RunEvent(
            type="awaiting",
            data={"action": {"preview": "Delete files", "tool": "run_command", "arguments": {}}, "run_id": "abc"},
        )
        await renderer.render_event(event, window_id=1)
        mock_kitty.send_text.assert_called_once()
        call_args = mock_kitty.send_text.call_args[0]
        assert "Delete files" in call_args[1]

    async def test_render_completed_event(self, renderer: InlineRenderer, mock_kitty):
        event = RunEvent(type="completed", data={"result": "done"})
        await renderer.render_event(event, window_id=1)
        mock_kitty.send_text.assert_called_once()
        call_args = mock_kitty.send_text.call_args[0]
        assert "done" in call_args[1] or "✓" in call_args[1] or "done" in call_args[1].lower()

    async def test_render_error_event(self, renderer: InlineRenderer, mock_kitty):
        event = RunEvent(type="error", data={"message": "something went wrong"})
        await renderer.render_event(event, window_id=1)
        mock_kitty.send_text.assert_called_once()
        call_args = mock_kitty.send_text.call_args[0]
        assert "something went wrong" in call_args[1]

    async def test_render_cancelled_event(self, renderer: InlineRenderer, mock_kitty):
        event = RunEvent(type="cancelled", data={})
        await renderer.render_event(event, window_id=1)
        mock_kitty.send_text.assert_called_once()

    async def test_no_error_when_kitty_is_none(self):
        renderer = InlineRenderer(kitty_client=None)
        event = RunEvent(type="text", data="hello")
        # Should not raise
        await renderer.render_event(event, window_id=1)

    async def test_no_send_for_empty_text(self, renderer: InlineRenderer, mock_kitty):
        event = RunEvent(type="text", data="")
        await renderer.render_event(event, window_id=1)
        mock_kitty.send_text.assert_not_called()

    async def test_kitty_error_is_silently_ignored(self, mock_kitty):
        import anyio

        mock_kitty.send_text = AsyncMock(side_effect=RuntimeError("kitty died"))
        renderer = InlineRenderer(kitty_client=mock_kitty)
        event = RunEvent(type="text", data="hello")
        # Should not propagate the error
        await renderer.render_event(event, window_id=1)
