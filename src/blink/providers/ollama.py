"""Ollama completion provider.

Calls the Ollama HTTP API (default: http://localhost:11434) to generate shell
command suggestions.  Uses ``httpx`` (already a project dependency) — no
additional optional packages are required.

Default model: ``llama3`` (override via ProviderConfig.model).
"""

from __future__ import annotations

import json
import logging

import httpx

from blink.completions.context import CompletionContext
from blink.completions.ranker import Completion
from blink.providers.base import CompletionProvider, ProviderConfig

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "llama3"
_DEFAULT_BASE_URL = "http://localhost:11434"

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


class OllamaProvider(CompletionProvider):
    """Ollama HTTP-API-based shell completion provider."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._base_url = (config.base_url or _DEFAULT_BASE_URL).rstrip("/")

    async def complete(self, context: CompletionContext) -> list[Completion]:
        """Generate completions via the Ollama /api/chat endpoint."""
        model = self._config.model or _DEFAULT_MODEL
        user_message = _build_user_message(context)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 256},
        }

        try:
            async with httpx.AsyncClient(timeout=self._config.timeout) as client:
                response = await client.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                )
                response.raise_for_status()
                data: dict[str, object] = response.json()

            raw_content: str = ""
            message = data.get("message", {})
            if isinstance(message, dict):
                raw_content = str(message.get("content", ""))

            return self._parse_response(raw_content, model)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Ollama completion error: %s", exc)
            return []

    async def health_check(self) -> bool:
        """Return True if Ollama is running and reachable."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False

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
            logger.debug("Ollama returned non-JSON content: %r", content)
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
                    metadata={"provider": "ollama", "model": model},
                )
            )
        return results


__all__ = ["OllamaProvider"]
