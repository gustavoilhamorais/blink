"""Blink CLI entry point."""

import typer
from rich.console import Console

from blink import __version__

app = typer.Typer(
    name="blink",
    help="Blink — an open-source Warp.dev alternative built on Kitty terminal.",
    no_args_is_help=True,
)

console = Console()

# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"blink {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Blink terminal — AI-powered shell completions and inline agents."""


# ---------------------------------------------------------------------------
# daemon subcommand group
# ---------------------------------------------------------------------------

daemon_app = typer.Typer(help="Manage the Blink background daemon.")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("start")
def daemon_start() -> None:
    """Start the Blink daemon."""
    console.print("[yellow]daemon start[/yellow] — not yet implemented")
    raise typer.Exit(1)


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the Blink daemon."""
    console.print("[yellow]daemon stop[/yellow] — not yet implemented")
    raise typer.Exit(1)


@daemon_app.command("status")
def daemon_status() -> None:
    """Show the current daemon status."""
    console.print("[yellow]daemon status[/yellow] — not yet implemented")
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# block subcommand group
# ---------------------------------------------------------------------------

block_app = typer.Typer(help="Manage Blink output blocks.")
app.add_typer(block_app, name="block")


@block_app.command("list")
def block_list() -> None:
    """List recent output blocks."""
    console.print("[yellow]block list[/yellow] — not yet implemented")
    raise typer.Exit(1)


@block_app.command("show")
def block_show(block_id: str = typer.Argument(..., help="Block ID to display.")) -> None:
    """Display a specific output block."""
    console.print(f"[yellow]block show {block_id}[/yellow] — not yet implemented")
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# provider subcommand group
# ---------------------------------------------------------------------------

provider_app = typer.Typer(help="Manage AI provider configurations.")
app.add_typer(provider_app, name="provider")


@provider_app.command("list")
def provider_list() -> None:
    """List configured AI providers."""
    console.print("[yellow]provider list[/yellow] — not yet implemented")
    raise typer.Exit(1)


@provider_app.command("add")
def provider_add(
    name: str = typer.Argument(..., help="Provider name (e.g. openai, anthropic)."),
) -> None:
    """Add and configure an AI provider."""
    console.print(f"[yellow]provider add {name}[/yellow] — not yet implemented")
    raise typer.Exit(1)


@provider_app.command("remove")
def provider_remove(
    name: str = typer.Argument(..., help="Provider name to remove."),
) -> None:
    """Remove an AI provider configuration."""
    console.print(f"[yellow]provider remove {name}[/yellow] — not yet implemented")
    raise typer.Exit(1)
