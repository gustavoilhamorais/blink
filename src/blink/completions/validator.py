"""Completion validator.

Validates completion candidates before they are returned to the caller:

1. Shell-syntax check  — parse the command with the ``shlex`` module (POSIX
   mode) to catch unbalanced quotes and other lexical errors.
2. Optional path check — if *check_paths* is True, verify that the first
   token (the command name) is a known executable on PATH *or* an existing
   file path. Only commands that look like filesystem paths are checked; bare
   words like ``git`` are always considered valid.

The validator is deliberately lenient: it filters out only clearly broken
completions (unparseable syntax) so that exotic but valid shell idioms are
not accidentally rejected.
"""

from __future__ import annotations

import os
import shlex
import shutil

import anyio

from blink.completions.context import CompletionContext
from blink.completions.ranker import Completion


class Validator:
    """Validate and optionally clean completion candidates."""

    def __init__(self, check_paths: bool = False) -> None:
        """
        Parameters
        ----------
        check_paths:
            When True, verify that file-path-like first tokens actually exist.
            Disabled by default to avoid false negatives for commands that
            create files or use complex glob syntax.
        """
        self._check_paths = check_paths

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def validate(
        self, completion: Completion, context: CompletionContext
    ) -> Completion | None:
        """Validate a single completion.

        Returns the (possibly cleaned) completion, or ``None`` if invalid.
        """
        text = completion.text.strip()
        if not text:
            return None

        # 1. Syntax check via shlex
        if not self._is_valid_syntax(text):
            return None

        # 2. Optional path existence check
        if self._check_paths and not await self._first_token_ok(text):
            return None

        # Return a clean copy with stripped whitespace
        if text != completion.text:
            return completion.model_copy(update={"text": text, "display": text})
        return completion

    async def validate_all(
        self, completions: list[Completion], context: CompletionContext | None = None
    ) -> list[Completion]:
        """Validate all completions and return only the valid ones.

        The order of the input list is preserved.
        """
        if context is None:
            context = CompletionContext()

        results: list[Completion] = []
        for comp in completions:
            validated = await self.validate(comp, context)
            if validated is not None:
                results.append(validated)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_valid_syntax(text: str) -> bool:
        """Return True if *text* can be lexed without errors by shlex."""
        try:
            shlex.split(text, posix=True)
            return True
        except ValueError:
            return False

    async def _first_token_ok(self, text: str) -> bool:
        """Return True if the first token is an accessible command or file."""
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            return False

        if not tokens:
            return True

        first = tokens[0]

        # Shell built-ins and common metacharacters — always allow
        _SHELL_BUILTINS = frozenset(
            {
                "cd", "echo", "export", "source", ".", "alias", "unalias",
                "set", "unset", "eval", "exec", "exit", "return", "true",
                "false", "test", "[", "[[", "]]", "if", "then", "else",
                "elif", "fi", "for", "do", "done", "while", "until", "case",
                "esac", "function", "time", "!", ":", "read", "printf",
                "local", "declare", "typeset", "let", "shift", "trap",
                "break", "continue", "bg", "fg", "jobs", "wait", "kill",
                "pwd", "umask", "ulimit", "hash", "type", "command",
                "builtin", "enable",
            }
        )
        if first in _SHELL_BUILTINS:
            return True

        # Absolute or relative path — check existence
        if "/" in first:
            return await anyio.to_thread.run_sync(lambda: os.path.exists(first))

        # Bare word — check PATH
        return await anyio.to_thread.run_sync(lambda: shutil.which(first) is not None)


__all__ = ["Validator"]
