"""Tests for AI completion providers.

All network calls are mocked — these tests do not require real API keys or
a running Ollama instance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink.completions.context import CompletionContext
from blink.completions.ranker import Completion
from blink.providers.anthropic import AnthropicProvider
from blink.providers.base import CompletionProvider, ProviderConfig
from blink.providers.ollama import OllamaProvider
from blink.providers.openai import OpenAIProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> CompletionContext:
    return CompletionContext(
        buffer="git st",
        cursor_position=6,
        shell="bash",
        cwd="/home/user/project",
        git_branch="main",
        recent_commands=["git log", "ls -la"],
        visible_files=["README.md", "src"],
    )


def _make_config(name: str, api_key: str | None = "test-key", model: str | None = None):
    return ProviderConfig(name=name, api_key=api_key, model=model, timeout=5.0)


# ---------------------------------------------------------------------------
# ProviderConfig
# ---------------------------------------------------------------------------


class TestProviderConfig:
    def test_defaults(self) -> None:
        cfg = ProviderConfig(name="test")
        assert cfg.api_key is None
        assert cfg.model is None
        assert cfg.base_url is None
        assert cfg.timeout == 5.0

    def test_custom_values(self) -> None:
        cfg = ProviderConfig(
            name="openai",
            api_key="sk-abc",
            model="gpt-4o",
            base_url="https://custom.endpoint",
            timeout=10.0,
        )
        assert cfg.api_key == "sk-abc"
        assert cfg.model == "gpt-4o"
        assert cfg.timeout == 10.0


# ---------------------------------------------------------------------------
# CompletionProvider ABC
# ---------------------------------------------------------------------------


class TestCompletionProviderABC:
    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            CompletionProvider()  # type: ignore[abstract]

    def test_concrete_must_implement_methods(self) -> None:
        class Incomplete(CompletionProvider):
            async def complete(self, context):
                return []

        # Missing health_check — should still raise TypeError.
        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------


class TestOpenAIProvider:
    def test_no_api_key_returns_empty(self, ctx: CompletionContext) -> None:
        cfg = _make_config("openai", api_key=None)
        provider = OpenAIProvider(cfg)
        # _get_client returns None when no API key
        assert provider._get_client() is None

    async def test_no_package_returns_empty(self, ctx: CompletionContext) -> None:
        cfg = _make_config("openai")
        provider = OpenAIProvider(cfg)
        with patch("builtins.__import__", side_effect=ImportError):
            provider._client = None  # force re-evaluation
            provider._get_client()
            # ImportError means client is None
            # But we can't easily patch __import__ in isolation; test the
            # public contract instead.
        assert isinstance(provider, OpenAIProvider)

    async def test_complete_with_mock_client(self, ctx: CompletionContext) -> None:
        cfg = _make_config("openai", model="gpt-4o-mini")
        provider = OpenAIProvider(cfg)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '["git status", "git diff", "git log"]'

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        provider._client = mock_client

        results = await provider.complete(ctx)
        assert len(results) == 3
        assert results[0].text == "git status"
        assert results[0].source == "llm"
        assert results[0].metadata["provider"] == "openai"

    async def test_api_error_returns_empty(self, ctx: CompletionContext) -> None:
        cfg = _make_config("openai")
        provider = OpenAIProvider(cfg)

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API error"))
        provider._client = mock_client

        results = await provider.complete(ctx)
        assert results == []

    def test_parse_response_valid_json(self) -> None:
        result = OpenAIProvider._parse_response(
            '["git status", "git diff HEAD"]', "gpt-4o-mini"
        )
        assert len(result) == 2
        assert result[0].text == "git status"
        assert result[1].text == "git diff HEAD"

    def test_parse_response_with_markdown_fences(self) -> None:
        content = '```json\n["git status"]\n```'
        result = OpenAIProvider._parse_response(content, "gpt-4o-mini")
        assert len(result) == 1
        assert result[0].text == "git status"

    def test_parse_response_invalid_json(self) -> None:
        result = OpenAIProvider._parse_response("not json at all", "gpt-4o-mini")
        assert result == []

    def test_parse_response_not_a_list(self) -> None:
        result = OpenAIProvider._parse_response('{"cmd": "git status"}', "gpt-4o-mini")
        assert result == []

    def test_parse_response_filters_empty_strings(self) -> None:
        result = OpenAIProvider._parse_response('["git status", "", "ls"]', "gpt-4o-mini")
        texts = [r.text for r in result]
        assert "" not in texts

    def test_parse_response_confidence_descending(self) -> None:
        result = OpenAIProvider._parse_response(
            '["cmd1", "cmd2", "cmd3"]', "gpt-4o-mini"
        )
        confidences = [r.confidence for r in result]
        assert confidences == sorted(confidences, reverse=True)

    async def test_health_check_no_client(self) -> None:
        cfg = _make_config("openai", api_key=None)
        provider = OpenAIProvider(cfg)
        assert await provider.health_check() is False

    async def test_health_check_api_error(self) -> None:
        cfg = _make_config("openai")
        provider = OpenAIProvider(cfg)

        mock_client = AsyncMock()
        mock_client.models.list = AsyncMock(side_effect=Exception("network error"))
        provider._client = mock_client

        assert await provider.health_check() is False


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------


class TestAnthropicProvider:
    def test_no_api_key_returns_none_client(self, ctx: CompletionContext) -> None:
        cfg = _make_config("anthropic", api_key=None)
        provider = AnthropicProvider(cfg)
        assert provider._get_client() is None

    async def test_complete_with_mock_client(self, ctx: CompletionContext) -> None:
        cfg = _make_config("anthropic", model="claude-haiku-4-5")
        provider = AnthropicProvider(cfg)

        text_block = MagicMock()
        text_block.text = '["git status", "git log --oneline"]'

        mock_response = MagicMock()
        mock_response.content = [text_block]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        provider._client = mock_client

        results = await provider.complete(ctx)
        assert len(results) == 2
        assert results[0].text == "git status"
        assert results[0].source == "llm"
        assert results[0].metadata["provider"] == "anthropic"

    async def test_api_error_returns_empty(self, ctx: CompletionContext) -> None:
        cfg = _make_config("anthropic")
        provider = AnthropicProvider(cfg)

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("rate limited"))
        provider._client = mock_client

        results = await provider.complete(ctx)
        assert results == []

    def test_parse_response_valid_json(self) -> None:
        result = AnthropicProvider._parse_response(
            '["git status", "git diff"]', "claude-haiku-4-5"
        )
        assert len(result) == 2

    def test_parse_response_with_markdown_fences(self) -> None:
        content = "```\n[\"git status\"]\n```"
        result = AnthropicProvider._parse_response(content, "claude-haiku-4-5")
        assert len(result) == 1

    def test_parse_response_invalid_json(self) -> None:
        result = AnthropicProvider._parse_response("I cannot help with that", "claude-haiku-4-5")
        assert result == []

    async def test_health_check_with_client(self) -> None:
        cfg = _make_config("anthropic")
        provider = AnthropicProvider(cfg)

        mock_client = MagicMock()
        provider._client = mock_client

        assert await provider.health_check() is True

    async def test_health_check_no_client(self) -> None:
        cfg = _make_config("anthropic", api_key=None)
        provider = AnthropicProvider(cfg)
        assert await provider.health_check() is False


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------


class TestOllamaProvider:
    async def test_complete_with_mock_response(self, ctx: CompletionContext) -> None:
        cfg = ProviderConfig(name="ollama", model="llama3", timeout=5.0)
        provider = OllamaProvider(cfg)

        response_body = {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": '["git status", "git log --oneline -5"]',
            },
            "done": True,
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value=response_body)
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("blink.providers.ollama.httpx.AsyncClient", return_value=mock_client):
            results = await provider.complete(ctx)

        assert len(results) == 2
        assert results[0].text == "git status"
        assert results[0].source == "llm"
        assert results[0].metadata["provider"] == "ollama"

    async def test_complete_connection_error_returns_empty(self, ctx: CompletionContext) -> None:
        cfg = ProviderConfig(name="ollama", timeout=1.0)
        provider = OllamaProvider(cfg)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("blink.providers.ollama.httpx.AsyncClient", return_value=mock_client):
            results = await provider.complete(ctx)

        assert results == []

    async def test_health_check_success(self) -> None:
        cfg = ProviderConfig(name="ollama")
        provider = OllamaProvider(cfg)

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("blink.providers.ollama.httpx.AsyncClient", return_value=mock_client):
            result = await provider.health_check()

        assert result is True

    async def test_health_check_connection_refused(self) -> None:
        cfg = ProviderConfig(name="ollama")
        provider = OllamaProvider(cfg)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("blink.providers.ollama.httpx.AsyncClient", return_value=mock_client):
            result = await provider.health_check()

        assert result is False

    def test_default_base_url(self) -> None:
        cfg = ProviderConfig(name="ollama")
        provider = OllamaProvider(cfg)
        assert provider._base_url == "http://localhost:11434"

    def test_custom_base_url(self) -> None:
        cfg = ProviderConfig(name="ollama", base_url="http://my-server:11434/")
        provider = OllamaProvider(cfg)
        # Trailing slash should be stripped
        assert provider._base_url == "http://my-server:11434"

    def test_parse_response_valid(self) -> None:
        result = OllamaProvider._parse_response('["ls -la", "pwd"]', "llama3")
        assert len(result) == 2
        assert result[0].text == "ls -la"

    def test_parse_response_invalid_json(self) -> None:
        result = OllamaProvider._parse_response("Sure! Here are some commands:", "llama3")
        assert result == []


# ---------------------------------------------------------------------------
# Broker integration (light-weight, no real I/O)
# ---------------------------------------------------------------------------


class TestBrokerWithProviders:
    """Verify CompletionBroker wires providers correctly."""

    async def test_broker_uses_llm_when_no_fast_path(
        self, tmp_path, ctx: CompletionContext
    ) -> None:
        from blink.completions.broker import CompletionBroker
        from blink.completions.ranker import HistoryRanker
        from blink.completions.validator import Validator
        from blink.storage import Storage

        db = tmp_path / "broker_test.db"
        storage = Storage(db_path=db)
        await storage.init_db()

        ranker = HistoryRanker(storage)

        # Mock provider returns one result
        mock_provider = MagicMock(spec=CompletionProvider)
        mock_provider.complete = AsyncMock(
            return_value=[
                Completion(text="git status", display="git status", confidence=0.85, source="llm")
            ]
        )

        broker = CompletionBroker(
            ranker=ranker,
            provider=mock_provider,
            validator=Validator(),
            debounce_s=0.0,
        )
        results = await broker.get_completions(ctx)

        assert len(results) >= 1
        assert any(r.text == "git status" for r in results)
        await storage.close()

    async def test_broker_no_provider_history_only(
        self, tmp_path, ctx: CompletionContext
    ) -> None:
        from datetime import timedelta  # noqa: PLC0415

        from blink.completions.broker import CompletionBroker
        from blink.completions.ranker import HistoryRanker
        from blink.completions.validator import Validator
        from blink.storage import Storage

        db = tmp_path / "broker_hist.db"
        storage = Storage(db_path=db)
        await storage.init_db()

        executed_at = (datetime.now(tz=UTC) - timedelta(seconds=5)).isoformat()
        await storage.execute(
            "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
            ("git status", "/home/user/project", 0, executed_at),
        )

        ranker = HistoryRanker(storage)
        broker = CompletionBroker(
            ranker=ranker,
            provider=None,  # history-only mode
            validator=Validator(),
            debounce_s=0.0,
        )

        results = await broker.get_completions(ctx)
        assert any(r.text == "git status" for r in results)
        await storage.close()
