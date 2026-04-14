"""Tests for ACP run state machine (RunManager + AgentRun)."""

from __future__ import annotations

import pytest

from blink.acp.runs import AgentRun, PendingAction, RunEvent, RunManager, RunState
from blink.storage import Storage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage(tmp_path):
    """Temporary in-memory SQLite storage."""
    db = tmp_path / "test.db"
    s = Storage(db_path=db)
    await s.init_db()
    yield s
    await s.close()


@pytest.fixture
async def run_manager(storage):
    return RunManager(storage=storage)


# ---------------------------------------------------------------------------
# RunState constants
# ---------------------------------------------------------------------------


class TestRunState:
    def test_created_value(self):
        assert RunState.CREATED == "created"

    def test_in_progress_value(self):
        assert RunState.IN_PROGRESS == "in_progress"

    def test_awaiting_input_value(self):
        assert RunState.AWAITING_INPUT == "awaiting_input"

    def test_completed_value(self):
        assert RunState.COMPLETED == "completed"

    def test_failed_value(self):
        assert RunState.FAILED == "failed"

    def test_cancelled_value(self):
        assert RunState.CANCELLED == "cancelled"


# ---------------------------------------------------------------------------
# AgentRun model
# ---------------------------------------------------------------------------


class TestAgentRun:
    def test_default_state_is_created(self):
        run = AgentRun(session_id="s1", prompt="hello")
        assert run.state == RunState.CREATED

    def test_has_auto_generated_id(self):
        run = AgentRun(session_id="s1", prompt="hello")
        assert run.id
        assert len(run.id) > 0

    def test_two_runs_have_different_ids(self):
        r1 = AgentRun(session_id="s1", prompt="a")
        r2 = AgentRun(session_id="s1", prompt="b")
        assert r1.id != r2.id

    def test_is_terminal_for_completed(self):
        run = AgentRun(session_id="s1", prompt="x", state=RunState.COMPLETED)
        assert run.is_terminal() is True

    def test_is_terminal_for_failed(self):
        run = AgentRun(session_id="s1", prompt="x", state=RunState.FAILED)
        assert run.is_terminal() is True

    def test_is_terminal_for_cancelled(self):
        run = AgentRun(session_id="s1", prompt="x", state=RunState.CANCELLED)
        assert run.is_terminal() is True

    def test_is_not_terminal_for_in_progress(self):
        run = AgentRun(session_id="s1", prompt="x", state=RunState.IN_PROGRESS)
        assert run.is_terminal() is False

    def test_is_not_terminal_for_created(self):
        run = AgentRun(session_id="s1", prompt="x", state=RunState.CREATED)
        assert run.is_terminal() is False

    def test_is_not_terminal_for_awaiting_input(self):
        run = AgentRun(session_id="s1", prompt="x", state=RunState.AWAITING_INPUT)
        assert run.is_terminal() is False


# ---------------------------------------------------------------------------
# PendingAction model
# ---------------------------------------------------------------------------


class TestPendingAction:
    def test_basic_construction(self):
        action = PendingAction(tool="run_command", arguments={"cmd": "ls"}, preview="List files")
        assert action.tool == "run_command"
        assert action.arguments == {"cmd": "ls"}
        assert action.preview == "List files"

    def test_empty_arguments_default(self):
        action = PendingAction(tool="cancel_command", preview="Cancel running command")
        assert action.arguments == {}


# ---------------------------------------------------------------------------
# RunEvent model
# ---------------------------------------------------------------------------


class TestRunEvent:
    def test_text_event(self):
        event = RunEvent(type="text", data="hello")
        assert event.type == "text"
        assert event.data == "hello"

    def test_completed_event(self):
        event = RunEvent(type="completed", data={"result": "done"})
        assert event.type == "completed"

    def test_error_event(self):
        event = RunEvent(type="error", data={"message": "oops"})
        assert event.type == "error"

    def test_data_can_be_none(self):
        event = RunEvent(type="cancelled")
        assert event.data is None


# ---------------------------------------------------------------------------
# RunManager.create
# ---------------------------------------------------------------------------


