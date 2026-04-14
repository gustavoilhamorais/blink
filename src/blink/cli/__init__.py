"""Blink CLI entry point."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from blink import __version__

app = typer.Typer(
    name="blink",
    help="Blink — an open-source Warp.dev alternative built on Kitty terminal.",
    no_args_is_help=True,
)

console = Console()

# Daemon paths (mirror daemon/app.py defaults so we don't import the daemon here)
_BLINK_DIR = Path(os.environ.get("BLINK_DIR", Path.home() / ".blink"))
_PID_FILE = _BLINK_DIR / "blink.pid"
_SOCKET_PATH = _BLINK_DIR / "blink.sock"

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
# Daemon helpers
# ---------------------------------------------------------------------------


def _read_pid() -> int | None:
    if _PID_FILE.exists():
        try:
            return int(_PID_FILE.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def _daemon_alive(pid: int) -> bool:
    """Return True if a process with *pid* exists."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _socket_ping() -> bool:
    """Return True if the daemon socket is accepting connections."""
    if not _SOCKET_PATH.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(str(_SOCKET_PATH))
            s.sendall(b'{"cmd":"ping","params":{}}\n')
            data = s.recv(256)
            return b"pong" in data
    except OSError:
        return False


# ---------------------------------------------------------------------------
# daemon subcommand group
# ---------------------------------------------------------------------------

daemon_app = typer.Typer(help="Manage the Blink background daemon.")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("start")
def daemon_start(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in the foreground."),
) -> None:
    """Start the Blink daemon."""
    pid = _read_pid()
    if pid is not None and _daemon_alive(pid):
        console.print(f"[green]Blink daemon is already running[/green] (pid {pid})")
        raise typer.Exit(0)

    if foreground:
        console.print("[cyan]Starting Blink daemon in the foreground…[/cyan]")
        from blink.daemon.app import main as daemon_main

        daemon_main()
        return

    # Launch as background subprocess
    _BLINK_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _BLINK_DIR / "blink.log"
    with log_path.open("ab") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "blink.daemon.app"],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
    console.print(f"[green]Blink daemon started[/green] (pid {proc.pid})")


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the Blink daemon."""
    pid = _read_pid()
    if pid is None:
        console.print("[yellow]No PID file found — is the daemon running?[/yellow]")
        raise typer.Exit(1)
    if not _daemon_alive(pid):
        console.print(f"[yellow]PID {pid} is not alive; cleaning up.[/yellow]")
        _PID_FILE.unlink(missing_ok=True)
        raise typer.Exit(1)
    os.kill(pid, signal.SIGTERM)
    console.print(f"[green]Sent SIGTERM to daemon[/green] (pid {pid})")


@daemon_app.command("status")
def daemon_status() -> None:
    """Check if the Blink daemon is running."""
    pid = _read_pid()
    if pid is None:
        console.print("[red]Blink daemon is not running[/red] (no PID file)")
        raise typer.Exit(1)
    if not _daemon_alive(pid):
        console.print(f"[red]Blink daemon is not running[/red] (stale PID {pid})")
        raise typer.Exit(1)
    alive = _socket_ping()
    if alive:
        console.print(f"[green]Blink daemon is running[/green] (pid {pid}, socket OK)")
    else:
        console.print(f"[yellow]Blink daemon is running[/yellow] (pid {pid}, socket not ready)")


# ---------------------------------------------------------------------------
# block subcommand group
# ---------------------------------------------------------------------------

block_app = typer.Typer(help="Manage Blink output blocks.")
app.add_typer(block_app, name="block")


def _send_ipc(cmd: str, params: dict) -> dict:  # type: ignore[type-arg]
    """Send a single IPC command to the daemon and return the response dict."""
    import json

    if not _SOCKET_PATH.exists():
        raise RuntimeError(
            "Blink daemon socket not found. Is the daemon running? Try: blink daemon start"
        )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5.0)
        s.connect(str(_SOCKET_PATH))
        s.sendall((json.dumps({"cmd": cmd, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n")[0])  # type: ignore[return-value]


@block_app.command("list")
def block_list(
    session_id: str = typer.Option(
        "", "--session", "-s", help="Session ID to query (defaults to env var BLINK_SESSION_ID)."
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of blocks to show."),
) -> None:
    """List recent output blocks from the current session."""
    sid = session_id or os.environ.get("BLINK_SESSION_ID", "")
    if not sid:
        console.print(
            "[yellow]No session ID provided.[/yellow] "
            "Set BLINK_SESSION_ID or pass --session."
        )
        raise typer.Exit(1)

    try:
        resp = _send_ipc("get_recent_blocks", {"session_id": sid, "limit": limit})
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if not resp.get("ok"):
        console.print(f"[red]Daemon error:[/red] {resp.get('error')}")
        raise typer.Exit(1)

    blocks = resp.get("data", [])
    if not blocks:
        console.print("[dim]No blocks found for this session.[/dim]")
        raise typer.Exit(0)

    table = Table(title=f"Recent blocks — session {sid[:8]}…", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Command", style="bold")
    table.add_column("Exit", width=5)
    table.add_column("CWD", style="dim")
    table.add_column("Ended at", style="dim")

    for i, b in enumerate(blocks, 1):
        exit_code = b.get("exit_code")
        exit_str = (
            f"[green]{exit_code}[/green]"
            if exit_code == 0
            else f"[red]{exit_code}[/red]"
            if exit_code is not None
            else "—"
        )
        table.add_row(
            str(i),
            b.get("command", "") or "[dim]<empty>[/dim]",
            exit_str,
            b.get("cwd", ""),
            (b.get("ended_at") or "")[:19],
        )

    console.print(table)


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
