"""Abstract base for AI completion providers.

All providers must implement:
- ``complete(context)`` — return a list of Completion candidates.
- ``health_check()`` — return True when the provider is reachable.

The ``ProviderConfig`` Pydantic model is used to pass configuration (API key,
model name, endpoint URL, timeout) to provider constructors.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

from blink.completions.context import CompletionContext
from blink.completions.ranker import Completion


class ProviderConfig(BaseModel):
    """Configuration for an AI completion provider."""

    name: str
    """Human-readable provider name (e.g. 'openai', 'anthropic', 'ollama')."""

    api_key: str | None = None
    """API key or token. May be None for local providers like Ollama."""

    model: str | None = None
    """Model identifier to use (provider-specific default if None)."""

    base_url: str | None = None
    """Override the default API base URL (useful for proxies / local models)."""

    timeout: float = 5.0
    """Per-request timeout in seconds."""


class CompletionProvider(ABC):
    """Abstract base class for shell completion providers."""

    @abstractmethod
    async def complete(self, context: CompletionContext) -> list[Completion]:
        """Generate completions from the given context.

        Implementations MUST:
        - Return an empty list (not raise) when the API is unavailable or when
          no API key is configured.
        - Include ``source='llm'`` in each returned :class:`Completion`.
        - Respect ``ProviderConfig.timeout`` for HTTP calls.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the provider is currently reachable and configured."""


__all__ = ["CompletionProvider", "ProviderConfig"]
