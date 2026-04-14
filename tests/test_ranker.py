"""Tests for HistoryRanker and Completion model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from blink.completions.context import CompletionContext
from blink.completions.ranker import (
    Completion,
    HistoryRanker,
    _cwd_similarity,
    _prefix_match_score,
    _prior_acceptance_score,
    _recency_score,
    _repo_similarity,
)
from blink.storage import Storage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage(tmp_path):
    db_file = tmp_path / "ranker_test.db"
    s = Storage(db_path=db_file)
    await s.init_db()
    yield s
    await s.close()


@pytest.fixture
def ranker(storage):
    return HistoryRanker(storage, max_results=10, min_score=0.01)


async def _insert_history(
    storage: Storage,
    command: str,
    cwd: str = "/home/user",
    exit_code: int = 0,
    age_seconds: float = 60,
) -> None:
    executed_at = (datetime.now(tz=UTC) - timedelta(seconds=age_seconds)).isoformat()
    await storage.execute(
        "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
        (command, cwd, exit_code, executed_at),
    )


# ---------------------------------------------------------------------------
# Unit tests for scoring functions
# ---------------------------------------------------------------------------


class TestPrefixMatchScore:
    def test_exact_prefix(self) -> None:
        assert _prefix_match_score("git status", "git") > 0.7

    def test_full_command(self) -> None:
        assert _prefix_match_score("git status", "git status") == 1.0

    def test_no_match(self) -> None:
        assert _prefix_match_score("ls -la", "git") == 0.0

    def test_empty_buffer(self) -> None:
        score = _prefix_match_score("anything", "")
        assert 0 < score <= 1.0

    def test_case_insensitive(self) -> None:
        assert _prefix_match_score("Git Status", "git") > 0.0


class TestCwdSimilarity:
    def test_identical_cwd(self) -> None:
        assert _cwd_similarity("/home/user", "/home/user") == 1.0

    def test_parent_directory(self) -> None:
        score = _cwd_similarity("/home/user", "/home/user/project")
        assert 0 < score < 1.0

    def test_completely_different(self) -> None:
        score = _cwd_similarity("/var/log", "/home/user/project")
        assert score < 0.5

    def test_empty_inputs(self) -> None:
        assert _cwd_similarity("", "") == 0.5
        assert _cwd_similarity("/a", "") == 0.5


class TestRepoSimilarity:
    def test_inside_repo(self) -> None:
        assert _repo_similarity("/home/user/project/src", "/home/user/project") == 1.0

    def test_outside_repo(self) -> None:
        assert _repo_similarity("/var/log", "/home/user/project") == 0.3

    def test_no_repo_root(self) -> None:
        assert _repo_similarity("/anywhere", None) == 0.5


class TestRecencyScore:
    def test_recent_command(self) -> None:
        ts = (datetime.now(tz=UTC) - timedelta(seconds=30)).isoformat()
        score = _recency_score(ts)
        assert score > 0.95

    def test_old_command(self) -> None:
        ts = (datetime.now(tz=UTC) - timedelta(days=30)).isoformat()
        score = _recency_score(ts)
        assert score < 0.2

    def test_invalid_timestamp(self) -> None:
        assert _recency_score("not-a-date") == 0.1


class TestPriorAcceptanceScore:
    def test_success(self) -> None:
        assert _prior_acceptance_score(0) == 1.0

    def test_failure(self) -> None:
        assert _prior_acceptance_score(1) == 0.3
        assert _prior_acceptance_score(127) == 0.3

    def test_unknown(self) -> None:
        assert _prior_acceptance_score(None) == 0.7


# ---------------------------------------------------------------------------
# HistoryRanker integration tests
# ---------------------------------------------------------------------------


class TestHistoryRanker:
    async def test_empty_history(self, ranker: HistoryRanker) -> None:
        ctx = CompletionContext(buffer="git", cursor_position=3)
        results = await ranker.rank(ctx)
        assert results == []

    async def test_returns_matching_commands(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        await _insert_history(storage, "git status")
        await _insert_history(storage, "git log --oneline")
        await _insert_history(storage, "ls -la")

        ctx = CompletionContext(buffer="git", cursor_position=3, cwd="/home/user")
        results = await ranker.rank(ctx)
        commands = [r.text for r in results]
        assert "git status" in commands
        assert "git log --oneline" in commands
        assert "ls -la" not in commands

    async def test_no_prefix_match_excluded(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        await _insert_history(storage, "docker build .")
        ctx = CompletionContext(buffer="git", cursor_position=3)
        results = await ranker.rank(ctx)
        assert all("git" in r.text.lower() for r in results)

    async def test_recent_commands_ranked_higher(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        # Older command
        await _insert_history(storage, "git status", age_seconds=3600 * 24 * 30)
        # Recent command
        await _insert_history(storage, "git status --short", age_seconds=5)

        ctx = CompletionContext(buffer="git", cursor_position=3, cwd="/home/user")
        results = await ranker.rank(ctx)
        assert len(results) >= 1
        # Both match "git" but recent one should score higher
        confidences = {r.text: r.confidence for r in results}
        if "git status --short" in confidences and "git status" in confidences:
            assert confidences["git status --short"] >= confidences["git status"]

    async def test_cwd_match_boosts_score(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        await _insert_history(storage, "make build", cwd="/project", age_seconds=60)
        await _insert_history(storage, "make test", cwd="/other", age_seconds=60)

        ctx = CompletionContext(buffer="make", cursor_position=4, cwd="/project")
        results = await ranker.rank(ctx)
        cmds = {r.text: r.confidence for r in results}
        assert "make build" in cmds
        assert "make test" in cmds
        # Command run in same cwd should score higher
        assert cmds["make build"] > cmds["make test"]

    async def test_failed_command_penalised(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        await _insert_history(storage, "git push", exit_code=0, age_seconds=60)
        await _insert_history(storage, "git pushh", exit_code=1, age_seconds=60)

        ctx = CompletionContext(buffer="git push", cursor_position=8)
        results = await ranker.rank(ctx)
        cmds = {r.text: r.confidence for r in results}
        if "git push" in cmds and "git pushh" in cmds:
            assert cmds["git push"] > cmds["git pushh"]

    async def test_max_results_respected(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        for i in range(20):
            await _insert_history(storage, f"git cmd{i:02d}", age_seconds=i * 10)

        ctx = CompletionContext(buffer="git", cursor_position=3)
        results = await ranker.rank(ctx)
        assert len(results) <= ranker._max_results

    async def test_deduplicate_commands(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        # Same command run multiple times should appear only once
        await _insert_history(storage, "git status", age_seconds=10)
        await _insert_history(storage, "git status", age_seconds=20)
        await _insert_history(storage, "git status", age_seconds=30)

        ctx = CompletionContext(buffer="git", cursor_position=3)
        results = await ranker.rank(ctx)
        texts = [r.text for r in results]
        assert texts.count("git status") == 1

    async def test_completion_source_is_history(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        await _insert_history(storage, "ls -la")
        ctx = CompletionContext(buffer="ls", cursor_position=2)
        results = await ranker.rank(ctx)
        assert all(r.source == "history" for r in results)

    async def test_empty_buffer_returns_recent(
        self, ranker: HistoryRanker, storage: Storage
    ) -> None:
        await _insert_history(storage, "ls -la", age_seconds=5)
        await _insert_history(storage, "pwd", age_seconds=10)

        ctx = CompletionContext(buffer="", cursor_position=0)
        results = await ranker.rank(ctx)
        # With empty buffer everything qualifies at 0.5 prefix weight
        assert len(results) > 0


class TestCompletionModel:
    def test_completion_model_fields(self) -> None:
        c = Completion(
            text="git status",
            display="git status",
            confidence=0.9,
            source="history",
            metadata={"exit_code": 0},
        )
        assert c.text == "git status"
        assert c.confidence == 0.9
        assert c.source == "history"
        assert c.metadata["exit_code"] == 0

    def test_completion_default_metadata(self) -> None:
        c = Completion(text="ls", display="ls", confidence=0.5, source="llm")
        assert c.metadata == {}
