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


@daemon_app.command("logs")
def daemon_logs(
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new log lines as they arrive."),
    lines: int = typer.Option(50, "--lines", "-n", help="Number of recent lines to show."),
) -> None:
    """Show daemon logs."""
    log_path = _BLINK_DIR / "blink.log"
    if not log_path.exists():
        console.print("[yellow]No log file found at[/yellow] " + str(log_path))
        raise typer.Exit(1)

    if follow:
        console.print(f"[dim]Tailing {log_path} (Ctrl-C to stop)…[/dim]")
        try:
            import subprocess as _sp

            proc = _sp.Popen(
                ["tail", "-n", str(lines), "-f", str(log_path)],
                stdout=_sp.PIPE,
                text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                console.print(line, end="")
        except KeyboardInterrupt:
            pass
        return

    # Static tail
    log_lines = log_path.read_text(errors="replace").splitlines()
    for line in log_lines[-lines:]:
        console.print(line)


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
def block_show(
    block_id: str = typer.Argument(..., help="Block ID to display."),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text | json."),
) -> None:
    """Show block details (command, output, exit code, timing)."""
    try:
        resp = _send_ipc("get_block", {"block_id": block_id})
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if not resp.get("ok"):
        console.print(f"[red]Daemon error:[/red] {resp.get('error')}")
        raise typer.Exit(1)

    block = resp.get("data")
    if not block:
        console.print(f"[yellow]Block {block_id!r} not found.[/yellow]")
        raise typer.Exit(1)

    if format == "json":
        import json

        console.print(json.dumps(block, indent=2))
        return

    exit_code = block.get("exit_code")
    exit_str = (
        f"[green]{exit_code}[/green]"
        if exit_code == 0
        else f"[red]{exit_code}[/red]"
        if exit_code is not None
        else "—"
    )
    console.print(f"[bold]Block:[/bold]   {block.get('id', '')}")
    console.print(f"[bold]Command:[/bold] {block.get('command', '')}")
    console.print(f"[bold]CWD:[/bold]     {block.get('cwd', '')}")
    console.print(f"[bold]Exit:[/bold]    {exit_str}")
    console.print(f"[bold]Started:[/bold] {block.get('started_at', '')}")
    console.print(f"[bold]Ended:[/bold]   {block.get('ended_at', '')}")
    output = block.get("output", "")
    if output:
        console.print("\n[bold]Output:[/bold]")
        console.print(output)


@block_app.command("explain")
def block_explain(block_id: str = typer.Argument(..., help="Block ID to explain.")) -> None:
    """Ask the agent to explain a block's output."""
    import anyio

    sid = os.environ.get("BLINK_SESSION_ID", "")
    if not sid:
        console.print(
            "[yellow]No session ID found.[/yellow] "
            "Set BLINK_SESSION_ID to use agent features."
        )
        raise typer.Exit(1)

    # Fetch the block first
    try:
        resp = _send_ipc("get_block", {"block_id": block_id})
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if not resp.get("ok") or not resp.get("data"):
        console.print(f"[yellow]Block {block_id!r} not found.[/yellow]")
        raise typer.Exit(1)

    block = resp["data"]
    cmd = block.get("command", "")
    output = (block.get("output") or "")[:2000]
    exit_code = block.get("exit_code")

    prompt = (
        f"Please explain the following terminal command and its output.\n\n"
        f"Command: {cmd}\n"
        f"Exit code: {exit_code}\n"
        f"Output:\n{output}"
    )

    console.print(f"[dim]Asking agent to explain block {block_id[:8]}…[/dim]\n")

    async def _run() -> None:
        from blink.acp.gateway import ACPGateway, RunMode
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
        policy = SecurityPolicy(granted_capability=Capability.OBSERVE, require_confirmation=False)
        mcp_server = MCPServer(daemon=daemon, policy=policy)
        run_manager = RunManager(storage=storage)
        session_manager = SessionManager(storage=storage, daemon=daemon)
        gateway = ACPGateway(
            mcp_server=mcp_server,
            providers={},
            run_manager=run_manager,
            session_manager=session_manager,
        )
        run = await gateway.create_run(prompt=prompt, session_id=sid, mode=RunMode.STREAM)
        async for event in await gateway.stream_run(run):
            if event.type == "text":
                console.print(str(event.data or ""), end="")
            elif event.type == "completed":
                console.print()
            elif event.type == "error":
                data = event.data or {}
                msg = data.get("message", str(data)) if isinstance(data, dict) else str(data)
                console.print(f"\n[red]Error:[/red] {msg}")
        await storage.close()
        await daemon._storage.close()

    try:
        anyio.run(_run)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        raise typer.Exit(1) from None


@block_app.command("retry")
def block_retry(block_id: str = typer.Argument(..., help="Block ID to re-run.")) -> None:
    """Re-run the command from a block."""
    try:
        resp = _send_ipc("get_block", {"block_id": block_id})
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if not resp.get("ok") or not resp.get("data"):
        console.print(f"[yellow]Block {block_id!r} not found.[/yellow]")
        raise typer.Exit(1)

    block = resp["data"]
    cmd = block.get("command", "").strip()
    if not cmd:
        console.print("[yellow]Block has no command to retry.[/yellow]")
        raise typer.Exit(1)

    console.print(f"[dim]Re-running:[/dim] {cmd}")
    import subprocess

    result = subprocess.run(cmd, shell=True)  # noqa: S602
    raise typer.Exit(result.returncode)


# ---------------------------------------------------------------------------
# history subcommand group
# ---------------------------------------------------------------------------

history_app = typer.Typer(help="Command history operations.")
app.add_typer(history_app, name="history")


@history_app.command("search")
def history_search(
    query: str = typer.Argument(..., help="Search query (substring or regex)."),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results to show."),
    regex: bool = typer.Option(False, "--regex", "-r", help="Treat query as a regex pattern."),
) -> None:
    """Search command history."""
    import anyio

    async def _search() -> None:
        from blink.storage import Storage

        async with Storage() as storage:
            if regex:
                # SQLite REGEXP requires user-defined function; use LIKE fallback with note
                console.print("[dim]Note: using LIKE matching (not full regex)[/dim]")
                rows = await storage.fetchall(
                    "SELECT command, cwd, exit_code, executed_at FROM history "
                    "WHERE command LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"%{query}%", limit),
                )
            else:
                rows = await storage.fetchall(
                    "SELECT command, cwd, exit_code, executed_at FROM history "
                    "WHERE command LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"%{query}%", limit),
                )

        if not rows:
            console.print(f"[dim]No history found matching {query!r}[/dim]")
            return

        from rich.table import Table

        table = Table(title=f"History search: {query!r}", show_lines=True)
        table.add_column("Command", style="bold")
        table.add_column("Exit", width=5)
        table.add_column("CWD", style="dim")
        table.add_column("Executed at", style="dim", width=20)

        for row in rows:
            exit_code = row.get("exit_code")
            exit_str = (
                f"[green]{exit_code}[/green]"
                if exit_code == 0
                else f"[red]{exit_code}[/red]"
                if exit_code is not None
                else "—"
            )
            table.add_row(
                row.get("command", ""),
                exit_str,
                row.get("cwd", ""),
                (row.get("executed_at") or "")[:19],
            )

        console.print(table)

    anyio.run(_search)


@history_app.command("stats")
def history_stats() -> None:
    """Show command history statistics."""
    import anyio

    async def _stats() -> None:
        from blink.storage import Storage

        async with Storage() as storage:
            total_row = await storage.fetchone("SELECT COUNT(*) as cnt FROM history")
            total = (total_row or {}).get("cnt", 0)

            top_cmds = await storage.fetchall(
                "SELECT command, COUNT(*) as cnt FROM history "
                "GROUP BY command ORDER BY cnt DESC LIMIT 10"
            )
            fail_row = await storage.fetchone(
                "SELECT COUNT(*) as cnt FROM history WHERE exit_code != 0"
            )
            fail_count = (fail_row or {}).get("cnt", 0)

            top_dirs = await storage.fetchall(
                "SELECT cwd, COUNT(*) as cnt FROM history WHERE cwd != '' "
                "GROUP BY cwd ORDER BY cnt DESC LIMIT 5"
            )

        from rich.table import Table

        console.print(f"\n[bold]Total commands recorded:[/bold] {total}")
        if total > 0:
            fail_pct = 100 * int(fail_count) / int(total)
            console.print(f"[bold]Failed commands:[/bold] {fail_count} ({fail_pct:.1f}%)\n")

        if top_cmds:
            t = Table(title="Top 10 Commands", show_lines=False)
            t.add_column("Command", style="bold")
            t.add_column("Count", justify="right")
            for row in top_cmds:
                t.add_row(row.get("command", ""), str(row.get("cnt", 0)))
            console.print(t)

        if top_dirs:
            t2 = Table(title="Top 5 Directories", show_lines=False)
            t2.add_column("Directory", style="dim")
            t2.add_column("Count", justify="right")
            for row in top_dirs:
                t2.add_row(row.get("cwd", ""), str(row.get("cnt", 0)))
            console.print(t2)

    anyio.run(_stats)


@history_app.command("export")
def history_export(
    format: str = typer.Option("json", "--format", "-f", help="Export format: json | csv | tsv."),
    output: str = typer.Option("-", "--output", "-o", help="Output file path (- for stdout)."),
    limit: int = typer.Option(0, "--limit", "-n", help="Max rows (0 = all)."),
) -> None:
    """Export command history."""
    import anyio

    async def _export() -> None:
        import csv
        import io
        import json

        from blink.storage import Storage

        async with Storage() as storage:
            sql = "SELECT id, command, cwd, exit_code, executed_at FROM history ORDER BY id ASC"
            params: tuple[()] | tuple[int] = ()
            if limit > 0:
                sql += " LIMIT ?"
                params = (limit,)
            rows = await storage.fetchall(sql, params)

        if format == "json":
            text = json.dumps(rows, indent=2)
        elif format in ("csv", "tsv"):
            delim = "\t" if format == "tsv" else ","
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf,
                fieldnames=["id", "command", "cwd", "exit_code", "executed_at"],
                delimiter=delim,
            )
            writer.writeheader()
            writer.writerows(rows)
            text = buf.getvalue()
        else:
            console.print(f"[red]Unknown format:[/red] {format!r}. Choose json, csv, or tsv.")
            raise typer.Exit(1)

        if output == "-":
            console.print(text)
        else:
            from pathlib import Path

            Path(output).write_text(text)
            console.print(f"[green]Exported {len(rows)} rows to {output}[/green]")

    anyio.run(_export)


