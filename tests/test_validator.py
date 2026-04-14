"""Tests for the completion Validator."""

from __future__ import annotations

from blink.completions.context import CompletionContext
from blink.completions.ranker import Completion
from blink.completions.validator import Validator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_completion(text: str, source: str = "history", confidence: float = 0.8) -> Completion:
    return Completion(text=text, display=text, confidence=confidence, source=source)


# ---------------------------------------------------------------------------
# Syntax validation
# ---------------------------------------------------------------------------


class TestSyntaxValidation:
    async def test_valid_command_passes(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("git status")
        result = await v.validate(comp, ctx)
        assert result is not None
        assert result.text == "git status"

    async def test_empty_string_is_invalid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("")
        result = await v.validate(comp, ctx)
        assert result is None

    async def test_whitespace_only_is_invalid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("   ")
        result = await v.validate(comp, ctx)
        assert result is None

    async def test_unbalanced_single_quote_is_invalid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("echo 'hello")
        result = await v.validate(comp, ctx)
        assert result is None

    async def test_unbalanced_double_quote_is_invalid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion('echo "hello')
        result = await v.validate(comp, ctx)
        assert result is None

    async def test_complex_valid_command(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("find . -name '*.py' -exec grep -l 'import' {} \\;")
        result = await v.validate(comp, ctx)
        assert result is not None

    async def test_pipe_command_valid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("ps aux | grep python")
        result = await v.validate(comp, ctx)
        assert result is not None

    async def test_redirect_valid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("echo hello > /tmp/test.txt")
        result = await v.validate(comp, ctx)
        assert result is not None

    async def test_leading_trailing_whitespace_stripped(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("  git status  ")
        result = await v.validate(comp, ctx)
        assert result is not None
        assert result.text == "git status"

    async def test_balanced_quotes_valid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        comp = make_completion("git commit -m 'fix bug'")
        result = await v.validate(comp, ctx)
        assert result is not None


# ---------------------------------------------------------------------------
# validate_all
# ---------------------------------------------------------------------------


class TestValidateAll:
    async def test_filters_invalid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        completions = [
            make_completion("git status"),
            make_completion("echo 'bad"),
            make_completion("ls -la"),
            make_completion(""),
        ]
        results = await v.validate_all(completions, ctx)
        texts = [r.text for r in results]
        assert "git status" in texts
        assert "ls -la" in texts
        assert "echo 'bad" not in texts
        assert "" not in texts

    async def test_preserves_order(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        completions = [
            make_completion("ls"),
            make_completion("pwd"),
            make_completion("whoami"),
        ]
        results = await v.validate_all(completions, ctx)
        assert [r.text for r in results] == ["ls", "pwd", "whoami"]

    async def test_empty_input(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        results = await v.validate_all([], ctx)
        assert results == []

    async def test_all_invalid(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        completions = [
            make_completion("echo 'unterminated"),
            make_completion('cmd "broken'),
        ]
        results = await v.validate_all(completions, ctx)
        assert results == []

    async def test_default_context(self) -> None:
        """validate_all should work when context is not passed."""
        v = Validator()
        completions = [make_completion("git log")]
        results = await v.validate_all(completions)
        assert len(results) == 1

    async def test_all_valid_returned(self) -> None:
        v = Validator()
        ctx = CompletionContext()
        completions = [make_completion(f"cmd{i}") for i in range(5)]
        results = await v.validate_all(completions, ctx)
        assert len(results) == 5


# ---------------------------------------------------------------------------
# Path check mode
# ---------------------------------------------------------------------------


class TestPathCheckMode:
    async def test_known_command_passes(self) -> None:
        v = Validator(check_paths=True)
        ctx = CompletionContext()
        # "ls" is always on PATH
        comp = make_completion("ls -la")
        result = await v.validate(comp, ctx)
        assert result is not None

    async def test_shell_builtins_pass(self) -> None:
        v = Validator(check_paths=True)
        ctx = CompletionContext()
        for builtin in ["cd /tmp", "echo hello", "export FOO=bar"]:
            comp = make_completion(builtin)
            result = await v.validate(comp, ctx)
            assert result is not None, f"builtin {builtin!r} should pass"

    async def test_nonexistent_path_command_fails(self) -> None:
        v = Validator(check_paths=True)
        ctx = CompletionContext()
        comp = make_completion("/nonexistent/path/to/binary --flag")
        result = await v.validate(comp, ctx)
        assert result is None

    async def test_unknown_bare_word_without_check_passes(self) -> None:
        v = Validator(check_paths=False)
        ctx = CompletionContext()
        comp = make_completion("totally_made_up_command --arg")
        result = await v.validate(comp, ctx)
        assert result is not None
