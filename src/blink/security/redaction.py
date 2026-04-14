"""Secret redaction utilities.

Scrubs credentials, API keys, tokens, and other sensitive values from
strings and environment variable dictionaries before they are exposed
through the MCP server or audit log.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Patterns that match common secret forms
# ---------------------------------------------------------------------------

# Each pattern must capture the sensitive value in group 1 or match the
# entire secret span so we can replace just the value with [REDACTED].
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Bearer / Basic / Token authentication headers — must come BEFORE generic key=value
    # so that "Authorization: Bearer <token>" is fully consumed as one redaction.
    (
        re.compile(
            r"(?i)(authorization\s*[=:]\s*)?(?:bearer|basic|token)\s+([\w/+\-\.=]{6,})",
        ),
        r"***[REDACTED]***",
    ),
    # key=value or key: value assignments (generic)
    (
        re.compile(
            r"(?i)(api[_-]?key|secret|password|passwd|token|credential|auth)"
            r"s?\s*[=:]\s*['\"]?([\w/+\-\.]{6,})['\"]?",
        ),
        r"\1=***[REDACTED]***",
    ),
    # OpenAI keys: sk-...
    (
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        "sk-***[REDACTED]***",
    ),
    # GitHub personal access tokens: ghp_...  ghs_...  github_pat_...
    (
        re.compile(r"\b(ghp|ghs|gho|ghu|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
        r"\1_***[REDACTED]***",
    ),
    # AWS access key IDs: AKIA...
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "AKIA***[REDACTED]***",
    ),
    # Slack tokens: xoxb-... xoxp-... xoxa-...
    (
        re.compile(r"\bxox[bpas]-[A-Za-z0-9\-]{10,}\b"),
        "xox*-***[REDACTED]***",
    ),
    # Generic hex secrets >= 32 chars (API keys, private keys, etc.)
    (
        re.compile(r"\b[0-9a-fA-F]{32,}\b"),
        "***[REDACTED]***",
    ),
    # Generic base64-ish secrets >= 40 chars that look like tokens
    (
        re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
        "***[REDACTED]***",
    ),
]

# Environment variable name patterns that should always be redacted
_SENSITIVE_ENV_NAMES = re.compile(
    r"(?i)^(.*)(api[_-]?key|secret|password|passwd|token|credential|auth|private[_-]?key"
    r"|access[_-]?key|signing[_-]?key|encryption[_-]?key|webhook[_-]?secret"
    r"|database[_-]?url|db[_-]?pass)(.*)$"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def redact_secrets(text: str) -> str:
    """Replace known secret patterns in *text* with ``[REDACTED]``.

    This is a best-effort heuristic; it will not catch every possible
    secret format, but covers the most common cases.
    """
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of *env* with sensitive values replaced by ``[REDACTED]``.

    Any variable whose name matches ``_SENSITIVE_ENV_NAMES`` has its value
    replaced. Variable names are preserved so the agent knows which vars
    exist while protecting the actual credentials.
    """
    redacted: dict[str, str] = {}
    for key, value in env.items():
        if _SENSITIVE_ENV_NAMES.match(key):
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = redact_secrets(value)
    return redacted


__all__ = ["redact_secrets", "redact_env"]