# ---------------------------------------------------------------------------
# config subcommand group
# ---------------------------------------------------------------------------

config_app = typer.Typer(help="Configuration management.")
app.add_typer(config_app, name="config")

_CONFIG_FILE = _BLINK_DIR / "config.json"


def _load_config() -> dict:  # type: ignore[type-arg]
    """Load config from the JSON file, returning defaults if absent."""
    import json

    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_config(cfg: dict) -> None:  # type: ignore[type-arg]
    """Persist config to the JSON file."""
    import json

    _BLINK_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


@config_app.command("show")
def config_show() -> None:
    """Show current configuration."""
    import json

    cfg = _load_config()
    if not cfg:
        console.print("[dim]No configuration set. Using defaults.[/dim]")
        return
    console.print(json.dumps(cfg, indent=2))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (e.g. 'provider.default')."),
    value: str = typer.Argument(..., help="Value to set."),
) -> None:
    """Set a configuration value.

    Supports dot-notation for nested keys: ``blink config set provider.default anthropic``
    """
    cfg = _load_config()

    # Walk/create nested path
    parts = key.split(".")
    node: dict = cfg  # type: ignore[type-arg]
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]

    # Coerce common value types
    if value.lower() in ("true", "yes"):
        typed_value: object = True
    elif value.lower() in ("false", "no"):
        typed_value = False
    elif value.isdigit():
        typed_value = int(value)
    else:
        typed_value = value

    node[parts[-1]] = typed_value
    _save_config(cfg)
    console.print(f"[green]Set[/green] {key} = {typed_value!r}")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help="Config key to retrieve."),
) -> None:
    """Get a configuration value."""
    import json

    cfg = _load_config()
    parts = key.split(".")
    node: object = cfg
    for part in parts:
        if not isinstance(node, dict):
            console.print(f"[yellow]Key {key!r} not found.[/yellow]")
            raise typer.Exit(1)
        node = node.get(part)  # type: ignore[union-attr]
        if node is None:
            console.print(f"[yellow]Key {key!r} not found.[/yellow]")
            raise typer.Exit(1)
    console.print(json.dumps(node, indent=2) if isinstance(node, (dict, list)) else str(node))


