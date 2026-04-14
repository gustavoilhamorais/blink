"""Tests for the complete CLI — Phase 5 additions (history, config, etc.)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from blink.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Daemon logs command
# ---------------------------------------------------------------------------


class TestDaemonLogs:
    def test_daemon_logs_help(self) -> None:
        result = runner.invoke(app, ["daemon", "logs", "--help"])
        assert result.exit_code == 0
        assert "logs" in result.output.lower()

    def test_daemon_logs_missing_log_file(self, tmp_path: Path) -> None:
        """If the log file doesn't exist, exit with 1."""
        with patch("blink.cli._BLINK_DIR", tmp_path):
            result = runner.invoke(app, ["daemon", "logs"])
        assert result.exit_code == 1

    def test_daemon_logs_shows_last_n_lines(self, tmp_path: Path) -> None:
        log_file = tmp_path / "blink.log"
        lines = [f"line {i}" for i in range(20)]
        log_file.write_text("\n".join(lines))
        with patch("blink.cli._BLINK_DIR", tmp_path):
            result = runner.invoke(app, ["daemon", "logs", "--lines", "5"])
        assert result.exit_code == 0
        # Last 5 lines of 0..19 should be lines 15, 16, 17, 18, 19
        assert "line 19" in result.output
        assert "line 15" in result.output
        # First line should NOT be shown (only last 5)
        assert "line 0" not in result.output


# ---------------------------------------------------------------------------
# Provider test command
# ---------------------------------------------------------------------------


