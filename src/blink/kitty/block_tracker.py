"""Block tracker — parses OSC 133 semantic shell marks.

OSC 133 defines four marks that bracket command lifecycle:
  A  — prompt start
  B  — prompt end  (user begins typing)
  C  — command start  (Enter pressed, command executing)
  D;exit_code  — command end

Wire format:  ESC ] 133 ; <mark> BEL
              i.e. \\x1b]133;<mark>\\x07

This module accumulates lines from terminal output and reconstructs
structured :class:`Block` objects.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# OSC 133 escape sequences
# ESC ] 133 ; X BEL  or  ESC ] 133 ; X ST  (ST = ESC \\)
_OSC_133_RE = re.compile(
    r"\x1b\]133;([A-D](?:;\d+)?)\x07"
    r"|"
    r"\x1b\]133;([A-D](?:;\d+)?)\x1b\\",
)

# Strip all ANSI escape sequences for clean text extraction
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|][^\x07]*\x07|][^\x1b]*\x1b\\|.)")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class Block:
    """A completed (or in-progress) shell command block."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    prompt: str = ""
    command: str = ""
    output: str = ""
    cwd: str = ""
    exit_code: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def duration(self) -> float | None:
        """Return duration in seconds, or None if not yet ended."""
        if self.started_at is None or self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def is_complete(self) -> bool:
        return self.ended_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "command": self.command,
            "output": self.output,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration": self.duration,
        }


class BlockTracker:
    """Stateful parser for OSC 133 shell marks.

    Feed screen text via :meth:`parse_screen` (which replaces all state)
    or incrementally via :meth:`feed` (which appends).

    The tracker maintains a list of completed blocks and an optional
    in-progress block (mark A/B/C seen but no D yet).
    """

    def __init__(self) -> None:
        self._completed: list[Block] = []
        self._current: Block | None = None
        # Internal parse state machine
        self._state: str = "idle"  # idle | prompt | typing | running
        self._prompt_buf: str = ""
        self._cmd_buf: str = ""
        self._output_buf: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_screen(self, text: str) -> list[Block]:
        """Parse a full screen snapshot; resets all prior state.

        Args:
            text: Raw terminal text including OSC 133 escape sequences.

        Returns:
            List of completed :class:`Block` objects found in *text*.
        """
        self.reset()
        self.feed(text)
        return list(self._completed)

    def feed(self, text: str) -> list[Block]:
        """Feed more terminal text and return any newly completed blocks.

        Args:
            text: Raw terminal text (may contain partial escape sequences).

        Returns:
            List of :class:`Block` objects completed during this feed.
        """
        newly_completed: list[Block] = []
        pos = 0

        for match in _OSC_133_RE.finditer(text):
            mark_full = match.group(1) or match.group(2)
            # Text between previous position and this marker
            segment = text[pos : match.start()]
            self._accumulate(segment)
            pos = match.end()

            # Parse the mark letter and optional exit code
            if ";" in mark_full:
                letter, _, rest = mark_full.partition(";")
                try:
                    exit_code: int | None = int(rest)
                except ValueError:
                    exit_code = None
            else:
                letter = mark_full
                exit_code = None

            completed = self._handle_mark(letter, exit_code)
            if completed is not None:
                newly_completed.append(completed)

        # Accumulate any trailing text after the last marker
        self._accumulate(text[pos:])
        return newly_completed

    def get_current_block(self) -> Block | None:
        """Return the in-progress block (no D mark yet), or None."""
        return self._current

    def get_completed_blocks(self) -> list[Block]:
        """Return all completed blocks seen so far."""
        return list(self._completed)

    def reset(self) -> None:
        """Clear all state."""
        self._completed = []
        self._current = None
        self._state = "idle"
        self._prompt_buf = ""
        self._cmd_buf = ""
        self._output_buf = ""

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _accumulate(self, text: str) -> None:
        """Route text into the correct buffer based on current state."""
        if not text:
            return
        clean = _strip_ansi(text)
        if self._state == "prompt":
            self._prompt_buf += clean
        elif self._state == "typing":
            self._cmd_buf += clean
        elif self._state == "running":
            self._output_buf += clean

    def _handle_mark(self, letter: str, exit_code: int | None) -> Block | None:
        """Process a single OSC 133 mark; return a Block if one completed."""
        if letter == "A":
            # Prompt start — begin a new block
            self._current = Block(started_at=_now())
            self._prompt_buf = ""
            self._cmd_buf = ""
            self._output_buf = ""
            self._state = "prompt"

        elif letter == "B":
            # Prompt end — user starts typing the command
            if self._current is not None:
                self._current.prompt = self._prompt_buf.rstrip("\n\r")
            self._cmd_buf = ""
            self._state = "typing"

        elif letter == "C":
            # Command start — Enter was pressed
            if self._current is not None:
                self._current.command = self._cmd_buf.strip()
            self._output_buf = ""
            self._state = "running"

        elif letter == "D":
            # Command end — finalize the block
            if self._current is not None:
                block = self._current
                block.output = self._output_buf.rstrip()
                block.exit_code = exit_code
                block.ended_at = _now()
                self._completed.append(block)
                self._current = None
                self._state = "idle"
                return block

        return None


__all__ = ["Block", "BlockTracker"]
