"""ACP Run Manager.

State machine for agent runs: tracks lifecycle from CREATED through
IN_PROGRESS, AWAITING_INPUT, COMPLETED, FAILED, or CANCELLED.
Runs are stored both in memory (fast access) and in SQLite (durability).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from blink.storage import Storage

# ---------------------------------------------------------------------------
# State and event models
# ---------------------------------------------------------------------------


class RunState(str):
    """Valid run state values (use as string constants)."""

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Valid state transitions
_TRANSITIONS: dict[str, set[str]] = {
    RunState.CREATED: {RunState.IN_PROGRESS, RunState.CANCELLED},
    RunState.IN_PROGRESS: {
        RunState.AWAITING_INPUT,
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
    },
    RunState.AWAITING_INPUT: {
        RunState.IN_PROGRESS,
        RunState.FAILED,
        RunState.CANCELLED,
    },
    RunState.COMPLETED: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}


class PendingAction(BaseModel):
    """Action awaiting user confirmation."""

    tool: str
    """MCP tool name that will be called."""

    arguments: dict[str, Any] = Field(default_factory=dict)
    """Arguments that will be passed to the tool."""

    preview: str
    """Human-readable description of what will happen (shown to user)."""


class RunEvent(BaseModel):
    """Event emitted during an agent run.

    Event types:
    - ``text``        — agent generated text output
    - ``tool_call``   — agent is calling a tool (before execution)
    - ``tool_result`` — tool returned a result
    - ``awaiting``    — run paused, waiting for user confirmation
    - ``completed``   — run finished successfully
    - ``error``       — run failed with an error
    - ``cancelled``   — run was cancelled
    """

    type: str
    data: Any = None


class AgentRun(BaseModel):
    """Represents a single agent run lifecycle."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    prompt: str
    state: str = RunState.CREATED
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    result: str | None = None
    error: str | None = None
    pending_action: PendingAction | None = None

    def is_terminal(self) -> bool:
        """Return True if the run is in a terminal state (no further transitions)."""
        return self.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}


