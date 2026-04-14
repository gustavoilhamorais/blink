"""Tests for Blink kitten modules."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

KITTENS_DIR = Path(__file__).parent.parent / "kittens"


def _import_kitten(name: str):
    """Import a kitten module from the kittens/ directory."""
    kitten_path = KITTENS_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"kittens.{name}",
        str(kitten_path / "__init__.py"),
    )
    assert spec is not None, f"Cannot find kitten: {name}"
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Module loading tests
# ---------------------------------------------------------------------------


class TestKittenModuleLoading:
    """Verify that all kitten modules can be imported without error."""

    @pytest.mark.parametrize(
        "kitten_name",
        ["agent_overlay", "onboarding", "session_picker", "artifact_viewer"],
    )
    def test_kitten_loads(self, kitten_name: str) -> None:
        module = _import_kitten(kitten_name)
        assert module is not None

    @pytest.mark.parametrize(
        "kitten_name",
        ["agent_overlay", "onboarding", "session_picker", "artifact_viewer"],
    )
    def test_kitten_has_main(self, kitten_name: str) -> None:
        module = _import_kitten(kitten_name)
        assert hasattr(module, "main"), f"{kitten_name} must define main()"
        assert callable(module.main)

    @pytest.mark.parametrize(
        "kitten_name",
        ["agent_overlay", "onboarding", "session_picker", "artifact_viewer"],
    )
    def test_kitten_has_handle_result(self, kitten_name: str) -> None:
        module = _import_kitten(kitten_name)
        assert hasattr(module, "handle_result"), f"{kitten_name} must define handle_result()"
        assert callable(module.handle_result)

    @pytest.mark.parametrize(
        "kitten_name",
        ["agent_overlay", "onboarding", "session_picker", "artifact_viewer"],
    )
    def test_kitten_has_all(self, kitten_name: str) -> None:
        module = _import_kitten(kitten_name)
        assert hasattr(module, "__all__")
        all_names: list = module.__all__
        assert "main" in all_names
        assert "handle_result" in all_names


# ---------------------------------------------------------------------------
# agent_overlay tests
# ---------------------------------------------------------------------------


class TestAgentOverlayKitten:
    """Tests for the agent_overlay kitten."""

    def setup_method(self) -> None:
        self.mod = _import_kitten("agent_overlay")

    def test_parse_args_defaults(self) -> None:
        args = ["kittens/agent_overlay"]
        mode, run_id, block_id, action, title = self.mod._parse_args(args)
        assert mode == "approve"
        assert run_id is None
        assert block_id is None
        assert action is None
        assert title is None

    def test_parse_args_mode_long(self) -> None:
        args = ["prog", "--mode", "inspect"]
        mode, _, _, _, _ = self.mod._parse_args(args)
        assert mode == "inspect"

    def test_parse_args_mode_equals(self) -> None:
        args = ["prog", "--mode=status"]
        mode, _, _, _, _ = self.mod._parse_args(args)
        assert mode == "status"

    def test_parse_args_run_id(self) -> None:
        args = ["prog", "--run-id", "abc-123", "--mode", "approve"]
        mode, run_id, _, _, _ = self.mod._parse_args(args)
        assert run_id == "abc-123"
        assert mode == "approve"

    def test_parse_args_block_id(self) -> None:
        args = ["prog", "--block-id", "blk-456", "--mode", "inspect"]
        _, _, block_id, _, _ = self.mod._parse_args(args)
        assert block_id == "blk-456"

    def test_parse_args_action(self) -> None:
        args = ["prog", "--action=rm -rf /tmp/test"]
        _, _, _, action, _ = self.mod._parse_args(args)
        assert action == "rm -rf /tmp/test"

    def test_handle_result_approved(self) -> None:
        """handle_result should call _notify_daemon_approval for approve mode."""
        with patch.object(self.mod, "_notify_daemon_approval") as mock_notify:
            self.mod.handle_result(
                args=["prog", "--mode", "approve", "--run-id", "run-001"],
                answer="approved",
                target_window_id=1,
                boss=MagicMock(),
            )
            mock_notify.assert_called_once_with("run-001", True)

    def test_handle_result_denied(self) -> None:
        with patch.object(self.mod, "_notify_daemon_approval") as mock_notify:
            self.mod.handle_result(
                args=["prog", "--mode", "approve", "--run-id", "run-002"],
                answer="denied",
                target_window_id=1,
                boss=MagicMock(),
            )
            mock_notify.assert_called_once_with("run-002", False)

    def test_handle_result_inspect_mode_no_notify(self) -> None:
        """Inspect mode should not call notify."""
        with patch.object(self.mod, "_notify_daemon_approval") as mock_notify:
            self.mod.handle_result(
                args=["prog", "--mode", "inspect", "--block-id", "blk-001"],
                answer="",
                target_window_id=1,
                boss=MagicMock(),
            )
            mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# onboarding tests
# ---------------------------------------------------------------------------


class TestOnboardingKitten:
    """Tests for the onboarding kitten."""

    def setup_method(self) -> None:
        self.mod = _import_kitten("onboarding")

    def test_detect_bash_from_env(self) -> None:
        with patch.dict("os.environ", {"SHELL": "/bin/bash"}):
            shell = self.mod._detect_shell()
        assert shell == "bash"

    def test_detect_zsh_from_env(self) -> None:
        with patch.dict("os.environ", {"SHELL": "/usr/bin/zsh"}):
            shell = self.mod._detect_shell()
        assert shell == "zsh"

    def test_detect_fish_from_env(self) -> None:
        with patch.dict("os.environ", {"SHELL": "/usr/local/bin/fish"}):
            shell = self.mod._detect_shell()
        assert shell == "fish"

    def test_detect_unknown_shell_returns_name(self) -> None:
        with patch.dict("os.environ", {"SHELL": "/usr/bin/dash"}):
            shell = self.mod._detect_shell()
        assert shell == "dash"

    def test_main_skips_if_completed(self, tmp_path: Path) -> None:
        """main() should return 'skipped' if onboarding is already complete."""
        flag = tmp_path / "onboarding_complete"
        flag.touch()
        with patch.object(self.mod, "_ONBOARDING_FLAG", flag):
            with patch.object(self.mod, "_print_info") as mock_info:
                result = self.mod.main([])
        assert result == "skipped"
        mock_info.assert_called_once()

    def test_handle_result_no_action_for_empty_answer(self) -> None:
        """handle_result should not crash on 'skipped' answer."""
        # Should not raise
        self.mod.handle_result(
            args=[],
            answer="skipped",
            target_window_id=0,
            boss=MagicMock(),
        )

    def test_handle_result_no_action_for_missing_key(self) -> None:
        """handle_result should not crash if no api_key in data."""
        import json

        answer = json.dumps({"provider": {"name": "openai", "api_key": ""}})
        with patch.object(self.mod, "_persist_provider") as mock_persist:
            self.mod.handle_result(
                args=[],
                answer=answer,
                target_window_id=0,
                boss=MagicMock(),
            )
            mock_persist.assert_not_called()


# ---------------------------------------------------------------------------
# session_picker tests
# ---------------------------------------------------------------------------


class TestSessionPickerKitten:
    """Tests for the session_picker kitten."""

    def setup_method(self) -> None:
        self.mod = _import_kitten("session_picker")

    def test_main_no_sessions(self) -> None:
        """main() should return '' if no sessions available."""
        with patch.object(self.mod, "_fetch_sessions", return_value=[]):
            with patch.object(self.mod, "_print_info"):
                with patch.object(self.mod, "_wait_for_key"):
                    result = self.mod.main([])
        assert result == ""

    def test_handle_result_no_answer(self) -> None:
        """handle_result with empty answer should be a no-op."""
        with patch.object(self.mod, "_get_window_id_for_session") as mock_get:
            self.mod.handle_result(
                args=[],
                answer="",
                target_window_id=0,
                boss=MagicMock(),
            )
            mock_get.assert_not_called()

    def test_handle_result_with_answer_focuses_window(self) -> None:
        """handle_result should call focus when window_id is found."""
        boss = MagicMock()
        with patch.object(self.mod, "_get_window_id_for_session", return_value=3):
            with patch.object(self.mod, "_focus_window_via_rc") as mock_focus:
                # If boss doesn't have call_remote_control, falls back to rc
                del boss.call_remote_control
                self.mod.handle_result(
                    args=[],
                    answer="session-abc",
                    target_window_id=0,
                    boss=boss,
                )
            mock_focus.assert_called_once_with(3)

    def test_handle_result_window_not_found(self) -> None:
        """handle_result should be a no-op if window_id is None."""
        with patch.object(self.mod, "_get_window_id_for_session", return_value=None):
            with patch.object(self.mod, "_focus_window_via_rc") as mock_focus:
                self.mod.handle_result(
                    args=[],
                    answer="session-xyz",
                    target_window_id=0,
                    boss=MagicMock(),
                )
            mock_focus.assert_not_called()


# ---------------------------------------------------------------------------
# artifact_viewer tests
# ---------------------------------------------------------------------------


class TestArtifactViewerKitten:
    """Tests for the artifact_viewer kitten."""

    def setup_method(self) -> None:
        self.mod = _import_kitten("artifact_viewer")

    def test_detect_format_json_object(self) -> None:
        assert self.mod._detect_format('{"key": "value"}') == "json"

    def test_detect_format_json_array(self) -> None:
        assert self.mod._detect_format("[1, 2, 3]") == "json"

    def test_detect_format_code(self) -> None:
        assert self.mod._detect_format("```python\nprint('hi')\n```") == "code"

    def test_detect_format_table(self) -> None:
        table_text = "| Col1 | Col2 |\n| ---- | ---- |\n| A    | B    |"
        assert self.mod._detect_format(table_text) == "table"

    def test_detect_format_text_fallback(self) -> None:
        assert self.mod._detect_format("plain text output") == "text"

    def test_detect_format_empty(self) -> None:
        assert self.mod._detect_format("") == "text"

    def test_format_json_valid(self) -> None:
        lines = self.mod._format_json('{"a": 1, "b": "hello"}')
        assert isinstance(lines, list)
        assert len(lines) > 0
        # Should not contain raw JSON braces after formatting
        full = "\n".join(lines)
        assert "a" in full

    def test_format_json_invalid(self) -> None:
        lines = self.mod._format_json("not json at all {{{")
        assert any("invalid JSON" in line for line in lines)

    def test_format_table(self) -> None:
        table = "| Name | Age |\n| ---- | --- |\n| Alice | 30 |"
        lines = self.mod._format_table(table)
        assert isinstance(lines, list)
        assert any("Alice" in line for line in lines)

    def test_format_code(self) -> None:
        code = "```python\nx = 1\n```"
        lines = self.mod._format_code(code)
        assert isinstance(lines, list)
        assert any("x = 1" in line for line in lines)

    def test_parse_args_block_id(self) -> None:
        opts = self.mod._parse_args(["prog", "--block-id", "blk-123"])
        assert opts["block_id"] == "blk-123"

    def test_parse_args_run_id(self) -> None:
        opts = self.mod._parse_args(["prog", "--run-id=run-456"])
        assert opts["run_id"] == "run-456"

    def test_parse_args_file_positional(self) -> None:
        opts = self.mod._parse_args(["prog", "/tmp/output.json"])
        assert opts["file"] == "/tmp/output.json"

    def test_parse_args_format(self) -> None:
        opts = self.mod._parse_args(["prog", "--format", "json"])
        assert opts["format"] == "json"

    def test_main_no_input_shows_error(self) -> None:
        """main() with no input args should show an error and return ''."""
        with patch.object(self.mod, "_show_error") as mock_err:
            result = self.mod.main(["prog"])
        assert result == ""
        mock_err.assert_called_once()

    def test_handle_result_is_noop(self) -> None:
        """handle_result for a viewer kitten should be a no-op."""
        # Should not raise
        self.mod.handle_result(
            args=[],
            answer="",
            target_window_id=0,
            boss=MagicMock(),
        )
