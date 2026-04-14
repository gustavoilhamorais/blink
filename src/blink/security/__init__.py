"""Security and credential management.

Handles:
- Secure storage of AI provider API keys (via OS keychain or encrypted file)
- Sandboxing utilities for restricting agent tool execution
- Permission model for MCP tool access
"""

from blink.security.capabilities import Capability, CapabilityChecker, SecurityPolicy
from blink.security.redaction import redact_env, redact_secrets

__all__ = [
    "Capability",
    "CapabilityChecker",
    "SecurityPolicy",
    "redact_env",
    "redact_secrets",
]
