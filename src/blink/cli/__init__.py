"""Blink CLI entry point."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

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
# mcp subcommand group
# ---------------------------------------------------------------------------

mcp_app = typer.Typer(help="MCP server commands.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve(
    transport: str = typer.Option(
        "stdio",
        "--transport",
        "-t",
        help="Transport to use: 'stdio' (default) or 'http'.",
    ),
    capability: str = typer.Option(
        "observe",
        "--capability",
        "-c",
        help="Granted capability level: observe, suggest, act, or admin.",
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help="Disable interactive confirmation for ACT-level tools.",
    ),
) -> None:
    """Start the Blink MCP server.

    Use --transport stdio (default) for local agents.
    The server communicates via JSON-RPC 2.0 over newline-delimited messages.
    """
    import anyio

    from blink.mcp.server import run_server
    from blink.security.capabilities import Capability, SecurityPolicy

    try:
        cap = Capability(capability.lower())
    except ValueError:
        console.print(
            f"[red]Unknown capability level:[/red] {capability!r}. "
            "Choose from: observe, suggest, act, admin."
        )
        raise typer.Exit(1) from None

    policy = SecurityPolicy(
        granted_capability=cap,
        require_confirmation=not no_confirm,
    )

    if transport != "stdio":
        console.print(f"[yellow]Transport '{transport}' is not yet implemented.[/yellow]")
        raise typer.Exit(1)

    anyio.run(run_server, transport, None, None, policy)


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


# ---------------------------------------------------------------------------
# agent subcommand group
# ---------------------------------------------------------------------------

agent_app = typer.Typer(help="Agent interaction commands.")
app.add_typer(agent_app, name="agent")


def _build_gateway(session_id: str) -> tuple[Any, Any]:
    """Build a gateway + run_manager pair for CLI agent commands.

    Returns (gateway, run_manager).  Caller is responsible for setting up
    anyio event loop context.
    """
    from blink.acp.gateway import ACPGateway
    from blink.acp.runs import RunManager
    from blink.acp.sessions import SessionManager
    from blink.daemon.app import BlinkDaemon
    from blink.mcp.server import MCPServer
    from blink.security.capabilities import Capability, SecurityPolicy
    from blink.storage import Storage

    # Minimal bootstrap: storage -> daemon -> mcp -> gateway
    storage = Storage()
    daemon = BlinkDaemon()
    policy = SecurityPolicy(
        granted_capability=Capability.ACT,
        require_confirmation=True,
    )
    mcp_server = MCPServer(daemon=daemon, policy=policy)
    run_manager = RunManager(storage=storage)
    session_manager = SessionManager(storage=storage, daemon=daemon)
    gateway = ACPGateway(
        mcp_server=mcp_server,
        providers={},
        run_manager=run_manager,
        session_manager=session_manager,
    )
    return gateway, run_manager


@agent_app.command("ask")
def agent_ask(
    prompt: str = typer.Argument(..., help="Prompt to send to the agent."),
    session_id: str = typer.Option(
        "",
        "--session",
        "-s",
        help="Blink session ID (defaults to BLINK_SESSION_ID env var).",
    ),
    no_stream: bool = typer.Option(False, "--no-stream", help="Wait for full response before printing."),
) -> None:
    """Send a prompt to the agent and stream the response."""
    import anyio

    from blink.acp.gateway import RunMode

    sid = session_id or os.environ.get("BLINK_SESSION_ID", "")
    if not sid:
        console.print(
            "[yellow]No session ID provided.[/yellow] "
            "Set BLINK_SESSION_ID or pass --session."
        )
        raise typer.Exit(1)

    async def _run() -> None:
        from blink.acp.gateway import ACPGateway
        from blink.acp.runs import RunManager
        from blink.acp.sessions import SessionManager
        from blink.daemon.app import BlinkDaemon
        from blink.mcp.server import MCPServer
        from blink.security.capabilities import Capability, SecurityPolicy
        from blink.storage import Storage

        storage = Storage()
        await storage.init_db()
        daemon = BlinkDaemon()
        await daemon._storage.init_db()
        policy = SecurityPolicy(
            granted_capability=Capability.ACT,
            require_confirmation=True,
        )
        mcp_server = MCPServer(daemon=daemon, policy=policy)
        run_manager = RunManager(storage=storage)
        session_manager = SessionManager(storage=storage, daemon=daemon)
        gateway = ACPGateway(
            mcp_server=mcp_server,
            providers={},
            run_manager=run_manager,
            session_manager=session_manager,
        )

        mode = RunMode.SYNC if no_stream else RunMode.STREAM
        run = await gateway.create_run(prompt=prompt, session_id=sid, mode=mode)
        console.print(f"[dim]Run {run.id[:8]}…[/dim]")

        async for event in await gateway.stream_run(run):
            if event.type == "text":
                console.print(str(event.data or ""), end="")
            elif event.type == "tool_call":
                data = event.data or {}
                if isinstance(data, dict):
                    console.print(f"\n[dim cyan][tool] {data.get('tool', '')}[/dim cyan]")
            elif event.type == "awaiting":
                data = event.data or {}
                if isinstance(data, dict):
                    action = data.get("action", {})
                    preview = action.get("preview", "Unknown action") if isinstance(action, dict) else str(action)
                    console.print(f"\n[yellow][confirm] {preview}[/yellow]")
                    answer = typer.prompt("Allow? [y/N]", default="N")
                    run_id = data.get("run_id", run.id)
                    approved = answer.strip().lower() in {"y", "yes"}
                    await run_manager.resolve_pending(run_id, approved)
            elif event.type == "completed":
                console.print()  # newline after streamed text
            elif event.type == "error":
                data = event.data or {}
                msg = data.get("message", str(data)) if isinstance(data, dict) else str(data)
                console.print(f"\n[red]Error:[/red] {msg}")
            elif event.type == "cancelled":
                console.print("\n[yellow]Cancelled.[/yellow]")

        await storage.close()
        await daemon._storage.close()

    try:
        anyio.run(_run)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        raise typer.Exit(1) from None


@agent_app.command("cancel")
def agent_cancel(
    run_id: str = typer.Argument(..., help="Run ID to cancel."),
) -> None:
    """Cancel a running agent run."""
    import anyio

    async def _cancel() -> None:
        from blink.acp.runs import RunManager
        from blink.storage import Storage

        storage = Storage()
        await storage.init_db()
        run_manager = RunManager(storage=storage)
        run = await run_manager.get(run_id)
        if run is None:
            console.print(f"[red]Run {run_id!r} not found.[/red]")
            raise typer.Exit(1)
        if run.is_terminal():
            console.print(f"[yellow]Run {run_id[:8]}… is already in terminal state: {run.state}[/yellow]")
        else:
            await run_manager.cancel(run_id)
            console.print(f"[green]Run {run_id[:8]}… cancelled.[/green]")
        await storage.close()

    anyio.run(_cancel)


@agent_app.command("status")
def agent_status(
    run_id: str = typer.Argument(..., help="Run ID to inspect."),
) -> None:
    """Show the status of an agent run."""
    import anyio

    async def _status() -> None:
        from blink.acp.runs import RunManager
        from blink.storage import Storage

        storage = Storage()
        await storage.init_db()
        run_manager = RunManager(storage=storage)
        run = await run_manager.get(run_id)
        if run is None:
            console.print(f"[red]Run {run_id!r} not found.[/red]")
            raise typer.Exit(1)
        console.print(f"[bold]Run:[/bold] {run.id}")
        console.print(f"[bold]State:[/bold] {run.state}")
        console.print(f"[bold]Session:[/bold] {run.session_id}")
        console.print(f"[bold]Prompt:[/bold] {run.prompt}")
        console.print(f"[bold]Created:[/bold] {run.created_at.isoformat()}")
        if run.result:
            console.print(f"[bold]Result:[/bold] {run.result[:200]}")
        if run.error:
            console.print(f"[bold red]Error:[/bold red] {run.error}")
        if run.pending_action:
            console.print(f"[bold yellow]Pending:[/bold yellow] {run.pending_action.preview}")
        await storage.close()

    anyio.run(_status)


@agent_app.command("list")
def agent_list(
    session_id: str = typer.Option(
        "",
        "--session",
        "-s",
        help="Session ID to query (defaults to BLINK_SESSION_ID env var).",
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum number of runs to show."),
) -> None:
    """List recent agent runs for a session."""
    import anyio

    sid = session_id or os.environ.get("BLINK_SESSION_ID", "")
    if not sid:
        console.print(
            "[yellow]No session ID provided.[/yellow] "
            "Set BLINK_SESSION_ID or pass --session."
        )
        raise typer.Exit(1)

    async def _list() -> None:
        from blink.acp.runs import RunManager
        from blink.storage import Storage

        storage = Storage()
        await storage.init_db()
        run_manager = RunManager(storage=storage)
        runs = await run_manager.list_for_session(sid)
        runs = runs[:limit]

        if not runs:
            console.print("[dim]No runs found for this session.[/dim]")
            await storage.close()
            return

        table = Table(title=f"Agent runs — session {sid[:8]}…", show_lines=True)
        table.add_column("Run ID", style="dim", width=10)
        table.add_column("State", width=14)
        table.add_column("Prompt")
        table.add_column("Created", style="dim", width=20)

        state_styles = {
            "completed": "[green]completed[/green]",
            "failed": "[red]failed[/red]",
            "cancelled": "[yellow]cancelled[/yellow]",
            "in_progress": "[cyan]in_progress[/cyan]",
            "awaiting_input": "[yellow]awaiting_input[/yellow]",
            "created": "[dim]created[/dim]",
        }

        for run in runs:
            state_display = state_styles.get(run.state, run.state)
            table.add_row(
                run.id[:8] + "…",
                state_display,
                run.prompt[:60] + ("…" if len(run.prompt) > 60 else ""),
                run.created_at.isoformat()[:19],
            )

        console.print(table)
        await storage.close()

    anyio.run(_list)


@agent_app.command("approve")
def agent_approve(
    run_id: str = typer.Argument(..., help="Run ID with a pending action to approve."),
) -> None:
    """Approve a pending action for a paused agent run."""
    import anyio

    async def _approve() -> None:
        from blink.acp.runs import RunManager
        from blink.storage import Storage

        storage = Storage()
        await storage.init_db()
        run_manager = RunManager(storage=storage)
        run = await run_manager.get(run_id)
        if run is None:
            console.print(f"[red]Run {run_id!r} not found.[/red]")
            raise typer.Exit(1)
        if run.state != "awaiting_input":
            console.print(f"[yellow]Run is not awaiting input (state: {run.state})[/yellow]")
            raise typer.Exit(1)
        if run.pending_action:
            console.print(f"[bold]Action:[/bold] {run.pending_action.preview}")
        updated = await run_manager.resolve_pending(run_id, approved=True)
        console.print(f"[green]Approved. Run {run_id[:8]}… resumed (state: {updated.state})[/green]")
        await storage.close()

    anyio.run(_approve)


@agent_app.command("deny")
def agent_deny(
    run_id: str = typer.Argument(..., help="Run ID with a pending action to deny."),
) -> None:
    """Deny a pending action for a paused agent run."""
    import anyio

    async def _deny() -> None:
        from blink.acp.runs import RunManager
        from blink.storage import Storage

        storage = Storage()
        await storage.init_db()
        run_manager = RunManager(storage=storage)
        run = await run_manager.get(run_id)
        if run is None:
            console.print(f"[red]Run {run_id!r} not found.[/red]")
            raise typer.Exit(1)
        if run.state != "awaiting_input":
            console.print(f"[yellow]Run is not awaiting input (state: {run.state})[/yellow]")
            raise typer.Exit(1)
        await run_manager.resolve_pending(run_id, approved=False)
        console.print(f"[yellow]Denied. Run {run_id[:8]}… cancelled.[/yellow]")
        await storage.close()

    anyio.run(_deny)
