"""Tests for the ACP Router Agent (RouterAgent, AgentType, routing heuristics)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from blink.acp.gateway import ACPGateway
from blink.acp.router import AgentType, RouterAgent, _score_agent_type
from blink.acp.runs import RunEvent, RunManager, RunState
from blink.acp.sessions import ACPSession, SessionContext, SessionManager
from blink.daemon.app import BlinkDaemon
from blink.mcp.server import MCPServer
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
async def daemon(tmp_path):
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
def gateway(mcp_server, run_manager, session_manager):
    return ACPGateway(
        mcp_server=mcp_server,
        providers={},
        run_manager=run_manager,
        session_manager=session_manager,
    )


@pytest.fixture
def router(gateway, run_manager):
    return RouterAgent(
        gateway=gateway,
        providers={},
        run_manager=run_manager,
    )


@pytest.fixture
async def session(daemon, storage):
    blink_session = await daemon.register_session(cwd="/home/user")
    sm = SessionManager(storage=storage, daemon=daemon)
    return await sm.create_session(blink_session["id"])


# ---------------------------------------------------------------------------
# AgentType constants
# ---------------------------------------------------------------------------


class TestAgentType:
    def test_terminal_value(self):
        assert AgentType.TERMINAL == "terminal"

    def test_code_value(self):
        assert AgentType.CODE == "code"

    def test_specialist_value(self):
        assert AgentType.SPECIALIST == "specialist"


# ---------------------------------------------------------------------------
# _score_agent_type heuristic
# ---------------------------------------------------------------------------


class TestScoreAgentType:
    def test_simple_list_command_is_terminal(self):
        assert _score_agent_type("list all files in /tmp") == AgentType.TERMINAL

    def test_ls_command_is_terminal(self):
        assert _score_agent_type("ls -la") == AgentType.TERMINAL

    def test_run_command_is_terminal(self):
        assert _score_agent_type("run the tests") == AgentType.TERMINAL

    def test_edit_file_is_code(self):
        assert _score_agent_type("edit the main.py file") == AgentType.CODE

    def test_refactor_class_is_code(self):
        assert _score_agent_type("refactor the User class") == AgentType.CODE

    def test_implement_function_is_code(self):
        assert _score_agent_type("implement the sort function") == AgentType.CODE

    def test_fix_code_bug_is_code(self):
        assert _score_agent_type("fix the bug in my code") == AgentType.CODE

    def test_git_commit_is_specialist(self):
        assert _score_agent_type("git commit the changes") == AgentType.SPECIALIST

    def test_git_push_is_specialist(self):
        assert _score_agent_type("push to origin") == AgentType.SPECIALIST

    def test_docker_is_specialist(self):
        assert _score_agent_type("start docker container") == AgentType.SPECIALIST

    def test_sql_query_is_specialist(self):
        assert _score_agent_type("run this sql query on the database") == AgentType.SPECIALIST

    def test_unknown_prompt_defaults_to_terminal(self):
        # Unknown prompts should fall through to TERMINAL (safest default)
        result = _score_agent_type("xyzzy frobnicate")
        assert result == AgentType.TERMINAL

    def test_code_patterns_beat_terminal_patterns(self):
        # "edit file" has both code-type and terminal-type words, code should win
        assert _score_agent_type("edit the config file") == AgentType.CODE

    def test_curl_http_is_specialist(self):
        assert _score_agent_type("make an http request to the api") == AgentType.SPECIALIST


# ---------------------------------------------------------------------------
# RouterAgent.route
# ---------------------------------------------------------------------------


class TestRouterAgentRoute:
    async def test_route_terminal_prompt(
        self, router: RouterAgent
    ):
        ctx = SessionContext()
        result = await router.route("list all running processes", ctx)
        assert result == AgentType.TERMINAL

    async def test_route_code_prompt(
        self, router: RouterAgent
    ):
        ctx = SessionContext()
        result = await router.route("refactor the database module", ctx)
        assert result == AgentType.CODE

    async def test_route_specialist_prompt(
        self, router: RouterAgent
    ):
        ctx = SessionContext()
        result = await router.route("commit all staged changes to git", ctx)
        assert result == AgentType.SPECIALIST

    async def test_recent_git_history_boosts_specialist(
        self, router: RouterAgent
    ):
        """Heavy git usage in history should boost SPECIALIST routing for git prompts."""
        ctx = SessionContext(
            history=["git status", "git add .", "git commit", "git push", "git diff"]
        )
        result = await router.route("show me the git log", ctx)
        assert result == AgentType.SPECIALIST

    async def test_empty_context_still_routes(
        self, router: RouterAgent
    ):
        ctx = SessionContext()
        result = await router.route("do something", ctx)
        assert result in {AgentType.TERMINAL, AgentType.CODE, AgentType.SPECIALIST}


# ---------------------------------------------------------------------------
# RouterAgent.execute
# ---------------------------------------------------------------------------


class TestRouterAgentExecute:
    async def _collect(self, aiter) -> list[RunEvent]:
        events = []
        async for event in aiter:
            events.append(event)
        return events

    async def test_execute_terminal_yields_events(
        self, router: RouterAgent, session: ACPSession
    ):
        aiter = await router.execute(
            prompt="list files",
            agent_type=AgentType.TERMINAL,
            session=session,
        )
        events = await self._collect(aiter)
        assert len(events) > 0
        types = {e.type for e in events}
        # At minimum we should get completed or error
        assert types & {"completed", "error", "text"}

    async def test_execute_code_yields_note_and_events(
        self, router: RouterAgent, session: ACPSession
    ):
        aiter = await router.execute(
            prompt="edit the file",
            agent_type=AgentType.CODE,
            session=session,
        )
        events = await self._collect(aiter)
        # Should yield a "Note: External code agent not configured" text event
        text_events = [e for e in events if e.type == "text"]
        assert len(text_events) > 0
        combined = " ".join(str(e.data) for e in text_events)
        assert "code agent" in combined.lower() or "provider" in combined.lower()

    async def test_execute_specialist_yields_events(
        self, router: RouterAgent, session: ACPSession
    ):
        aiter = await router.execute(
            prompt="commit the changes",
            agent_type=AgentType.SPECIALIST,
            session=session,
        )
        events = await self._collect(aiter)
        assert len(events) > 0

    async def test_execute_creates_run(
        self, router: RouterAgent, session: ACPSession, run_manager: RunManager
    ):
        aiter = await router.execute(
            prompt="test prompt",
            agent_type=AgentType.TERMINAL,
            session=session,
        )
        events = await self._collect(aiter)
        runs = await run_manager.list_for_session(session.blink_session_id)
        assert len(runs) > 0


# ---------------------------------------------------------------------------
# RouterAgent._detect_domain
# ---------------------------------------------------------------------------


class TestRouterAgentDetectDomain:
    def test_git_domain(self):
        assert RouterAgent._detect_domain("git commit") == "git"

    def test_docker_domain(self):
        assert RouterAgent._detect_domain("run docker container") == "docker"

    def test_database_domain(self):
        assert RouterAgent._detect_domain("run sql query") == "database"

    def test_network_domain(self):
        assert RouterAgent._detect_domain("curl the api endpoint") == "network"

    def test_unknown_domain(self):
        assert RouterAgent._detect_domain("xyzzy") == "general"


# ---------------------------------------------------------------------------
# RouterAgent._domain_hint
# ---------------------------------------------------------------------------


class TestRouterAgentDomainHint:
    def test_git_hint_is_non_empty(self):
        hint = RouterAgent._domain_hint("git", SessionContext())
        assert len(hint) > 0
        assert "git" in hint.lower()

    def test_docker_hint_is_non_empty(self):
        hint = RouterAgent._domain_hint("docker", SessionContext())
        assert len(hint) > 0

    def test_general_domain_returns_empty(self):
        hint = RouterAgent._domain_hint("general", SessionContext())
        assert hint == ""

    def test_database_hint_warns_about_delete(self):
        hint = RouterAgent._domain_hint("database", SessionContext())
        assert "DELETE" in hint or "DROP" in hint
