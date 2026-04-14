"""ACP Gateway.

Blink acts as both an ACP *client* (sends requests to agents, manages runs)
and an ACP *server* (exposes terminal operations via MCP tools).

The gateway wires together:
- RunManager  — tracks run lifecycle
- MCPServer   — handles tool calls from agents
- Providers   — drives the LLM that powers the agent

Streaming is handled via async generators that yield RunEvents.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio

from blink.acp.runs import AgentRun, PendingAction, RunEvent, RunManager, RunState
from blink.acp.sessions import ACPSession, SessionContext, SessionManager
from blink.mcp.server import MCPServer
from blink.providers.base import CompletionProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Run mode
# ---------------------------------------------------------------------------


class RunMode:
    """How the caller wants to receive run output."""

    SYNC = "sync"    # Wait for complete response
    ASYNC = "async"  # Return immediately, poll for results
    STREAM = "stream"  # Stream response chunks


# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------

_AGENT_SYSTEM_PROMPT = """\
You are Blink, an AI assistant embedded in a terminal. You help the user \
execute shell commands, understand command output, and navigate their \
development environment.

You have access to MCP tools that let you:
- Read terminal state (get_active_session, list_blocks, get_visible_screen, etc.)
- Modify the command-line buffer (replace_prompt_buffer, insert_at_cursor)
- Execute commands (run_command, write_stdin, send_signal)

Always prefer the least-privileged tool. When executing commands, describe \
what you intend to do before calling the tool. Dangerous or destructive \
commands must always be confirmed by the user.