class TestRunManagerCreate:
    async def test_creates_run_in_created_state(self, run_manager: RunManager):
        run = await run_manager.create("session-1", "do something")
        assert run.state == RunState.CREATED

    async def test_run_has_correct_session_id(self, run_manager: RunManager):
        run = await run_manager.create("session-abc", "hello")
        assert run.session_id == "session-abc"

    async def test_run_has_correct_prompt(self, run_manager: RunManager):
        run = await run_manager.create("s", "my prompt")
        assert run.prompt == "my prompt"

    async def test_run_is_persisted(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        # Fetch from storage directly
        retrieved = await run_manager.get(run.id)
        assert retrieved is not None
        assert retrieved.id == run.id

    async def test_multiple_runs_have_unique_ids(self, run_manager: RunManager):
        r1 = await run_manager.create("s", "a")
        r2 = await run_manager.create("s", "b")
        assert r1.id != r2.id


# ---------------------------------------------------------------------------
# RunManager.get
# ---------------------------------------------------------------------------


class TestRunManagerGet:
    async def test_get_existing_run(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        fetched = await run_manager.get(run.id)
        assert fetched is not None
        assert fetched.id == run.id

    async def test_get_nonexistent_returns_none(self, run_manager: RunManager):
        result = await run_manager.get("nonexistent-id")
        assert result is None

    async def test_get_loads_from_storage(self, run_manager: RunManager, storage: Storage):
        """Clearing in-memory cache should still load from storage."""
        run = await run_manager.create("s", "persisted")
        # Clear the in-memory cache
        run_manager._runs.clear()
        fetched = await run_manager.get(run.id)
        assert fetched is not None
        assert fetched.prompt == "persisted"


# ---------------------------------------------------------------------------
# RunManager.transition
# ---------------------------------------------------------------------------


class TestRunManagerTransition:
    async def test_created_to_in_progress(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        updated = await run_manager.transition(run.id, RunState.IN_PROGRESS)
        assert updated.state == RunState.IN_PROGRESS

    async def test_created_to_cancelled(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        updated = await run_manager.transition(run.id, RunState.CANCELLED)
        assert updated.state == RunState.CANCELLED

    async def test_in_progress_to_completed(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        updated = await run_manager.transition(run.id, RunState.COMPLETED)
        assert updated.state == RunState.COMPLETED

    async def test_in_progress_to_failed(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        updated = await run_manager.transition(run.id, RunState.FAILED)
        assert updated.state == RunState.FAILED

    async def test_in_progress_to_awaiting_input(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        updated = await run_manager.transition(run.id, RunState.AWAITING_INPUT)
        assert updated.state == RunState.AWAITING_INPUT

    async def test_awaiting_input_to_in_progress(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        await run_manager.transition(run.id, RunState.AWAITING_INPUT)
        updated = await run_manager.transition(run.id, RunState.IN_PROGRESS)
        assert updated.state == RunState.IN_PROGRESS

    async def test_invalid_transition_raises_value_error(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        with pytest.raises(ValueError, match="Cannot transition"):
            await run_manager.transition(run.id, RunState.COMPLETED)

    async def test_terminal_to_any_raises_value_error(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        await run_manager.transition(run.id, RunState.COMPLETED)
        with pytest.raises(ValueError):
            await run_manager.transition(run.id, RunState.IN_PROGRESS)

    async def test_unknown_run_raises_key_error(self, run_manager: RunManager):
        with pytest.raises(KeyError):
            await run_manager.transition("nonexistent", RunState.IN_PROGRESS)

    async def test_transition_updates_updated_at(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        original_updated = run.updated_at
        updated = await run_manager.transition(run.id, RunState.IN_PROGRESS)
        assert updated.updated_at >= original_updated

    async def test_transition_clears_pending_action(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        action = PendingAction(tool="run_command", arguments={"cmd": "ls"}, preview="List files")
        await run_manager.set_pending_action(run.id, action)
        # Transition back to IN_PROGRESS clears the pending action
        updated = await run_manager.transition(run.id, RunState.IN_PROGRESS)
        assert updated.pending_action is None


# ---------------------------------------------------------------------------
# RunManager.set_pending_action
# ---------------------------------------------------------------------------


class TestRunManagerSetPendingAction:
    async def test_sets_action_and_transitions_to_awaiting(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        action = PendingAction(tool="run_command", arguments={"cmd": "rm -rf /"}, preview="Delete everything")
        await run_manager.set_pending_action(run.id, action)
        updated = await run_manager.get(run.id)
        assert updated is not None
        assert updated.state == RunState.AWAITING_INPUT
        assert updated.pending_action is not None
        assert updated.pending_action.tool == "run_command"

    async def test_raises_if_wrong_state(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        action = PendingAction(tool="run_command", arguments={}, preview="test")
        with pytest.raises(ValueError, match="IN_PROGRESS or AWAITING_INPUT"):
            await run_manager.set_pending_action(run.id, action)


# ---------------------------------------------------------------------------
# RunManager.resolve_pending
# ---------------------------------------------------------------------------


class TestRunManagerResolvePending:
    async def test_approve_transitions_to_in_progress(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        action = PendingAction(tool="run_command", arguments={}, preview="test")
        await run_manager.set_pending_action(run.id, action)
        updated = await run_manager.resolve_pending(run.id, approved=True)
        assert updated.state == RunState.IN_PROGRESS

    async def test_deny_transitions_to_cancelled(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        action = PendingAction(tool="run_command", arguments={}, preview="test")
        await run_manager.set_pending_action(run.id, action)
        updated = await run_manager.resolve_pending(run.id, approved=False)
        assert updated.state == RunState.CANCELLED

    async def test_raises_if_not_awaiting_input(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        with pytest.raises(ValueError, match="AWAITING_INPUT"):
            await run_manager.resolve_pending(run.id, approved=True)


# ---------------------------------------------------------------------------
# RunManager.complete / fail / cancel
# ---------------------------------------------------------------------------


class TestRunManagerTerminalMethods:
    async def test_complete_sets_result_and_state(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        updated = await run_manager.complete(run.id, "the answer")
        assert updated.state == RunState.COMPLETED
        assert updated.result == "the answer"

    async def test_fail_sets_error_and_state(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        updated = await run_manager.fail(run.id, "something broke")
        assert updated.state == RunState.FAILED
        assert updated.error == "something broke"

    async def test_cancel_in_progress_run(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        updated = await run_manager.cancel(run.id)
        assert updated.state == RunState.CANCELLED

    async def test_cancel_is_idempotent_for_terminal_runs(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        await run_manager.transition(run.id, RunState.IN_PROGRESS)
        await run_manager.complete(run.id, "done")
        # Cancelling an already-completed run should not raise
        result = await run_manager.cancel(run.id)
        assert result.state == RunState.COMPLETED  # unchanged

    async def test_cancel_created_run(self, run_manager: RunManager):
        run = await run_manager.create("s", "test")
        updated = await run_manager.cancel(run.id)
        assert updated.state == RunState.CANCELLED


# ---------------------------------------------------------------------------
# RunManager.list_for_session
# ---------------------------------------------------------------------------


class TestRunManagerListForSession:
    async def test_returns_runs_for_session(self, run_manager: RunManager):
        r1 = await run_manager.create("session-A", "first")
        r2 = await run_manager.create("session-A", "second")
        _r3 = await run_manager.create("session-B", "other")
        runs = await run_manager.list_for_session("session-A")
        ids = {r.id for r in runs}
        assert r1.id in ids
        assert r2.id in ids

    async def test_does_not_include_other_sessions(self, run_manager: RunManager):
        _r1 = await run_manager.create("session-A", "first")
        r2 = await run_manager.create("session-B", "other")
        runs = await run_manager.list_for_session("session-A")
        ids = {r.id for r in runs}
        assert r2.id not in ids

    async def test_empty_session_returns_empty_list(self, run_manager: RunManager):
        runs = await run_manager.list_for_session("nonexistent-session")
        assert runs == []
