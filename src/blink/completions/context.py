"""Completion context model.

CompletionContext captures everything the ranker and provider need to generate
relevant shell completions: the current buffer, cursor position, shell type,
working directory, git state, recent history, and visible files.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class CompletionContext(BaseModel):
    """Full context snapshot used to generate shell completions."""

    buffer: str = ""
    """Current command line buffer (what the user has typed so far)."""

    cursor_position: int = 0
    """Cursor position within the buffer (0 = start)."""

    shell: str = "bash"
    """Shell type: 'bash', 'zsh', or 'fish'."""

    cwd: str = ""
    """Current working directory."""

    repo_root: str | None = None
    """Absolute path of the git repository root, or None if not in a repo."""

    recent_commands: list[str] = Field(default_factory=list)
    """Last N commands executed in any directory (most recent first)."""

    recent_failures: list[str] = Field(default_factory=list)
    """Commands that exited with a non-zero exit code recently."""

    visible_files: list[str] = Field(default_factory=list)
    """File/directory names visible in cwd (from a plain `ls`)."""

    git_branch: str | None = None
    """Current git branch name, or None if not in a repo / no commits."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    """When this context snapshot was captured."""

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def current_token(self) -> str:
        """Return the word under / before the cursor (the token being completed)."""
        prefix = self.buffer[: self.cursor_position]
        return prefix.split()[-1] if prefix.strip() else ""

    @property
    def typed_words(self) -> list[str]:
        """Return all tokens typed so far (up to the cursor)."""
        return self.buffer[: self.cursor_position].split()


__all__ = ["CompletionContext"]
