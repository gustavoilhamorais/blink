"""Anthropic (Claude) completion provider.

Uses the messages API to generate shell command suggestions.  The
``anthropic`` package is an optional dependency — if it is not installed the
provider will return empty results rather than crashing.

Default model: ``claude-haiku-4-5`` (fast / low-latency; override via ProviderConfig).
"""

from __future__ import annotations

import json
import logging

from blink.completions.context import CompletionContext
from blink.completions.ranker import Completion
from blink.providers.base import CompletionProvider, ProviderConfig

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5"

_SYSTEM_PROMPT = """\
You are a shell completion assistant. Given context about the user's terminal \
session, suggest up to 3 complete shell commands that are most likely to be \
useful. Return ONLY a JSON array of strings — no markdown, no explanations. \
Each string must be a complete, valid shell command. Example output:
["git status", "git diff HEAD", "git log --oneline -10"]
"""


def _build_user_message(context: CompletionContext) -> str:
    parts: list[str] = []
    parts.append(f"Shell: {context.shell}")
    parts.append(f"Current directory: {context.cwd}")
    if context.git_branch:
        parts.append(f"Git branch: {context.git_branch}")
    if context.buffer:
        parts.append(f"Buffer (typed so far): {context.buffer!r}")
    if context.recent_commands:
        recent = context.recent_commands[:10]
        parts.append("Recent commands:\n" + "\n".join(f"  {c}" for c in recent))
    if context.visible_files:
        files = context.visible_files[:20]
        parts.append("Files in cwd: " + ", ".join(files))
    return "\n".join(parts)


class AnthropicProvider(CompletionProvider):
    """Anthropic messages-API-based shell completion provider."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._client: object | None = None

    def _get_client(self) -> object | None:
        """Return (and lazily create) the AsyncAnthropic client, or None."""
        if self._client is not None:
            return self._client
        if not self._config.api_key:
            return None
        try:
            import anthropic  # type: ignore[import]

            kwargs: dict[str, object] = {
                "api_key": self._config.api_key,
                "timeout": self._config.timeout,
            }
            if self._config.base_url:
                kwargs["base_url"] = self._config.base_url
            self._client = anthropic.AsyncAnthropic(**kwargs)
            return self._client
        except ImportError:
            logger.debug("anthropic package not installed; AnthropicProvider unavailable")
            return None

    async def complete(self, context: CompletionContext) -> list[Completion]:
        """Generate completions via Anthropic messages API."""
        client = self._get_client()
        if client is None:
            return []

        model = self._config.model or _DEFAULT_MODEL
        user_message = _build_user_message(context)

        try:
            message = await client.messages.create(  # type: ignore[attr-defined]
                model=model,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw_content: str = ""
            for block in message.content:
                if hasattr(block, "text"):
                    raw_content = block.text
                    break
            return self._parse_response(raw_content, model)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Anthropic completion error: %s", exc)
            return []

    async def health_check(self) -> bool:
        """Return True if the client can be initialised (package + API key present)."""
        client = self._get_client()
        if client is None:
            return False
        # The Anthropic SDK does not offer a free "ping" endpoint; we verify
        # that the client object was created successfully (API key present and
        # package installed) as a lightweight proxy for availability.
        return True

    @staticmethod
    def _parse_response(content: str, model: str) -> list[Completion]:
        """Parse the JSON array returned by the model into Completions."""
        content = content.strip()
        for fence in ("```json", "```"):
            if content.startswith(fence):
                content = content[len(fence):]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            commands: list[str] = json.loads(content)
        except json.JSONDecodeError:
            logger.debug("Anthropic returned non-JSON content: %r", content)
            return []

        if not isinstance(commands, list):
            return []

        results: list[Completion] = []
        for i, cmd in enumerate(commands[:5]):
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            confidence = max(0.5, 0.9 - i * 0.1)
            results.append(
                Completion(
                    text=cmd.strip(),
                    display=cmd.strip(),
                    confidence=confidence,
                    source="llm",
                    metadata={"provider": "anthropic", "model": model},
                )
            )
        return results


__all__ = ["AnthropicProvider"]
