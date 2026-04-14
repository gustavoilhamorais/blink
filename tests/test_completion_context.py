"""Tests for CompletionContext."""

from __future__ import annotations

from datetime import UTC, datetime

from blink.completions.context import CompletionContext


class TestCompletionContextDefaults:
    def test_default_values(self) -> None:
        ctx = CompletionContext()
        assert ctx.buffer == ""
        assert ctx.cursor_position == 0
        assert ctx.shell == "bash"
        assert ctx.cwd == ""
        assert ctx.repo_root is None
        assert ctx.recent_commands == []
        assert ctx.recent_failures == []
        assert ctx.visible_files == []
        assert ctx.git_branch is None
        assert isinstance(ctx.timestamp, datetime)

    def test_timestamp_is_utc(self) -> None:
        ctx = CompletionContext()
        assert ctx.timestamp.tzinfo is not None

    def test_explicit_timestamp(self) -> None:
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        ctx = CompletionContext(timestamp=ts)
        assert ctx.timestamp == ts


class TestCompletionContextConstruction:
    def test_full_context(self) -> None:
        ctx = CompletionContext(
            buffer="git sta",
            cursor_position=7,
            shell="zsh",
            cwd="/home/user/project",
            repo_root="/home/user/project",
            recent_commands=["git log", "ls -la"],
            recent_failures=["make fail"],
            visible_files=["README.md", "src", "tests"],
            git_branch="main",
        )
        assert ctx.buffer == "git sta"
        assert ctx.cursor_position == 7
        assert ctx.shell == "zsh"
        assert ctx.cwd == "/home/user/project"
        assert ctx.repo_root == "/home/user/project"
        assert ctx.recent_commands == ["git log", "ls -la"]
        assert ctx.recent_failures == ["make fail"]
        assert ctx.visible_files == ["README.md", "src", "tests"]
        assert ctx.git_branch == "main"

    def test_fish_shell(self) -> None:
        ctx = CompletionContext(shell="fish")
        assert ctx.shell == "fish"


class TestCurrentToken:
    def test_empty_buffer(self) -> None:
        ctx = CompletionContext(buffer="", cursor_position=0)
        assert ctx.current_token == ""

    def test_single_word(self) -> None:
        ctx = CompletionContext(buffer="git", cursor_position=3)
        assert ctx.current_token == "git"

    def test_partial_word(self) -> None:
        ctx = CompletionContext(buffer="git sta", cursor_position=7)
        assert ctx.current_token == "sta"

    def test_cursor_in_middle(self) -> None:
        # Cursor is after "git " — token should be empty (trailing space).
        ctx = CompletionContext(buffer="git stat", cursor_position=4)
        # "git " up to position 4 → last token is "git"
        assert ctx.current_token == "git"

    def test_whitespace_only(self) -> None:
        ctx = CompletionContext(buffer="   ", cursor_position=3)
        assert ctx.current_token == ""


class TestTypedWords:
    def test_empty_buffer(self) -> None:
        ctx = CompletionContext(buffer="", cursor_position=0)
        assert ctx.typed_words == []

    def test_single_command(self) -> None:
        ctx = CompletionContext(buffer="ls", cursor_position=2)
        assert ctx.typed_words == ["ls"]

    def test_command_with_args(self) -> None:
        ctx = CompletionContext(buffer="git commit -m", cursor_position=13)
        assert ctx.typed_words == ["git", "commit", "-m"]

    def test_cursor_before_end(self) -> None:
        # Only text up to cursor_position is considered
        ctx = CompletionContext(buffer="git commit -m 'msg'", cursor_position=10)
        assert ctx.typed_words == ["git", "commit"]


class TestSerialization:
    def test_round_trip_json(self) -> None:
        ctx = CompletionContext(
            buffer="ls -la",
            cursor_position=6,
            shell="bash",
            cwd="/tmp",
            git_branch="main",
        )
        restored = CompletionContext.model_validate_json(ctx.model_dump_json())
        assert restored.buffer == ctx.buffer
        assert restored.shell == ctx.shell
        assert restored.cwd == ctx.cwd
        assert restored.git_branch == ctx.git_branch
