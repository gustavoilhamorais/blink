"""AI provider abstraction layer.

Defines the base Provider interface and concrete implementations for:
- OpenAI (GPT-4o, GPT-4o-mini, …)
- Anthropic (Claude Haiku / Sonnet / Opus)
- Ollama (local models via HTTP API)

Additional providers can be added by implementing the CompletionProvider ABC.
"""

from blink.providers.anthropic import AnthropicProvider
from blink.providers.base import CompletionProvider, ProviderConfig
from blink.providers.ollama import OllamaProvider
from blink.providers.openai import OpenAIProvider

__all__ = [
    "AnthropicProvider",
    "CompletionProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "ProviderConfig",
]
