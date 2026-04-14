"""AI provider abstraction layer.

Defines the base Provider interface and concrete implementations for:
- OpenAI (GPT-4o, GPT-4o-mini, …)
- Anthropic (Claude 3.x Haiku / Sonnet / Opus)

Additional providers (Ollama, Google Gemini, etc.) can be added by
implementing the Provider protocol.
"""