# ---------------------------------------------------------------------------
# Schema for SQLite persistence
# ---------------------------------------------------------------------------

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS acp_runs (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    result      TEXT,
    error       TEXT,
    pending_action TEXT
);
CREATE INDEX IF NOT EXISTS idx_acp_runs_session ON acp_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_acp_runs_state   ON acp_runs(state);
"""


# ---------------------------------------------------------------------------
# RunManager
# ---------------------------------------------------------------------------


class RunManager:
    """Manages the lifecycle of agent runs.

    Stores runs in-memory for fast access and persists to SQLite for
    durability across restarts.
    """

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._runs: dict[str, AgentRun] = {}
        self._schema_ready = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_schema(self) -> None:
        if not self._schema_ready:
            # executescript handles multiple statements separated by semicolons
            db = self._storage._ensure_open()
            await db.executescript(_RUNS_SCHEMA)
            await db.commit()
            self._schema_ready = True

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, session_id: str, prompt: str) -> AgentRun:
        """Create a new run in CREATED state and persist it."""
        await self._ensure_schema()
        run = AgentRun(session_id=session_id, prompt=prompt)
        self._runs[run.id] = run
        await self._persist(run)
        return run

    async def get(self, run_id: str) -> AgentRun | None:
        """Return a run by ID (checks in-memory first, then storage)."""
        if run_id in self._runs:
            return self._runs[run_id]
        await self._ensure_schema()
        row = await self._storage.fetchone(
            "SELECT * FROM acp_runs WHERE id = ?", (run_id,)
        )
        if row is None:
            return None
        run = self._row_to_run(row)
        self._runs[run.id] = run
        return run

    async def list_for_session(self, session_id: str) -> list[AgentRun]:
        """Return all runs for a session, ordered by creation time (newest first)."""
        await self._ensure_schema()
        rows = await self._storage.fetchall(
            "SELECT * FROM acp_runs WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        )
        runs = [self._row_to_run(r) for r in rows]
        # Update in-memory cache
        for run in runs:
            self._runs[run.id] = run
        return runs

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    async def transition(self, run_id: str, new_state: str) -> AgentRun:
        """Transition a run to a new state.

        Raises:
            KeyError: If run_id is not found.
            ValueError: If the transition is not valid.
        """
        run = await self._require(run_id)
        allowed = _TRANSITIONS.get(run.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Cannot transition run {run_id!r} from {run.state!r} to {new_state!r}. "
                f"Allowed: {sorted(allowed)}"
            )
        run.state = new_state
        run.updated_at = datetime.now(tz=UTC)
        # Clear pending action when leaving AWAITING_INPUT
        if new_state != RunState.AWAITING_INPUT:
            run.pending_action = None
        await self._persist(run)
        return run

    async def set_pending_action(self, run_id: str, action: PendingAction) -> None:
        """Set a pending action that requires user confirmation.

        The run must be in IN_PROGRESS or AWAITING_INPUT state.
        """
        run = await self._require(run_id)
        if run.state not in {RunState.IN_PROGRESS, RunState.AWAITING_INPUT}:
            raise ValueError(
                f"Cannot set pending action on run in state {run.state!r}. "
                "Run must be IN_PROGRESS or AWAITING_INPUT."
            )
        run.pending_action = action
        run.state = RunState.AWAITING_INPUT
        run.updated_at = datetime.now(tz=UTC)
        await self._persist(run)

    async def resolve_pending(self, run_id: str, approved: bool) -> AgentRun:
        """Resolve a pending action — user confirmed (True) or denied (False).

        If approved, transitions to IN_PROGRESS.
        If denied, transitions to CANCELLED.
        """
        run = await self._require(run_id)
        if run.state != RunState.AWAITING_INPUT:
            raise ValueError(
                f"Run {run_id!r} is not in AWAITING_INPUT state (current: {run.state!r})."
            )
        if approved:
            new_state = RunState.IN_PROGRESS
        else:
            new_state = RunState.CANCELLED
        return await self.transition(run_id, new_state)

    async def complete(self, run_id: str, result: str) -> AgentRun:
        """Mark a run as successfully completed with the given result."""
        run = await self._require(run_id)
        run.result = result
        run.updated_at = datetime.now(tz=UTC)
        await self._persist(run)
        return await self.transition(run_id, RunState.COMPLETED)

    async def fail(self, run_id: str, error: str) -> AgentRun:
        """Mark a run as failed with an error message."""
        run = await self._require(run_id)
        run.error = error
        run.updated_at = datetime.now(tz=UTC)
        await self._persist(run)
        return await self.transition(run_id, RunState.FAILED)

    async def cancel(self, run_id: str) -> AgentRun:
        """Cancel an in-progress run."""
        run = await self._require(run_id)
        if run.is_terminal():
            return run  # Already terminal; idempotent cancel
        return await self.transition(run_id, RunState.CANCELLED)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _require(self, run_id: str) -> AgentRun:
        """Return a run or raise KeyError."""
        run = await self.get(run_id)
        if run is None:
            raise KeyError(f"Run {run_id!r} not found.")
        return run

    async def _persist(self, run: AgentRun) -> None:
        """Upsert a run to SQLite."""
        pending_json = (
            run.pending_action.model_dump_json() if run.pending_action else None
        )
        await self._storage.execute(
            """
            INSERT INTO acp_runs
                (id, session_id, prompt, state, created_at, updated_at, result, error, pending_action)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state          = excluded.state,
                updated_at     = excluded.updated_at,
                result         = excluded.result,
                error          = excluded.error,
                pending_action = excluded.pending_action
            """,
            (
                run.id,
                run.session_id,
                run.prompt,
                run.state,
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
                run.result,
                run.error,
                pending_json,
            ),
        )

    @staticmethod
    def _row_to_run(row: dict[str, Any]) -> AgentRun:
        """Convert a SQLite row dict to an AgentRun."""
        pending: PendingAction | None = None
        if row.get("pending_action"):
            try:
                pending = PendingAction.model_validate_json(row["pending_action"])
            except Exception:  # noqa: BLE001
                pending = None

        return AgentRun(
            id=row["id"],
            session_id=row["session_id"],
            prompt=row["prompt"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            result=row.get("result"),
            error=row.get("error"),
            pending_action=pending,
        )


__all__ = [
    "AgentRun",
    "PendingAction",
    "RunEvent",
    "RunManager",
    "RunState",
]
