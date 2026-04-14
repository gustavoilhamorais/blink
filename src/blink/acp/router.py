"""ACP Router Agent.

Routes agent requests to the most appropriate handler based on heuristics
about what the user is asking for:

- TERMINAL  — simple shell queries; uses MCP tools directly
- CODE      — code editing, project tasks; routes to an external agent
- SPECIALIST — domain-specific work (e.g. git, docker, SQL)

The router uses a fast keyword/pattern heuristic first, and optionally
asks the LLM for routing decisions when the request is ambiguous.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

from blink.acp.gateway import ACPGateway
from blink.acp.runs import AgentRun, RunEvent, RunManager, RunState
from blink.acp.sessions import ACPSession, SessionContext
from blink.providers.base import CompletionProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent types
# ---------------------------------------------------------------------------


class AgentType:
    """Categories of agent that can handle a request."""

    TERMINAL = "terminal"
    """Handles requests using MCP tools directly (shell commands, terminal state)."""

    CODE = "code"
    """Delegates to an external code agent (e.g. Claude Code, aider)."""

    SPECIALIST = "specialist"
    """Domain-specific agents (git, docker, database, etc.)."""


# ---------------------------------------------------------------------------
# Routing heuristics
# ---------------------------------------------------------------------------

# Keywords that strongly indicate a CODE agent should handle the request
_CODE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(edit|refactor|rewrite|implement|fix|create|write)\b.*\b(file|function|class|method|module|code|script)\b", re.I),
    re.compile(r"\b(add|remove|update)\b.*\b(import|dependency|package)\b", re.I),
    re.compile(r"\bopen\b.*\bin\b.*(editor|vim|nano|vscode|code)\b", re.I),
    re.compile(r"\b(debug|trace|profile)\b.*\b(program|app|application)\b", re.I),
    re.compile(r"\b(pull request|PR|diff|patch|merge conflict)\b", re.I),
]

# Keywords for SPECIALIST agents
_SPECIALIST_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "git": [
        re.compile(r"\b(git|commit|branch|merge|rebase|cherry.pick|stash|tag)\b", re.I),
        re.compile(r"\b(push|pull|fetch|clone|fork|origin|remote)\b", re.I),
    ],
    "docker": [
        re.compile(r"\b(docker|container|image|dockerfile|compose|kubernetes|k8s)\b", re.I),
    ],
    "database": [
        re.compile(r"\b(sql|postgres|mysql|sqlite|mongodb|redis|database|query|table|schema)\b", re.I),
    ],
    "network": [
        re.compile(r"\b(curl|wget|http|https|api|endpoint|request|response|proxy|nginx|apache)\b", re.I),
    ],
}

# Patterns that suggest a simple TERMINAL query (read-only or low-risk)
_TERMINAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(list|show|display|print|cat|ls|find|grep|search)\b", re.I),
    re.compile(r"\b(what|where|which|who|how many)\b", re.I),
    re.compile(r"\b(run|execute|install|start|stop|restart)\b\s+\w", re.I),
    re.compile(r"\b(cd|pwd|mkdir|rm|cp|mv)\b", re.I),
    re.compile(r"\b(explain|describe|summarize)\b.*\b(output|error|command|result)\b", re.I),
]


def _score_agent_type(prompt: str) -> AgentType:
    """Score a prompt against patterns and return the best agent type.

    Returns AgentType.TERMINAL as the default (safest) choice.
    """
    # Check CODE patterns first (highest specificity)
    for pattern in _CODE_PATTERNS:
        if pattern.search(prompt):
            return AgentType.CODE

    # Check SPECIALIST patterns
    for _domain, patterns in _SPECIALIST_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(prompt):
                return AgentType.SPECIALIST

    # Check explicit TERMINAL patterns
    for pattern in _TERMINAL_PATTERNS:
        if pattern.search(prompt):
            return AgentType.TERMINAL

    # Default: TERMINAL (safest, uses existing MCP tools)
    return AgentType.TERMINAL


# ---------------------------------------------------------------------------
# RouterAgent
# ---------------------------------------------------------------------------


class RouterAgent:
    """Routes requests to the appropriate agent type and executes them.

    The router maintains a reference to the gateway so it can call back
    into the streaming execution engine.
    """

    def __init__(
        self,
        gateway: ACPGateway,
        providers: dict[str, CompletionProvider],
        run_manager: RunManager,
    ) -> None:
        self._gateway = gateway
        self._providers = providers
        self._run_manager = run_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def route(self, prompt: str, context: SessionContext) -> AgentType:
        """Decide which agent type should handle the request.

        Uses fast keyword heuristics. If a provider is available and the
        prompt is ambiguous, it may ask the LLM for a routing decision.

        Args:
            prompt: The user's natural-language request.
            context: Current session context (cwd, history, etc.).

        Returns:
            An AgentType constant.
        """
        # Fast heuristic routing (no LLM call needed)
        agent_type = _score_agent_type(prompt)

        # Contextual boost: if history shows heavy git use, prefer SPECIALIST
        if agent_type == AgentType.TERMINAL and context.history:
            recent_git = sum(1 for cmd in context.history[:5] if cmd.startswith("git "))
            if recent_git >= 3:
                # Re-check for git-related prompt
                for pattern in _SPECIALIST_PATTERNS.get("git", []):
                    if pattern.search(prompt):
                        agent_type = AgentType.SPECIALIST
                        break

        logger.debug("Routing prompt %r -> %s", prompt[:50], agent_type)
        return agent_type

    async def execute(
        self,
        prompt: str,
        agent_type: AgentType,
        session: ACPSession,
    ) -> AsyncIterator[RunEvent]:
        """Execute a request with the selected agent type.

        Returns an async iterator of RunEvents. The iterator drives the
        underlying agent to completion and yields events as they occur.

        Args:
            prompt: The user's natural-language request.
            agent_type: Which agent handler to use.
            session: The ACP session context.
        """
        run = await self._run_manager.create(
            session_id=session.blink_session_id,
            prompt=prompt,
        )

        if agent_type == AgentType.TERMINAL:
            return self._execute_terminal(run, session)
        elif agent_type == AgentType.CODE:
            return self._execute_code(run, session)
        elif agent_type == AgentType.SPECIALIST:
            return self._execute_specialist(run, session)
        else:
            return self._execute_terminal(run, session)

    # ------------------------------------------------------------------
    # Agent-type handlers
    # ------------------------------------------------------------------

    async def _execute_terminal(
        self, run: AgentRun, session: ACPSession
    ) -> AsyncIterator[RunEvent]:
        """Execute using the gateway's streaming agent loop (MCP tools)."""
        async for event in await self._gateway.stream_run(run):
            yield event

    async def _execute_code(
        self, run: AgentRun, session: ACPSession
    ) -> AsyncIterator[RunEvent]:
        """Delegate to an external code agent.

        Currently falls back to the terminal agent since external agent
        integration is not yet implemented. Future: launch Claude Code,
        aider, or another external tool via subprocess.

        Note: We yield the informational text event first, then delegate
        to stream_run which transitions the run to IN_PROGRESS internally.
        """
        # Notify the user that we're using terminal mode as a fallback
        yield RunEvent(
            type="text",
            data="[Note: External code agent not configured, using terminal agent]\n",
        )
        # stream_run handles state transitions; do not pre-transition here
        async for event in await self._gateway.stream_run(run):
            yield event

    async def _execute_specialist(
        self, run: AgentRun, session: ACPSession
    ) -> AsyncIterator[RunEvent]:
        """Execute with domain-specific context injected.

        For now, specialist execution uses the same terminal agent but with
        domain-specific context prepended to the prompt.
        """
        # Detect domain and inject specialized context
        domain = self._detect_domain(run.prompt)
        domain_hint = self._domain_hint(domain, session)

        if domain_hint:
            # Inject domain context by modifying the run's prompt
            enriched_prompt = f"{domain_hint}\n\nUser request: {run.prompt}"
            # Create a new run with enriched prompt instead
            await self._run_manager.transition(run.id, RunState.CANCELLED)
            run = await self._run_manager.create(
                session_id=session.blink_session_id,
                prompt=enriched_prompt,
            )

        async for event in await self._gateway.stream_run(run):
            yield event

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_domain(prompt: str) -> str:
        """Return the specialist domain for a prompt, or 'general'."""
        for domain, patterns in _SPECIALIST_PATTERNS.items():
            for pattern in patterns:
                if pattern.search(prompt):
                    return domain
        return "general"

    @staticmethod
    def _domain_hint(domain: str, session: ACPSession) -> str:
        """Generate a domain-specific context hint for the agent."""
        hints: dict[str, str] = {
            "git": (
                "You are assisting with a git-related task. "
                "Prefer safe, non-destructive git operations. "
                "Always show the user what will be committed/changed before acting."
            ),
            "docker": (
                "You are assisting with Docker/container operations. "
                "Be careful with container lifecycle commands. "
                "Prefer non-destructive inspection first."
            ),
            "database": (
                "You are assisting with database operations. "
                "Never run DELETE or DROP without explicit user confirmation. "
                "Always show queries before executing them."
            ),
            "network": (
                "You are assisting with network/HTTP operations. "
                "Redact any API keys or secrets from output."
            ),
        }
        return hints.get(domain, "")


__all__ = ["AgentType", "RouterAgent"]
