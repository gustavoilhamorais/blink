"""Input sanitization utilities.

Provides helpers to sanitize shell commands and file paths before they are
passed to subprocesses or stored in the database.  The goal is to prevent:

- Shell injection via null bytes and other control characters
- Path traversal attacks (``..`` components in file paths)
- Excessively long inputs that could cause memory or performance issues

These are *defence-in-depth* measures — the primary security boundary is the
capability checker (:mod:`blink.security.capabilities`) and user confirmation.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum lengths to prevent denial-of-service via huge inputs
_MAX_COMMAND_LENGTH = 8192
_MAX_PATH_LENGTH = 4096
_MAX_ARGUMENT_LENGTH = 65536

# Control characters to strip (except common ones like \t, \n, \r)
_STRIP_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)

# ANSI escape sequences (for sanitising text that will be displayed)
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Suspicious shell metacharacters when used in certain contexts
# (This is NOT meant to be a complete blacklist — shell commands CAN contain
# these; it is only used when caller specifically requests strict sanitisation.)
_SHELL_INJECTION_PATTERN = re.compile(
    r"[;\|&`\$\(\)\{\}\<\>]"
)


# ---------------------------------------------------------------------------
# Command sanitization
# ---------------------------------------------------------------------------


def sanitize_command(cmd: str, *, strict: bool = False) -> str:
    """Sanitize a shell command string for safe execution.

    This function:

    1. Truncates the command to :data:`_MAX_COMMAND_LENGTH` characters.
    2. Removes null bytes (``\\x00``) and other dangerous control characters
       that could cause issues with ``exec``-family calls.
    3. Normalises Unicode to NFC form to prevent homoglyph attacks.
    4. Optionally (``strict=True``) removes shell metacharacters.  Only use
       this when building commands programmatically, not for user-typed input.

    Args:
        cmd: The raw command string to sanitize.
        strict: If ``True``, strip shell metacharacters in addition to control
                characters.  Default is ``False``.

    Returns:
        The sanitized command string.

    Note:
        This function does *not* perform shell escaping.  Use
        :func:`shlex.quote` when interpolating values into shell strings.
    """
    if not cmd:
        return ""

    # 1. Truncate
    cmd = cmd[:_MAX_COMMAND_LENGTH]

    # 2. Remove null bytes and non-printable control characters
    cmd = cmd.replace("\x00", "")
    cmd = _STRIP_CONTROL_CHARS.sub("", cmd)

    # 3. Normalise Unicode (NFC)
    cmd = unicodedata.normalize("NFC", cmd)

    # 4. Strip shell metacharacters in strict mode
    if strict:
        cmd = _SHELL_INJECTION_PATTERN.sub("", cmd)

    return cmd.strip()


def sanitize_argument(value: str) -> str:
    """Sanitize a single command argument (e.g. a tool parameter value).

    Similar to :func:`sanitize_command` but with a higher length limit and
    ANSI escape removal (suitable for values that will be displayed).

    Args:
        value: The raw argument string.

    Returns:
        The sanitized argument string.
    """
    if not value:
        return ""

    value = value[:_MAX_ARGUMENT_LENGTH]
    value = value.replace("\x00", "")
    value = _STRIP_CONTROL_CHARS.sub("", value)
    value = _ANSI_ESCAPE.sub("", value)
    value = unicodedata.normalize("NFC", value)
    return value


# ---------------------------------------------------------------------------
# Path sanitization
# ---------------------------------------------------------------------------


def sanitize_path(path: str, *, base_dir: str | Path | None = None) -> str:
    """Sanitize a file path to prevent path traversal attacks.

    This function:

    1. Truncates to :data:`_MAX_PATH_LENGTH`.
    2. Removes null bytes and control characters.
    3. Resolves ``..`` components and symlinks (if ``base_dir`` is provided).
    4. If ``base_dir`` is provided, verifies the resolved path is within it.

    Args:
        path: The raw path string.
        base_dir: Optional directory to confine the path to.  If the resolved
                  path would escape this directory, a :exc:`ValueError` is
                  raised.

    Returns:
        The sanitized, normalised path string.

    Raises:
        ValueError: If ``base_dir`` is given and the path escapes it.
    """
    if not path:
        return ""

    # 1. Truncate and strip control chars
    path = path[:_MAX_PATH_LENGTH]
    path = path.replace("\x00", "")
    path = _STRIP_CONTROL_CHARS.sub("", path)
    path = unicodedata.normalize("NFC", path)

    # 2. Normalise using PurePosixPath to collapse redundant separators/dots
    pure = PurePosixPath(path)
    normalised = str(pure)

    # 3. Enforce base_dir containment
    if base_dir is not None:
        base = Path(base_dir).resolve()
        try:
            candidate = (base / normalised).resolve()
        except OSError as exc:
            # Cannot resolve — treat as a traversal attempt
            raise ValueError(
                f"Path {path!r} could not be resolved relative to base directory {base}"
            ) from exc
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                f"Path {path!r} escapes the allowed base directory {base}"
            ) from exc
        return str(candidate)

    return normalised


def is_safe_path(path: str, base_dir: str | Path) -> bool:
    """Return ``True`` if *path* is safely contained within *base_dir*.

    A convenience wrapper around :func:`sanitize_path` that returns a boolean
    instead of raising.

    Args:
        path: The path to check.
        base_dir: The directory the path must be confined to.
    """
    try:
        sanitize_path(path, base_dir=base_dir)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Text sanitization (for display)
# ---------------------------------------------------------------------------


def sanitize_display_text(text: str, *, max_length: int = _MAX_ARGUMENT_LENGTH) -> str:
    """Sanitize arbitrary text for safe terminal display.

    Strips ANSI escape sequences, null bytes, and non-printable control
    characters.  Does *not* HTML-encode — this is for terminal display only.

    Args:
        text: The raw text.
        max_length: Maximum length before truncation.

    Returns:
        The sanitized display string.
    """
    if not text:
        return ""

    text = text[:max_length]
    text = text.replace("\x00", "")
    text = _STRIP_CONTROL_CHARS.sub("", text)
    text = _ANSI_ESCAPE.sub("", text)
    text = unicodedata.normalize("NFC", text)
    return text


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from *text*.

    Useful when comparing terminal output in tests.
    """
    return _ANSI_ESCAPE.sub("", text)


__all__ = [
    "sanitize_command",
    "sanitize_argument",
    "sanitize_path",
    "is_safe_path",
    "sanitize_display_text",
    "strip_ansi",
]
