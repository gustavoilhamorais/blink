"""Kitten for agent interaction overlays.

Shows approval prompts, block inspector, and agent status in a floating overlay.
Invoked by Kitty as: kitty +kitten blink_agent_overlay <args>

Args:
    --mode: overlay mode — approve | inspect | status
    --run-id: ACP run ID (for approve/status modes)
    --block-id: block ID (for inspect mode)
    --action: action preview text (for approve mode)
"""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Kitty kitten API
# ---------------------------------------------------------------------------


def main(args: list[str]) -> str | None:
    """Entry point called by Kitty.

    Parses args, renders the overlay UI, and returns the user's answer as a
    string that will be forwarded to :func:`handle_result`.

    Supported modes:

    - ``approve``  — show an approve/deny prompt for a pending agent action
    - ``inspect``  — display block details (command, output, exit code, CWD)
    - ``status``   — show current agent run status
    """
    mode, run_id, block_id, action, title = _parse_args(args)

    if mode == "approve":
        return _run_approve_overlay(action or "Unknown action", run_id or "")
    elif mode == "inspect":
        return _run_inspect_overlay(block_id or "")
    elif mode == "status":
        return _run_status_overlay(run_id or "")
    else:
        _print_error(f"Unknown mode: {mode!r}. Use --mode approve|inspect|status")
        return None


def handle_result(
    args: list[str],
    answer: str,
    target_window_id: int,
    boss: Any,
) -> None:
    """Handle the result of the kitten execution.

    Called by Kitty after :func:`main` returns.  Sends the user's decision
    back to the Blink daemon via IPC.

    Args:
        args: Original CLI args passed to the kitten.
        answer: String returned by :func:`main` (e.g. ``"approved"`` / ``"denied"``).
        target_window_id: Kitty window that launched the kitten.
        boss: Kitty boss object (used to send RC commands).
    """
    mode, run_id, block_id, _, _ = _parse_args(args)

    if mode == "approve" and run_id:
        approved = answer.strip().lower() == "approved"
        _notify_daemon_approval(run_id, approved)
    elif mode == "inspect":
        # Nothing to do after inspect — just close overlay
        pass
    elif mode == "status":
        pass


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

