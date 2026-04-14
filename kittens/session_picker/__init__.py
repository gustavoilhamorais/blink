"""Session picker kitten.

Lists all active Blink sessions, allows switching/focusing.
Invoked by Kitty as: kitty +kitten blink_session_picker

The user selects a session from the list.  Kitty then focuses the
corresponding window via the Kitty remote-control API.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Kitty kitten API
# ---------------------------------------------------------------------------


def main(args: list[str]) -> str:
    """Entry point called by Kitty.

    Fetches all sessions from the Blink daemon, displays an interactive
    selection menu, and returns the selected session ID (or empty string).
    """
    sessions = _fetch_sessions()

    if not sessions:
        _print_info("No active Blink sessions found.")
        print(f"\n  {_DIM}Start a session by opening a new Kitty window and running a command.{_RESET}\n")
        _wait_for_key()
        return ""

    _clear_screen()
    selected_id = _render_session_menu(sessions)
    return selected_id


def handle_result(
    args: list[str],
    answer: str,
    target_window_id: int,
    boss: Any,
) -> None:
    """Handle the result of session selection.

    Focuses the Kitty window associated with the selected session.

    Args:
        args: Original kitten args.
        answer: Session ID returned by :func:`main`.
        target_window_id: The window that launched the kitten.
        boss: Kitty boss object — used to focus windows.
    """
    if not answer:
        return

    session_id = answer.strip()
    window_id = _get_window_id_for_session(session_id)

    if window_id is None:
        return

    # Focus the target window using Kitty's boss API
    try:
        if hasattr(boss, "call_remote_control"):
            boss.call_remote_control(
                None,
                ("focus-window", f"--match=id:{window_id}"),
            )
        else:
            # Fallback: use kitty @ socket command
            _focus_window_via_rc(window_id)
    except Exception:  # noqa: BLE001
        _focus_window_via_rc(window_id)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

_OK = "\033[1;32m"
_WARN = "\033[1;33m"
_CYAN = "\033[1;36m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_SELECTED = "\033[7m"  # reverse video


def _render_session_menu(sessions: list[dict[str, Any]]) -> str:
    """Interactive arrow-key session picker. Returns selected session ID."""
    current_idx = 0
    total = len(sessions)

    while True:
        _clear_screen()
        print(f"\n  {_BOLD}{_CYAN}Blink Session Picker{_RESET}\n")
        print(f"  {_DIM}Use arrow keys or j/k to move, Enter to select, q/Esc to cancel.{_RESET}\n")

        for i, session in enumerate(sessions):
            sid = session.get("id", "")
            cwd = session.get("cwd", "~")
            last_active = (session.get("last_active") or "")[:16]
            wid = session.get("kitty_window_id", "?")
            cmd_count = session.get("cmd_count", "?")

            prefix = f"  {_SELECTED}" if i == current_idx else "  "
            suffix = _RESET if i == current_idx else ""

            sid_short = sid[:8] + "..." if len(sid) > 8 else sid
            print(
                f"{prefix}  [{i + 1:2d}] "
                f"{_CYAN}{sid_short}{_RESET}  "
                f"{_DIM}win:{wid}{_RESET}  "
                f"{cwd:<40}  "
                f"{_DIM}{last_active}  {cmd_count} cmds{_RESET}"
                f"{suffix}"
            )

        print()
        key = _read_key()

        if key in ("q", "\x1b"):  # q or Escape
            return ""
        elif key in ("\r", "\n", " "):  # Enter or Space
            return sessions[current_idx].get("id", "")
        elif key in ("j", "\x1b[B"):  # j or down arrow
            current_idx = (current_idx + 1) % total
        elif key in ("k", "\x1b[A"):  # k or up arrow
            current_idx = (current_idx - 1) % total
        elif key.isdigit() and 1 <= int(key) <= total:
            return sessions[int(key) - 1].get("id", "")


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------


def _get_socket_path() -> str:
    blink_dir = os.environ.get("BLINK_DIR", os.path.expanduser("~/.blink"))
    return os.path.join(blink_dir, "blink.sock")


def _send_ipc(cmd: str, params: dict[str, Any]) -> dict[str, Any] | None:
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


def _fetch_sessions() -> list[dict[str, Any]]:
    """Fetch session list from daemon, annotated with command counts."""
    resp = _send_ipc("list_sessions", {})
    if resp and resp.get("ok"):
        return resp.get("data", [])
    return []


def _get_window_id_for_session(session_id: str) -> int | None:
    """Fetch the Kitty window ID for a given session."""
    resp = _send_ipc("get_session", {"session_id": session_id})
    if resp and resp.get("ok"):
        data = resp.get("data", {})
        wid = data.get("kitty_window_id")
        return int(wid) if wid is not None else None
    return None


def _focus_window_via_rc(window_id: int) -> None:
    """Focus a Kitty window using the kitty @ command-line RC interface."""
    import subprocess

    try:
        subprocess.run(  # noqa: S603 S607
            ["kitty", "@", "focus-window", f"--match=id:{window_id}"],
            timeout=3,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def _clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _read_key() -> str:
    """Read a single key press, handling escape sequences for arrow keys."""
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            # Check for escape sequence (arrow keys start with \x1b[)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    return "\x1b[" + ch3  # e.g. \x1b[A = up arrow
                return ch  # plain Escape
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, AttributeError):
        return input()[:1] or ""


def _wait_for_key() -> None:
    print(f"  {_DIM}Press any key to close...{_RESET}", end="", flush=True)
    _read_key()


def _print_info(msg: str) -> None:
    _clear_screen()
    print(f"\n  {_DIM}{msg}{_RESET}\n")


__all__ = ["main", "handle_result"]
