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

# ---------------------------------------------------------------------------
# Additional patterns for enhanced detection
# ---------------------------------------------------------------------------

_ADDITIONAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Private key labels (file content markers)
    (
        re.compile(r"-----BEGIN\s+(?:\w+\s+)*PRIVATE KEY-----[\s\S]*?-----END\s+(?:\w+\s+)*PRIVATE KEY-----"),
        "-----BEGIN PRIVATE KEY----- ***[REDACTED]*** -----END PRIVATE KEY-----",
    ),
    # Client secret assignments
    (
        re.compile(r"(?i)client[_-]?secret\s*[=:]\s*['\"]?([\w/+\-\.]{6,})['\"]?"),
        "client_secret=***[REDACTED]***",
    ),
    # Database URL with embedded credentials: postgres://user:pass@host or mysql://...
    (
        re.compile(r"(?i)(postgres|postgresql|mysql|mariadb|mongodb)://[^:@\s]+:[^@\s]+@"),
        r"\1://***[REDACTED]***@",
    ),
    # mongodb+srv:// DSN
    (
        re.compile(r"mongodb\+srv://[^:@\s]+:[^@\s]+@"),
        "mongodb+srv://***[REDACTED]***@",
    ),
    # Generic private_key assignments
    (
        re.compile(r"(?i)private[_-]?key\s*[=:]\s*['\"]?([\w/+\-\.]{6,})['\"]?"),
        "private_key=***[REDACTED]***",
    ),
    # JWT tokens: three base64url segments separated by dots
    (
        re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "ey***[REDACTED JWT]***",
    ),
    # Stripe secret keys: sk_live_... or sk_test_...
    (
        re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b"),
        "sk_***[REDACTED]***",
    ),
    # Twilio auth tokens
    (
        re.compile(r"\b[0-9a-f]{32}\b"),
        "***[REDACTED]***",
    ),
]

# Merge additional patterns into the primary list
_SECRET_PATTERNS.extend(_ADDITIONAL_PATTERNS)

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
