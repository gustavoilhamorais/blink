"""Tests for OSC 133 block tracker."""

from __future__ import annotations

from datetime import UTC

from blink.kitty.block_tracker import Block, BlockTracker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OSC_A = "\x1b]133;A\x07"
OSC_B = "\x1b]133;B\x07"
OSC_C = "\x1b]133;C\x07"


def osc_d(exit_code: int = 0) -> str:
    return f"\x1b]133;D;{exit_code}\x07"


def make_command_block(prompt: str, command: str, output: str, exit_code: int = 0) -> str:
    """Build a synthetic terminal sequence containing one full command block."""
    return (
        f"{OSC_A}{prompt}{OSC_B}{command}{OSC_C}{output}{osc_d(exit_code)}"
    )


# ---------------------------------------------------------------------------
# Basic mark parsing
# ---------------------------------------------------------------------------


class TestBlockTrackerBasic:
    def test_parse_single_command(self) -> None:
        tracker = BlockTracker()
        text = make_command_block("$ ", "echo hello", "hello")
        blocks = tracker.parse_screen(text)
        assert len(blocks) == 1
        b = blocks[0]
        assert b.command == "echo hello"
        assert b.output == "hello"
        assert b.exit_code == 0

    def test_parse_command_exit_code_nonzero(self) -> None:
        tracker = BlockTracker()
        text = make_command_block("$ ", "false", "", exit_code=1)
        blocks = tracker.parse_screen(text)
        assert len(blocks) == 1
        assert blocks[0].exit_code == 1

    def test_parse_multiple_commands(self) -> None:
        tracker = BlockTracker()
        text = (
            make_command_block("$ ", "ls", "file1.txt\nfile2.txt")
            + make_command_block("$ ", "pwd", "/home/user")
        )
        blocks = tracker.parse_screen(text)
        assert len(blocks) == 2
        assert blocks[0].command == "ls"
        assert blocks[1].command == "pwd"

    def test_prompt_captured(self) -> None:
        tracker = BlockTracker()
        text = make_command_block("user@host:~$ ", "date", "Mon Jan  1 00:00:00 UTC 2024")
        blocks = tracker.parse_screen(text)
        assert blocks[0].prompt == "user@host:~$ "

    def test_multiline_output(self) -> None:
        tracker = BlockTracker()
        output = "line1\nline2\nline3"
        text = make_command_block("$ ", "cat file.txt", output)
        blocks = tracker.parse_screen(text)
        assert "line1" in blocks[0].output
        assert "line3" in blocks[0].output


# ---------------------------------------------------------------------------
# Incremental feeding
# ---------------------------------------------------------------------------


class TestBlockTrackerFeed:
    def test_incremental_feed(self) -> None:
        tracker = BlockTracker()
        part1 = f"{OSC_A}$ {OSC_B}echo hi{OSC_C}"
        part2 = f"hi{osc_d(0)}"
        newly1 = tracker.feed(part1)
        assert len(newly1) == 0  # no D mark yet
        newly2 = tracker.feed(part2)
        assert len(newly2) == 1
        assert newly2[0].command == "echo hi"

    def test_current_block_before_completion(self) -> None:
        tracker = BlockTracker()
        tracker.feed(f"{OSC_A}$ {OSC_B}ls{OSC_C}")
        current = tracker.get_current_block()
        assert current is not None
        assert current.command == "ls"
        assert not current.is_complete

    def test_current_block_none_after_completion(self) -> None:
        tracker = BlockTracker()
        tracker.feed(make_command_block("$ ", "pwd", "/tmp"))
        assert tracker.get_current_block() is None

    def test_get_completed_blocks(self) -> None:
        tracker = BlockTracker()
        tracker.feed(make_command_block("$ ", "cmd1", "out1"))
        tracker.feed(make_command_block("$ ", "cmd2", "out2"))
        completed = tracker.get_completed_blocks()
        assert len(completed) == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestBlockTrackerEdgeCases:
    def test_empty_string(self) -> None:
        tracker = BlockTracker()
        blocks = tracker.parse_screen("")
        assert blocks == []

    def test_no_osc_marks(self) -> None:
        tracker = BlockTracker()
        blocks = tracker.parse_screen("just plain text no marks here")
        assert blocks == []

    def test_partial_block_no_d(self) -> None:
        tracker = BlockTracker()
        # A/B/C present but no D
        blocks = tracker.parse_screen(f"{OSC_A}$ {OSC_B}ls{OSC_C}output")
        assert blocks == []
        assert tracker.get_current_block() is not None

    def test_ansi_stripped_from_command(self) -> None:
        tracker = BlockTracker()
        # Bold escape around command text
        text = f"{OSC_A}$ {OSC_B}\x1b[1mls -la\x1b[0m{OSC_C}\x1b[0m{osc_d(0)}"
        blocks = tracker.parse_screen(text)
        assert blocks[0].command == "ls -la"

    def test_st_terminated_osc(self) -> None:
        """Handles ESC \\ (ST) as OSC terminator as well as BEL."""
        tracker = BlockTracker()
        # Use ST terminator instead of BEL
        osc_a_st = "\x1b]133;A\x1b\\"
        osc_b_st = "\x1b]133;B\x1b\\"
        osc_c_st = "\x1b]133;C\x1b\\"
        osc_d_st = "\x1b]133;D;0\x1b\\"
        text = f"{osc_a_st}$ {osc_b_st}echo test{osc_c_st}test\n{osc_d_st}"
        blocks = tracker.parse_screen(text)
        assert len(blocks) == 1
        assert blocks[0].command == "echo test"

    def test_reset_clears_state(self) -> None:
        tracker = BlockTracker()
        tracker.feed(make_command_block("$ ", "ls", "files"))
        tracker.reset()
        assert tracker.get_completed_blocks() == []
        assert tracker.get_current_block() is None

    def test_duration_set_on_completed_block(self) -> None:
        tracker = BlockTracker()
        tracker.feed(make_command_block("$ ", "sleep 0", ""))
        blocks = tracker.get_completed_blocks()
        assert blocks[0].duration is not None
        assert blocks[0].duration >= 0

    def test_block_to_dict(self) -> None:
        tracker = BlockTracker()
        tracker.feed(make_command_block("$ ", "echo hi", "hi"))
        d = tracker.get_completed_blocks()[0].to_dict()
        assert d["command"] == "echo hi"
        assert "id" in d
        assert d["exit_code"] == 0

    def test_block_id_unique(self) -> None:
        tracker = BlockTracker()
        tracker.feed(make_command_block("$ ", "cmd1", ""))
        tracker.feed(make_command_block("$ ", "cmd2", ""))
        ids = [b.id for b in tracker.get_completed_blocks()]
        assert len(set(ids)) == 2


# ---------------------------------------------------------------------------
# Block dataclass
# ---------------------------------------------------------------------------


class TestBlockDataclass:
    def test_default_values(self) -> None:
        b = Block()
        assert b.command == ""
        assert b.exit_code is None
        assert b.started_at is None
        assert b.duration is None
        assert not b.is_complete

    def test_is_complete(self) -> None:
        from datetime import datetime

        b = Block()
        assert not b.is_complete
        b.started_at = datetime.now(tz=UTC)
        b.ended_at = datetime.now(tz=UTC)
        assert b.is_complete