class TestProviderTest:
    def test_provider_test_help(self) -> None:
        result = runner.invoke(app, ["provider", "test", "--help"])
        assert result.exit_code == 0

    def test_provider_test_unknown_provider(self) -> None:
        result = runner.invoke(app, ["provider", "test", "unknown_xyz"])
        assert result.exit_code != 0
        # Check output or exception message for the unknown provider
        combined = result.output + (str(result.exception) if result.exception else "")
        assert "Unknown provider" in combined or "unknown" in combined.lower()

    def test_provider_test_anthropic_no_key(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["provider", "test", "anthropic"])
        assert result.exit_code != 0

    def test_provider_test_openai_no_key(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["provider", "test", "openai"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Block commands (show / explain / retry)
# ---------------------------------------------------------------------------


class TestBlockShowCommand:
    def test_block_show_help(self) -> None:
        result = runner.invoke(app, ["block", "show", "--help"])
        assert result.exit_code == 0

    def test_block_show_daemon_not_running(self) -> None:
        """Should fail gracefully if daemon socket is absent."""
        with patch("blink.cli._SOCKET_PATH", Path("/nonexistent/blink.sock")):
            result = runner.invoke(app, ["block", "show", "blk-001"])
        assert result.exit_code == 1

    def test_block_show_text_format(self) -> None:
        mock_block = {
            "id": "blk-abc123",
            "command": "ls -la",
            "cwd": "/tmp",
            "exit_code": 0,
            "output": "total 0\ndrwxr-xr-x 1 user user 0 Apr 14",
            "started_at": "2026-04-14T10:00:00Z",
            "ended_at": "2026-04-14T10:00:01Z",
        }
        with patch("blink.cli._send_ipc", return_value={"ok": True, "data": mock_block}):
            result = runner.invoke(app, ["block", "show", "blk-abc123"])
        assert result.exit_code == 0
        assert "ls -la" in result.output
        assert "/tmp" in result.output

    def test_block_show_json_format(self) -> None:
        mock_block = {
            "id": "blk-abc123",
            "command": "echo hello",
            "cwd": "/home/user",
            "exit_code": 0,
            "output": "hello",
        }
        with patch("blink.cli._send_ipc", return_value={"ok": True, "data": mock_block}):
            result = runner.invoke(app, ["block", "show", "blk-abc123", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["command"] == "echo hello"

    def test_block_show_not_found(self) -> None:
        with patch("blink.cli._send_ipc", return_value={"ok": True, "data": None}):
            result = runner.invoke(app, ["block", "show", "blk-000"])
        assert result.exit_code == 1


class TestBlockExplainCommand:
    def test_block_explain_help(self) -> None:
        result = runner.invoke(app, ["block", "explain", "--help"])
        assert result.exit_code == 0

    def test_block_explain_no_session(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "BLINK_SESSION_ID"}
        with patch.dict("os.environ", env, clear=True):
            result = runner.invoke(app, ["block", "explain", "blk-001"])
        assert result.exit_code == 1
        assert "session" in result.output.lower()

    def test_block_explain_block_not_found(self) -> None:
        with patch.dict("os.environ", {"BLINK_SESSION_ID": "sess-test"}):
            with patch("blink.cli._send_ipc", return_value={"ok": False, "error": "not found"}):
                result = runner.invoke(app, ["block", "explain", "blk-nope"])
        assert result.exit_code == 1


class TestBlockRetryCommand:
    def test_block_retry_help(self) -> None:
        result = runner.invoke(app, ["block", "retry", "--help"])
        assert result.exit_code == 0

    def test_block_retry_not_found(self) -> None:
        with patch("blink.cli._send_ipc", return_value={"ok": True, "data": None}):
            result = runner.invoke(app, ["block", "retry", "blk-nope"])
        assert result.exit_code == 1

    def test_block_retry_no_command(self) -> None:
        with patch("blink.cli._send_ipc", return_value={"ok": True, "data": {"command": ""}}):
            result = runner.invoke(app, ["block", "retry", "blk-001"])
        assert result.exit_code == 1

    def test_block_retry_runs_command(self) -> None:
        import subprocess

        mock_block = {"id": "blk-001", "command": "true"}
        with patch("blink.cli._send_ipc", return_value={"ok": True, "data": mock_block}):
            with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
                result = runner.invoke(app, ["block", "retry", "blk-001"])
        assert result.exit_code == 0
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# History commands
# ---------------------------------------------------------------------------


class TestHistorySearchCommand:
    def test_history_search_help(self) -> None:
        result = runner.invoke(app, ["history", "search", "--help"])
        assert result.exit_code == 0
        assert "search" in result.output.lower()

    def test_history_search_no_results(self, tmp_path: Path) -> None:
        db_path = tmp_path / "blink.db"
        with patch.dict("os.environ", {"BLINK_DB_PATH": str(db_path)}):
            result = runner.invoke(app, ["history", "search", "xyznotfound999"])
        assert result.exit_code == 0
        assert "no history" in result.output.lower()

    def test_history_search_returns_results(self, tmp_path: Path) -> None:
        import anyio

        from blink.storage import Storage

        db_path = tmp_path / "blink.db"

        async def _seed():
            async with Storage(db_path) as s:
                await s.execute(
                    "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
                    ("git status", "/repo", 0, "2026-04-14T10:00:00Z"),
                )

        anyio.run(_seed)

        with patch.dict("os.environ", {"BLINK_DB_PATH": str(db_path)}):
            result = runner.invoke(app, ["history", "search", "git"])
        assert result.exit_code == 0
        assert "git status" in result.output


class TestHistoryStatsCommand:
    def test_history_stats_help(self) -> None:
        result = runner.invoke(app, ["history", "stats", "--help"])
        assert result.exit_code == 0

    def test_history_stats_empty_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "blink.db"
        with patch.dict("os.environ", {"BLINK_DB_PATH": str(db_path)}):
            result = runner.invoke(app, ["history", "stats"])
        assert result.exit_code == 0
        assert "0" in result.output  # total = 0

    def test_history_stats_with_data(self, tmp_path: Path) -> None:
        import anyio

        from blink.storage import Storage

        db_path = tmp_path / "blink.db"

        async def _seed():
            async with Storage(db_path) as s:
                for i in range(5):
                    await s.execute(
                        "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
                        ("ls", "/tmp", 0, f"2026-04-14T10:0{i}:00Z"),
                    )

        anyio.run(_seed)

        with patch.dict("os.environ", {"BLINK_DB_PATH": str(db_path)}):
            result = runner.invoke(app, ["history", "stats"])
        assert result.exit_code == 0
        assert "5" in result.output


class TestHistoryExportCommand:
    def test_history_export_help(self) -> None:
        result = runner.invoke(app, ["history", "export", "--help"])
        assert result.exit_code == 0

    def test_history_export_json(self, tmp_path: Path) -> None:
        import anyio

        from blink.storage import Storage

        db_path = tmp_path / "blink.db"

        async def _seed():
            async with Storage(db_path) as s:
                await s.execute(
                    "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
                    ("echo hi", "/tmp", 0, "2026-04-14T10:00:00Z"),
                )

        anyio.run(_seed)

        with patch.dict("os.environ", {"BLINK_DB_PATH": str(db_path)}):
            result = runner.invoke(app, ["history", "export", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert any(row.get("command") == "echo hi" for row in data)

    def test_history_export_csv(self, tmp_path: Path) -> None:
        import anyio

        from blink.storage import Storage

        db_path = tmp_path / "blink.db"

        async def _seed():
            async with Storage(db_path) as s:
                await s.execute(
                    "INSERT INTO history (command, cwd, exit_code, executed_at) VALUES (?,?,?,?)",
                    ("pwd", "/home", 0, "2026-04-14T10:00:00Z"),
                )

        anyio.run(_seed)

        with patch.dict("os.environ", {"BLINK_DB_PATH": str(db_path)}):
            result = runner.invoke(app, ["history", "export", "--format", "csv"])
        assert result.exit_code == 0
        assert "command" in result.output  # header row
        assert "pwd" in result.output

    def test_history_export_to_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "blink.db"
        out_file = tmp_path / "history.json"

        with patch.dict("os.environ", {"BLINK_DB_PATH": str(db_path)}):
            result = runner.invoke(
                app, ["history", "export", "--format", "json", "--output", str(out_file)]
            )
        assert result.exit_code == 0
        assert out_file.exists()

    def test_history_export_invalid_format(self, tmp_path: Path) -> None:
        db_path = tmp_path / "blink.db"
        with patch.dict("os.environ", {"BLINK_DB_PATH": str(db_path)}):
            result = runner.invoke(app, ["history", "export", "--format", "xml"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


class TestConfigShowCommand:
    def test_config_show_help(self) -> None:
        result = runner.invoke(app, ["config", "show", "--help"])
        assert result.exit_code == 0

    def test_config_show_empty(self, tmp_path: Path) -> None:
        with patch("blink.cli._CONFIG_FILE", tmp_path / "config.json"):
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "defaults" in result.output.lower() or "no configuration" in result.output.lower()

    def test_config_show_with_data(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"provider": {"default": "anthropic"}}')
        with patch("blink.cli._CONFIG_FILE", cfg_file):
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "anthropic" in result.output


class TestConfigSetCommand:
    def test_config_set_help(self) -> None:
        result = runner.invoke(app, ["config", "set", "--help"])
        assert result.exit_code == 0

    def test_config_set_simple_key(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        with patch("blink.cli._BLINK_DIR", tmp_path):
            with patch("blink.cli._CONFIG_FILE", cfg_file):
                result = runner.invoke(app, ["config", "set", "theme", "dark"])
        assert result.exit_code == 0
        assert "dark" in result.output

    def test_config_set_nested_key(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        with patch("blink.cli._BLINK_DIR", tmp_path):
            with patch("blink.cli._CONFIG_FILE", cfg_file):
                result = runner.invoke(app, ["config", "set", "provider.default", "openai"])
        assert result.exit_code == 0
        data = json.loads(cfg_file.read_text())
        assert data["provider"]["default"] == "openai"

    def test_config_set_boolean_coercion(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        with patch("blink.cli._BLINK_DIR", tmp_path):
            with patch("blink.cli._CONFIG_FILE", cfg_file):
                runner.invoke(app, ["config", "set", "completions.enabled", "true"])
        data = json.loads(cfg_file.read_text())
        assert data["completions"]["enabled"] is True

    def test_config_set_integer_coercion(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        with patch("blink.cli._BLINK_DIR", tmp_path):
            with patch("blink.cli._CONFIG_FILE", cfg_file):
                runner.invoke(app, ["config", "set", "history.limit", "100"])
        data = json.loads(cfg_file.read_text())
        assert data["history"]["limit"] == 100


class TestConfigGetCommand:
    def test_config_get_existing_key(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"theme": "dark"}')
        with patch("blink.cli._CONFIG_FILE", cfg_file):
            result = runner.invoke(app, ["config", "get", "theme"])
        assert result.exit_code == 0
        assert "dark" in result.output

    def test_config_get_missing_key(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        with patch("blink.cli._CONFIG_FILE", cfg_file):
            result = runner.invoke(app, ["config", "get", "nonexistent"])
        assert result.exit_code == 1


class TestConfigResetCommand:
    def test_config_reset_help(self) -> None:
        result = runner.invoke(app, ["config", "reset", "--help"])
        assert result.exit_code == 0

    def test_config_reset_force(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"theme": "dark"}')
        with patch("blink.cli._CONFIG_FILE", cfg_file):
            result = runner.invoke(app, ["config", "reset", "--force"])
        assert result.exit_code == 0
        assert not cfg_file.exists()

    def test_config_reset_aborted_by_user(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"theme": "dark"}')
        with patch("blink.cli._CONFIG_FILE", cfg_file):
            # Simulate user typing "n" at the confirmation prompt
            result = runner.invoke(app, ["config", "reset"], input="n\n")
        assert result.exit_code == 0
        assert cfg_file.exists()  # file should remain
