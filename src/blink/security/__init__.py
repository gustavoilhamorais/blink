"""Security and credential management.

Handles:
- Secure storage of AI provider API keys (via OS keychain or encrypted file)
- Sandboxing utilities for restricting agent tool execution
- Permission model for MCP tool access
- Rate limiting for MCP tool calls
- Input sanitization
- Security audit utilities
"""

from blink.security.audit import AuditFinding, audit_report, audit_tools
from blink.security.capabilities import Capability, CapabilityChecker, SecurityPolicy
from blink.security.keyring import CredentialStore
from blink.security.ratelimit import RateLimiter
from blink.security.redaction import redact_env, redact_secrets
from blink.security.sanitize import (
    is_safe_path,
    sanitize_argument,
    sanitize_command,
    sanitize_display_text,
    sanitize_path,
    strip_ansi,
)

__all__ = [
    "AuditFinding",
    "audit_report",
    "audit_tools",
    "Capability",
    "CapabilityChecker",
    "CredentialStore",
    "is_safe_path",
    "RateLimiter",
    "redact_env",
    "redact_secrets",
    "sanitize_argument",
    "sanitize_command",
    "sanitize_display_text",
    "sanitize_path",
    "SecurityPolicy",
    "strip_ansi",
]