@config_app.command("reset")
def config_reset(
    force: bool = typer.Option(False, "--force", "-y", help="Skip confirmation prompt."),
) -> None:
    """Reset to default configuration."""
    if not force:
        confirm = typer.confirm("Reset all configuration to defaults?")
        if not confirm:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)

    if _CONFIG_FILE.exists():
        _CONFIG_FILE.unlink()
    console.print("[green]Configuration reset to defaults.[/green]")


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


@provider_app.command("test")
def provider_test(
    name: str = typer.Argument(..., help="Provider name to test (e.g. anthropic, openai, ollama)."),
) -> None:
    """Test provider connectivity by sending a minimal request."""
    import anyio

    async def _test() -> None:
        from blink.providers.base import CompletionProvider, ProviderConfig

        # Map name to provider class
        provider: CompletionProvider | None = None
        provider_lower = name.lower()

        if provider_lower == "anthropic":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                console.print("[red]ANTHROPIC_API_KEY not set.[/red]")
                raise typer.Exit(1)
            from blink.providers.anthropic import AnthropicProvider

            provider = AnthropicProvider(ProviderConfig(name="anthropic", api_key=api_key))
        elif provider_lower in ("openai", "openai-compatible"):
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                console.print("[red]OPENAI_API_KEY not set.[/red]")
                raise typer.Exit(1)
            from blink.providers.openai import OpenAIProvider

            provider = OpenAIProvider(ProviderConfig(name="openai", api_key=api_key))
        elif provider_lower == "ollama":
            from blink.providers.ollama import OllamaProvider

            provider = OllamaProvider(ProviderConfig(name="ollama"))
        else:
            console.print(
                f"[red]Unknown provider:[/red] {name!r}. "
                "Choose from: anthropic, openai, ollama."
            )
            raise typer.Exit(1)

        console.print(f"[dim]Testing {name} provider connectivity…[/dim]")
        try:
            reachable = await provider.health_check()
            if reachable:
                console.print(f"[green]{name} provider is reachable.[/green]")
            else:
                console.print(f"[yellow]{name} provider health check returned False.[/yellow]")
                raise typer.Exit(1)
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{name} provider FAILED:[/red] {exc}")
            raise typer.Exit(1) from exc

    anyio.run(_test)


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
