"""Smoke tests for the Blink CLI."""

from typer.testing import CliRunner

from blink import __version__
from blink.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_version_short_flag() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "blink" in result.output.lower()


def test_daemon_help() -> None:
    result = runner.invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    assert "daemon" in result.output.lower()


def test_block_help() -> None:
    result = runner.invoke(app, ["block", "--help"])
    assert result.exit_code == 0
    assert "block" in result.output.lower()


def test_provider_help() -> None:
    result = runner.invoke(app, ["provider", "--help"])
    assert result.exit_code == 0
    assert "provider" in result.output.lower()


def test_daemon_start_not_implemented() -> None:
    result = runner.invoke(app, ["daemon", "start"])
    assert result.exit_code == 1


def test_provider_list_not_implemented() -> None:
    result = runner.invoke(app, ["provider", "list"])
    assert result.exit_code == 1