_BANNER = "\033[1;36m[blink]\033[0m"
_OK = "\033[1;32m"
_WARN = "\033[1;33m"
_ERR = "\033[1;31m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _run_approve_overlay(action: str, run_id: str) -> str:
    """Render the approve/deny prompt and return 'approved' or 'denied'."""
    _clear_screen()
    print(f"\n{_BANNER} Agent Action Approval\n")
    print(f"  Run ID : {_DIM}{run_id[:16]}{'...' if len(run_id) > 16 else ''}{_RESET}")
    print(f"  Action : {_WARN}{action}{_RESET}\n")
    print("  This action will be executed on your behalf.")
    print(f"  {_DIM}Press [y] to approve or [n] to deny.{_RESET}\n")

    while True:
        try:
            ch = _read_single_char("  Allow? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            ch = "n"

        if ch.lower() in ("y", "yes"):
            print(f"\n  {_OK}Approved.{_RESET}\n")
            return "approved"
        elif ch.lower() in ("n", "no", ""):
            print(f"\n  {_WARN}Denied.{_RESET}\n")
            return "denied"
        else:
            print("  Please enter y or n.")


def _run_inspect_overlay(block_id: str) -> str:
    """Display block details fetched from the daemon."""
    _clear_screen()
    print(f"\n{_BANNER} Block Inspector — {_DIM}{block_id[:16]}{_RESET}\n")

    block = _fetch_block(block_id)
    if block is None:
        print(f"  {_ERR}Block not found: {block_id}{_RESET}\n")
        _wait_for_key()
        return ""

    cmd = block.get("command", "")
    cwd = block.get("cwd", "")
    exit_code = block.get("exit_code")
    output = block.get("output", "")

    exit_color = _OK if exit_code == 0 else _ERR
    print(f"  Command    : {_WARN}{cmd}{_RESET}")
    print(f"  CWD        : {_DIM}{cwd}{_RESET}")
    print(f"  Exit code  : {exit_color}{exit_code}{_RESET}")
    print(f"  Started    : {_DIM}{block.get('started_at', '')}{_RESET}")
    print(f"  Ended      : {_DIM}{block.get('ended_at', '')}{_RESET}\n")

    if output:
        print(f"  {_DIM}--- Output ---{_RESET}")
        lines = output.splitlines()[:40]
        for line in lines:
            print(f"  {line}")
        if len(output.splitlines()) > 40:
            print(f"  {_DIM}... ({len(output.splitlines()) - 40} more lines){_RESET}")
    else:
        print(f"  {_DIM}(no output){_RESET}")

    print()
    _wait_for_key()
    return ""


def _run_status_overlay(run_id: str) -> str:
    """Display agent run status."""
    _clear_screen()
    print(f"\n{_BANNER} Agent Run Status\n")

    run = _fetch_run(run_id)
    if run is None:
        print(f"  {_ERR}Run not found: {run_id}{_RESET}\n")
        _wait_for_key()
        return ""

    state = run.get("state", "unknown")
    state_colors = {
        "completed": _OK,
        "failed": _ERR,
        "cancelled": _WARN,
        "in_progress": "\033[1;36m",
        "awaiting_input": _WARN,
        "created": _DIM,
    }
    sc = state_colors.get(state, _RESET)

    print(f"  Run ID  : {_DIM}{run_id[:32]}{_RESET}")
    print(f"  State   : {sc}{state}{_RESET}")
    print(f"  Session : {_DIM}{run.get('session_id', '')[:16]}{_RESET}")
    print(f"  Prompt  : {run.get('prompt', '')[:80]}")
    print(f"  Created : {_DIM}{run.get('created_at', '')}{_RESET}")

    if run.get("result"):
        print(f"\n  {_DIM}--- Result ---{_RESET}")
        print(f"  {run['result'][:200]}")

    if run.get("error"):
        print(f"\n  {_ERR}Error: {run['error']}{_RESET}")

    print()
    _wait_for_key()
    return ""


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------


def _get_socket_path() -> str:
    blink_dir = os.environ.get("BLINK_DIR", os.path.expanduser("~/.blink"))
    return os.path.join(blink_dir, "blink.sock")


def _send_ipc(cmd: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Send one IPC command to the Blink daemon."""
    import socket

    sock_path = _get_socket_path()
    if not os.path.exists(sock_path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect(sock_path)
            s.sendall((json.dumps({"cmd": cmd, "params": params}) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.split(b"\n")[0])
    except Exception:  # noqa: BLE001
        return None


def _fetch_block(block_id: str) -> dict[str, Any] | None:
    resp = _send_ipc("get_block", {"block_id": block_id})
    if resp and resp.get("ok"):
        return resp.get("data")
    return None


def _fetch_run(run_id: str) -> dict[str, Any] | None:
    resp = _send_ipc("get_run", {"run_id": run_id})
    if resp and resp.get("ok"):
        return resp.get("data")
    return None


def _notify_daemon_approval(run_id: str, approved: bool) -> None:
    _send_ipc("resolve_pending", {"run_id": run_id, "approved": approved})


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def _clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _read_single_char(prompt: str = "") -> str:
    """Read a single character from stdin (no echo, no Enter required)."""
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        return ch
    except (ImportError, AttributeError):
        # Fallback for non-Unix or non-tty environments
        return input().strip()


def _wait_for_key() -> None:
    print(f"  {_DIM}Press any key to close...{_RESET}", end="", flush=True)
    _read_single_char()


def _print_error(msg: str) -> None:
    print(f"\n{_ERR}Error: {msg}{_RESET}\n", file=sys.stderr)


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def _parse_args(
    args: list[str],
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Parse kitten args into (mode, run_id, block_id, action, title)."""
    mode = "approve"
    run_id = None
    block_id = None
    action = None
    title = None

    i = 1  # skip program name
    while i < len(args):
        arg = args[i]
        if arg == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
            i += 1
        elif arg == "--run-id" and i + 1 < len(args):
            run_id = args[i + 1]
            i += 2
        elif arg.startswith("--run-id="):
            run_id = arg.split("=", 1)[1]
            i += 1
        elif arg == "--block-id" and i + 1 < len(args):
            block_id = args[i + 1]
            i += 2
        elif arg.startswith("--block-id="):
            block_id = arg.split("=", 1)[1]
            i += 1
        elif arg == "--action" and i + 1 < len(args):
            action = args[i + 1]
            i += 2
        elif arg.startswith("--action="):
            action = arg.split("=", 1)[1]
            i += 1
        elif arg == "--title" and i + 1 < len(args):
            title = args[i + 1]
            i += 2
        elif arg.startswith("--title="):
            title = arg.split("=", 1)[1]
            i += 1
        else:
            i += 1

    return mode, run_id, block_id, action, title


__all__ = ["main", "handle_result"]
