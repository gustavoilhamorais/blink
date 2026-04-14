"""Agent Communication Protocol (ACP).

Defines the message format, session management, and agent runtime for
inline agent interactions within the Blink terminal.

ACP sessions are multiplexed over the daemon IPC socket. The gateway
coordinates between the LLM providers, MCP tools, and the terminal.

Key components:
- gateway.py  — ACPGateway (client + server), InlineRenderer, RunMode
- runs.py     — AgentRun state machine, PendingAction, RunEvent, RunManager
- sessions.py — ACPSession, SessionContext, SessionManager
- router.py   — RouterAgent, AgentType

Keybindings:
    Ctrl+Enter  Submit prompt to agent
    Ctrl+.      Explain last output
    Ctrl+C      Cancel current agent run
    Enter       Approve pending action
    Escape      Deny pending action
    e           Edit pending action before approving
"""

from blink.acp.gateway import AGENT_KEYBINDINGS, ACPGateway, InlineRenderer, RunMode
from blink.acp.router import AgentType, RouterAgent
from blink.acp.runs import AgentRun, PendingAction, RunEvent, RunManager, RunState
from blink.acp.sessions import (
    CONTEXT_HISTORY_LIMIT,
    CONTEXT_LARGE_OUTPUT_LIMIT,
    ACPSession,
    SessionContext,
    SessionManager,
)

__all__ = [
    # Gateway
    "ACPGateway",
    "AGENT_KEYBINDINGS",
    "InlineRenderer",
    "RunMode",
    # Router
    "AgentType",
    "RouterAgent",
    # Runs
    "AgentRun",
    "PendingAction",
    "RunEvent",
    "RunManager",
    "RunState",
    # Sessions
    "ACPSession",
    "CONTEXT_HISTORY_LIMIT",
    "CONTEXT_LARGE_OUTPUT_LIMIT",
    "SessionContext",
    "SessionManager",
]