Respond concisely. For terminal tasks, prefer showing commands over \
lengthy explanations.
"""

# ---------------------------------------------------------------------------
# ACPGateway
# ---------------------------------------------------------------------------


class ACPGateway:
    """ACP client/server gateway for Blink.

    Client side: create_run / stream_run / cancel_run
    Server side: handle_tool_call (delegates to MCPServer)
    """

    def __init__(
        self,
        mcp_server: MCPServer,
        providers: dict[str, CompletionProvider],
        run_manager: RunManager,
        session_manager: SessionManager,
    ) -> None:
        self._mcp = mcp_server
        self._providers = providers
        self._run_manager = run_manager
        self._session_manager = session_manager
        # Active run cancellation scopes keyed by run_id
        self._cancel_scopes: dict[str, anyio.CancelScope] = {}

    # ------------------------------------------------------------------
    # Client side — create and manage runs
    # ------------------------------------------------------------------

    async def create_run(
        self,
        prompt: str,
        session_id: str,
        mode: str = RunMode.STREAM,
    ) -> AgentRun:
        """Create a new agent run for the given prompt.

        Args:
            prompt: User's natural-language request.
            session_id: Blink session ID (from the daemon sessions table).
            mode: One of RunMode.SYNC / ASYNC / STREAM (currently all execute
                  the same way; mode is stored for the caller's reference).

        Returns:
            AgentRun in CREATED state.
        """
        run = await self._run_manager.create(session_id=session_id, prompt=prompt)
        return run

    async def stream_run(self, run: AgentRun) -> AsyncIterator[RunEvent]:
        """Drive an agent run to completion, yielding RunEvents as they occur.

        Callers iterate this async generator to receive streamed output.
        On return the run will be in a terminal state (COMPLETED, FAILED,
        or CANCELLED).

        Example::

            run = await gateway.create_run(prompt, session_id)
            async for event in gateway.stream_run(run):
                if event.type == "text":
                    print(event.data, end="", flush=True)
        """
        return self._stream_run_impl(run)

    async def cancel_run(self, run_id: str) -> None:
        """Cancel an in-progress run.

        If the run is currently executing inside stream_run, the cancel scope
        is triggered. The run will be transitioned to CANCELLED.
        """
        scope = self._cancel_scopes.get(run_id)
        if scope is not None:
            scope.cancel()
        # Ensure state is updated even if the scope was already gone
        try:
            run = await self._run_manager.get(run_id)
            if run and not run.is_terminal():
                await self._run_manager.cancel(run_id)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Server side — handle tool calls from agents
    # ------------------------------------------------------------------

    async def handle_tool_call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Handle a tool call from an agent (delegates to MCPServer).

        Returns the raw result dict from the tool executor.
        Raises PermissionError if the tool is not allowed by the policy.
        """
        result = await self._mcp.handle_tools_call(tool, arguments)
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"])
        content = result.get("content", [])
        if content:
            return content[0].get("text", "")
        return ""

    # ------------------------------------------------------------------
    # Internal — run execution engine
    # ------------------------------------------------------------------

    async def _stream_run_impl(self, run: AgentRun) -> AsyncIterator[RunEvent]:
        """Internal generator that drives the agent loop."""
        run = await self._run_manager.transition(run.id, RunState.IN_PROGRESS)

        # Get session context for the run
        try:
            session = await self._session_manager.get_or_create(run.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load ACP session: %s", exc)
            session = None

        # Select provider (prefer first available)
        provider = self._select_provider()

        if provider is None:
            # No LLM available — fall back to simple echo / help text
            async for event in self._fallback_run(run, session):
                yield event
            return

        # Drive the agentic loop with the LLM
        cancel_scope = anyio.CancelScope()
        self._cancel_scopes[run.id] = cancel_scope

        try:
            with cancel_scope:
                async for event in self._llm_agent_loop(run, session, provider):
                    yield event

            if cancel_scope.cancelled_caught:
                await self._run_manager.cancel(run.id)
                yield RunEvent(type="cancelled", data={"run_id": run.id})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent run %s failed: %s", run.id, exc)
            try:
                await self._run_manager.fail(run.id, str(exc))
            except Exception:  # noqa: BLE001
                pass
            yield RunEvent(type="error", data={"message": str(exc), "run_id": run.id})
        finally:
            self._cancel_scopes.pop(run.id, None)

    async def _llm_agent_loop(
        self,
        run: AgentRun,
        session: ACPSession | None,
        provider: CompletionProvider,
    ) -> AsyncIterator[RunEvent]:
        """Drive a multi-turn agent loop via the provider's chat interface.

        The provider is called via its ``chat`` method if available, otherwise
        we fall back to the completion API. Tool calls returned by the model
        are intercepted and routed through the MCP server.
        """
        # Build the initial message list
        messages: list[dict[str, Any]] = []

        # Inject context as a system-level message
        if session:
            ctx = session.context
            context_block = self._format_context(ctx)
            messages.append({"role": "system", "content": context_block})

        messages.append({"role": "user", "content": run.prompt})

        # Collect tool schemas for the provider
        tools_result = await self._mcp.handle_tools_list()
        tools = tools_result.get("tools", [])

        # Agentic loop: up to 10 turns to avoid infinite loops
        max_turns = 10
        accumulated_text: list[str] = []

        for _turn in range(max_turns):
            # Check if run was cancelled
            refreshed = await self._run_manager.get(run.id)
            if refreshed and refreshed.is_terminal():
                return

            # Call the provider
            try:
                response = await self._call_provider_chat(
                    provider=provider,
                    messages=messages,
                    tools=tools,
                    system_prompt=_AGENT_SYSTEM_PROMPT,
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"Provider call failed: {exc}") from exc

            # Process the response
            response_text = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            if response_text:
                accumulated_text.append(response_text)
                yield RunEvent(type="text", data=response_text)

            if not tool_calls:
                # Agent is done — no more tool calls
                break

            # Process each tool call
            messages.append({"role": "assistant", "content": response_text, "tool_calls": tool_calls})
            tool_results: list[dict[str, Any]] = []

            for tool_call in tool_calls:
                tool_name = tool_call.get("name", "")
                tool_args = tool_call.get("arguments", {})
                tool_call_id = tool_call.get("id", "")

                yield RunEvent(
                    type="tool_call",
                    data={"tool": tool_name, "arguments": tool_args, "id": tool_call_id},
                )

                # Check if this tool requires confirmation
                needs_confirmation = self._tool_needs_confirmation(tool_name)

                if needs_confirmation:
                    preview = self._format_tool_preview(tool_name, tool_args)
                    action = PendingAction(
                        tool=tool_name,
                        arguments=tool_args,
                        preview=preview,
                    )
                    await self._run_manager.set_pending_action(run.id, action)
                    yield RunEvent(
                        type="awaiting",
                        data={"action": action.model_dump(), "run_id": run.id},
                    )

                    # Wait for user decision (poll run state)
                    resolved_run = await self._wait_for_resolution(run.id)
                    if resolved_run is None or resolved_run.state == RunState.CANCELLED:
                        return  # User denied or run was cancelled

                    # Continue execution after approval
                    await self._run_manager.transition(run.id, RunState.IN_PROGRESS)

                # Execute the tool
                try:
                    result_text = await self.handle_tool_call(tool_name, tool_args)
                    tool_results.append({
                        "id": tool_call_id,
                        "content": str(result_text),
                        "is_error": False,
                    })
                    yield RunEvent(
                        type="tool_result",
                        data={"tool": tool_name, "result": result_text, "id": tool_call_id},
                    )
                except Exception as exc:  # noqa: BLE001
                    error_msg = str(exc)
                    tool_results.append({
                        "id": tool_call_id,
                        "content": error_msg,
                        "is_error": True,
                    })
                    yield RunEvent(
                        type="tool_result",
                        data={"tool": tool_name, "error": error_msg, "id": tool_call_id},
                    )

            # Add tool results to message history
            messages.append({"role": "tool", "tool_results": tool_results})

        # Run completed successfully
        final_text = "".join(accumulated_text)
        await self._run_manager.complete(run.id, final_text)
        yield RunEvent(type="completed", data={"result": final_text, "run_id": run.id})

    async def _fallback_run(
        self,
        run: AgentRun,
        session: ACPSession | None,
    ) -> AsyncIterator[RunEvent]:
        """Simple fallback when no LLM provider is configured."""
        msg = (
            "No AI provider configured. "
            "Configure a provider with: blink provider add <name>\n"
            f"Your prompt: {run.prompt}"
        )
        yield RunEvent(type="text", data=msg)
        await self._run_manager.complete(run.id, msg)
        yield RunEvent(type="completed", data={"result": msg, "run_id": run.id})

    # ------------------------------------------------------------------
    # Provider abstraction
    # ------------------------------------------------------------------

    def _select_provider(self) -> CompletionProvider | None:
        """Return the first healthy-looking provider, or None."""
        for provider in self._providers.values():
            return provider  # Return first available
        return None

    async def _call_provider_chat(
        self,
        provider: CompletionProvider,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> dict[str, Any]:
        """Call the provider with a chat-style interface.

        Falls back to a simplified interface if the provider doesn't implement
        the full chat API. Returns a dict with 'content' and optional 'tool_calls'.
        """
        # Check if provider has a native chat method
        if hasattr(provider, "chat"):
            return await provider.chat(  # type: ignore[attr-defined]
                messages=messages,
                tools=tools,
                system=system_prompt,
            )

        # Fall back: use the provider to build context and call complete()
        # This won't support tool calls, but gives basic LLM response
        from blink.completions.context import CompletionContext

        # Extract the last user message as the buffer
        last_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user = str(msg.get("content", ""))
                break

        ctx = CompletionContext(
            shell="bash",
            cwd="",
            buffer=last_user,
            recent_commands=[],
            visible_files=[],
        )

        completions = await provider.complete(ctx)
        if completions:
            return {"content": completions[0].text, "tool_calls": []}
        return {"content": "I was unable to generate a response.", "tool_calls": []}

    # ------------------------------------------------------------------
    # Tool confirmation helpers
    # ------------------------------------------------------------------

    # Tools that require user confirmation before execution
    _CONFIRM_TOOLS = frozenset({"run_command", "write_stdin", "send_signal", "cancel_command"})

    def _tool_needs_confirmation(self, tool_name: str) -> bool:
        """Return True if this tool requires user confirmation."""
        # Check MCP server policy — if require_confirmation is off, skip
        if not self._mcp._policy.require_confirmation:
            return False
        return tool_name in self._CONFIRM_TOOLS

    @staticmethod
    def _format_tool_preview(tool_name: str, arguments: dict[str, Any]) -> str:
        """Generate a human-readable description of what a tool call will do."""
        if tool_name == "run_command":
            cmd = arguments.get("cmd", "")
            cwd = arguments.get("cwd", "")
            cwd_str = f" in {cwd}" if cwd else ""
            return f"Execute command{cwd_str}: {cmd}"
        if tool_name == "write_stdin":
            text = arguments.get("text", "")
            return f"Send to stdin: {text!r}"
        if tool_name == "send_signal":
            pid = arguments.get("pid", "?")
            sig = arguments.get("signal", "SIGTERM")
            return f"Send {sig} to PID {pid}"
        if tool_name == "cancel_command":
            wid = arguments.get("window_id", "focused")
            return f"Send Ctrl+C to window {wid}"
        return f"Call {tool_name}({', '.join(f'{k}={v!r}' for k, v in arguments.items())})"

    @staticmethod
    def _format_context(ctx: SessionContext) -> str:
        """Format session context as a system message supplement."""
        parts = []
        if ctx.cwd:
            parts.append(f"Current directory: {ctx.cwd}")
        if ctx.history:
            recent = ctx.history[:10]
            parts.append("Recent commands:\n" + "\n".join(f"  {c}" for c in recent))
        return "\n".join(parts) if parts else ""

    async def _wait_for_resolution(
        self, run_id: str, timeout: float = 300.0, poll_interval: float = 0.25
    ) -> AgentRun | None:
        """Poll run state until it leaves AWAITING_INPUT or times out.

        Returns the updated run, or None if timed out.
        """
        elapsed = 0.0
        while elapsed < timeout:
            run = await self._run_manager.get(run_id)
            if run is None:
                return None
            if run.state != RunState.AWAITING_INPUT:
                return run
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        # Timed out — cancel
        await self._run_manager.cancel(run_id)
        return None


# ---------------------------------------------------------------------------
# Inline Renderer
# ---------------------------------------------------------------------------


class InlineRenderer:
    """Renders agent output inline in the terminal.

    Uses the Kitty RC client to write text directly to a window so that
    agent responses appear as a "block" within the shell session.
    """

    # ANSI colours for different event types
    _RESET = "\033[0m"
    _BOLD = "\033[1m"
    _DIM = "\033[2m"
    _CYAN = "\033[36m"
    _YELLOW = "\033[33m"
    _GREEN = "\033[32m"
    _RED = "\033[31m"
    _BLUE = "\033[34m"
    _MAGENTA = "\033[35m"

    def __init__(self, kitty_client: Any) -> None:
        self._kitty = kitty_client

    async def render_event(self, event: RunEvent, window_id: int) -> None:
        """Render a single event to the terminal."""
        if event.type == "text":
            await self._render_text(str(event.data or ""), window_id)
        elif event.type == "tool_call":
            await self._render_tool_indicator(event.data, window_id)
        elif event.type == "tool_result":
            await self._render_tool_result(event.data, window_id)
        elif event.type == "awaiting":
            await self._render_confirmation_ui(event.data, window_id)
        elif event.type == "completed":
            await self._render_completed(event.data, window_id)
        elif event.type == "error":
            await self._render_error(event.data, window_id)
        elif event.type == "cancelled":
            await self._render_cancelled(event.data, window_id)

    async def _render_text(self, text: str, window_id: int) -> None:
        await self._send(window_id, text)

    async def _render_tool_indicator(self, data: Any, window_id: int) -> None:
        tool = ""
        args = {}
        if isinstance(data, dict):
            tool = data.get("tool", "")
            args = data.get("arguments", {})
        preview = f" {args.get('cmd', '')}" if "cmd" in args else ""
        line = f"\n{self._DIM}{self._CYAN}[tool] {tool}{preview}{self._RESET}\n"
        await self._send(window_id, line)

    async def _render_tool_result(self, data: Any, window_id: int) -> None:
        if not isinstance(data, dict):
            return
        if data.get("error"):
            line = f"{self._DIM}{self._RED}[error] {data['error']}{self._RESET}\n"
        else:
            result = str(data.get("result", ""))
            # Truncate very long results for inline display
            if len(result) > 500:
                result = result[:497] + "…"
            line = f"{self._DIM}[result] {result}{self._RESET}\n"
        await self._send(window_id, line)

    async def _render_confirmation_ui(self, data: Any, window_id: int) -> None:
        """Show inline confirm/deny UI for a pending action."""
        action_data = {}
        if isinstance(data, dict):
            action_data = data.get("action", {})

        preview = action_data.get("preview", "Unknown action")
        run_id = data.get("run_id", "") if isinstance(data, dict) else ""

        lines = [
            f"\n{self._BOLD}{self._YELLOW}[blink] Agent wants to:{self._RESET}",
            f"  {preview}",
            f"  {self._BOLD}[Enter]{self._RESET} Approve  "
            f"{self._BOLD}[Esc]{self._RESET} Deny  "
            f"{self._BOLD}[e]{self._RESET} Edit",
            f"  {self._DIM}(run: {run_id[:8]}…){self._RESET}" if run_id else "",
        ]
        await self._send(window_id, "\n".join(lines) + "\n")

    async def _render_completed(self, data: Any, window_id: int) -> None:
        await self._send(window_id, f"\n{self._DIM}{self._GREEN}[done]{self._RESET}\n")

    async def _render_error(self, data: Any, window_id: int) -> None:
        msg = ""
        if isinstance(data, dict):
            msg = data.get("message", "Unknown error")
        await self._send(window_id, f"\n{self._BOLD}{self._RED}[error] {msg}{self._RESET}\n")

    async def _render_cancelled(self, data: Any, window_id: int) -> None:
        await self._send(window_id, f"\n{self._DIM}[cancelled]{self._RESET}\n")

    async def _send(self, window_id: int, text: str) -> None:
        """Write text to the terminal window via Kitty RC."""
        if self._kitty is None or not text:
            return
        try:
            await self._kitty.send_text(window_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("InlineRenderer send error: %s", exc)


# ---------------------------------------------------------------------------
# Keybindings documentation
# ---------------------------------------------------------------------------

AGENT_KEYBINDINGS: dict[str, str] = {
    "Ctrl+Enter": "Submit prompt to agent",
    "Ctrl+.": "Explain last output",
    "Ctrl+C": "Cancel current agent run",
    "Enter": "Approve pending action",
    "Escape": "Deny pending action",
    "e": "Edit pending action before approving",
}
"""Default keybindings for agent interactions.

These are documented here and referenced by shell integration hooks.
The actual binding configuration lives in the Kitty config / shell rc files.
"""


__all__ = [
    "ACPGateway",
    "AGENT_KEYBINDINGS",
    "InlineRenderer",
    "RunMode",
]
