"""Structured output viewer kitten.

Displays JSON, tables, code blocks, and other structured artifacts
in a scrollable, formatted view.

Invoked by Kitty as: kitty +kitten blink_artifact_viewer [options] [file]

Options:
    --block-id <id>    Fetch and display a block's output from the daemon.
    --run-id <id>      Fetch and display an agent run's result.
    --format <fmt>     Force format: auto | json | table | text | code
    --title <title>    Override the display title.
    [file]             Read artifact from a file path (use - for stdin).
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

    Fetches or reads the artifact, detects its type, and renders a paginated
    formatted view.  Returns an empty string (no result forwarding needed).
    """
    opts = _parse_args(args)

    title = opts.get("title", "Artifact Viewer")
    fmt = opts.get("format", "auto")

    # Load content
    content: str | None = None

    if opts.get("block_id"):
        title = title if opts.get("title") else f"Block: {opts['block_id'][:12]}"
        content = _fetch_block_output(opts["block_id"])
    elif opts.get("run_id"):
        title = title if opts.get("title") else f"Run: {opts['run_id'][:12]}"
        content = _fetch_run_result(opts["run_id"])
    elif opts.get("file"):
        path = opts["file"]
        title = title if opts.get("title") else path
        if path == "-":
            content = sys.stdin.read()
        else:
            try:
                with open(path) as f:
                    content = f.read()
            except OSError as exc:
                _show_error(f"Cannot read file: {exc}")
                return ""
    else:
        _show_error("No input specified. Use --block-id, --run-id, or provide a file path.")
        return ""

    if content is None:
        _show_error("No content to display.")
        return ""

    # Detect format and render
    if fmt == "auto":
        fmt = _detect_format(content)

    _render_artifact(title, content, fmt)
    return ""


def handle_result(
    args: list[str],
    answer: str,
    target_window_id: int,
    boss: Any,
) -> None:
    """No result handling needed for a pure viewer kitten."""


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def _detect_format(content: str) -> str:
    """Heuristically detect the artifact format."""
    stripped = content.strip()

    if not stripped:
        return "text"

    # Try JSON
    if stripped.startswith(("{", "[", '"')):
        try:
            json.loads(stripped)
            return "json"
        except ValueError:
            pass

    # Markdown code fences
    if "```" in stripped:
        return "code"

    # Table heuristic: multiple lines with consistent column separators
    lines = stripped.splitlines()
    if len(lines) >= 2:
        pipe_counts = [line.count("|") for line in lines[:5]]
        if all(c >= 2 for c in pipe_counts):
            return "table"

    return "text"


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_OK = "\033[1;32m"
_WARN = "\033[1;33m"
_CYAN = "\033[1;36m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_BLUE = "\033[1;34m"
_MAGENTA = "\033[1;35m"
_RESET = "\033[0m"


def _render_artifact(title: str, content: str, fmt: str) -> None:
    """Render the artifact with paging support."""
    lines = _format_content(content, fmt)
    _paginate(title, lines)


def _format_content(content: str, fmt: str) -> list[str]:
    """Convert raw content into a list of display lines."""
    if fmt == "json":
        return _format_json(content)
    elif fmt == "table":
        return _format_table(content)
    elif fmt == "code":
        return _format_code(content)
    else:
        return content.splitlines()


def _format_json(content: str) -> list[str]:
    """Pretty-print JSON with syntax colouring."""
    try:
        data = json.loads(content.strip())
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
    except ValueError:
        return [f"{_WARN}(invalid JSON){_RESET}", ""] + content.splitlines()

    lines: list[str] = []
    for line in pretty.splitlines():
        coloured = _colorize_json_line(line)
        lines.append(coloured)
    return lines


def _colorize_json_line(line: str) -> str:
    """Apply minimal ANSI colours to a JSON line."""
    stripped = line.lstrip()

    # String keys: "key":
    if stripped.startswith('"') and ":" in stripped:
        colon_pos = stripped.index(":")
        key_part = stripped[:colon_pos + 1]
        val_part = stripped[colon_pos + 1:]
        indent = " " * (len(line) - len(stripped))
        return f"{indent}{_CYAN}{key_part}{_RESET}{_format_json_value(val_part)}"

    # Bare values (arrays / numbers / booleans)
    return _format_json_value(line)


def _format_json_value(val: str) -> str:
    """Colour a JSON value fragment."""
    stripped = val.strip()
    if stripped.startswith('"'):
        return val.replace(stripped, f"{_OK}{stripped}{_RESET}", 1)
    elif stripped in ("true", "false"):
        return val.replace(stripped, f"{_MAGENTA}{stripped}{_RESET}", 1)
    elif stripped == "null":
        return val.replace(stripped, f"{_DIM}null{_RESET}", 1)
    elif stripped.lstrip("-").replace(".", "", 1).isdigit():
        return val.replace(stripped, f"{_BLUE}{stripped}{_RESET}", 1)
    return val


def _format_table(content: str) -> list[str]:
    """Render a pipe-separated Markdown-style table with aligned columns."""
    lines: list[str] = []
    rows: list[list[str]] = []

    for raw in content.splitlines():
        if not raw.strip():
            lines.append("")
            continue
        cells = [c.strip() for c in raw.strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return content.splitlines()

    # Compute column widths
    max_cols = max(len(r) for r in rows)
    col_widths = [0] * max_cols
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    for row_idx, row in enumerate(rows):
        # Separator row (---)
        if all(set(c).issubset({"-", ":", " "}) for c in row if c):
            sep = "─" * (sum(col_widths) + 3 * max_cols + 1)
            lines.append(f"  {_DIM}{sep}{_RESET}")
            continue

        parts: list[str] = []
        for i in range(max_cols):
            cell = row[i] if i < len(row) else ""
            width = col_widths[i]
            if row_idx == 0:
                # Header row
                parts.append(f"{_BOLD}{_CYAN}{cell:<{width}}{_RESET}")
            else:
                parts.append(f"{cell:<{width}}")

        lines.append("  " + f"  {_DIM}|{_RESET}  ".join(parts))

    return lines


def _format_code(content: str) -> list[str]:
    """Display code blocks with fence markers styled."""
    lines: list[str] = []
    in_fence = False
    lang = ""

    for line in content.splitlines():
        if line.startswith("```"):
            if not in_fence:
                in_fence = True
                lang = line[3:].strip()
                lang_hint = f" {_DIM}({lang}){_RESET}" if lang else ""
                lines.append(f"  {_DIM}┌─ code{lang_hint} {'─' * 40}{_RESET}")
            else:
                in_fence = False
                lines.append(f"  {_DIM}└{'─' * 46}{_RESET}")
        elif in_fence:
            lines.append(f"  {_CYAN}│{_RESET} {line}")
        else:
            lines.append(line)

    return lines


# ---------------------------------------------------------------------------
# Paginator
# ---------------------------------------------------------------------------

_TERMINAL_ROWS = 40


def _paginate(title: str, lines: list[str]) -> None:
    """Simple pager with keyboard navigation."""
    page_size = _get_terminal_rows() - 5  # reserve space for header/footer
    offset = 0
    total = len(lines)

    while True:
        _clear_screen()
        _render_page(title, lines, offset, page_size, total)

        if total <= page_size:
            # Content fits on one page — just wait for keypress
            _wait_for_key()
            return

        key = _read_key()
        if key in ("q", "\x1b"):
            return
        elif key in ("j", "\x1b[B", " "):  # down / space
            offset = min(offset + page_size, max(0, total - page_size))
        elif key in ("k", "\x1b[A"):  # up
            offset = max(0, offset - page_size)
        elif key in ("g",):  # top
            offset = 0
        elif key in ("G",):  # bottom
            offset = max(0, total - page_size)
        elif key in ("\r", "\n"):
            offset = min(offset + 1, max(0, total - page_size))


def _render_page(title: str, lines: list[str], offset: int, page_size: int, total: int) -> None:
    """Render a single page of content."""
    end = min(offset + page_size, total)
    pct = int(100 * end / total) if total > 0 else 100

    print(f"\n  {_BOLD}{_CYAN}{title}{_RESET}  {_DIM}(lines {offset + 1}–{end} of {total}  {pct}%){_RESET}\n")
    for line in lines[offset:end]:
        print(f"  {line}")

    nav = "[q] quit  [space/j] down  [k] up  [g] top  [G] bottom"
    print(f"\n  {_DIM}{nav}{_RESET}", end="", flush=True)


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


def _fetch_block_output(block_id: str) -> str | None:
    resp = _send_ipc("get_block", {"block_id": block_id})
    if resp and resp.get("ok"):
        data = resp.get("data", {})
        if isinstance(data, dict):
            return data.get("output", "")
    return None


def _fetch_run_result(run_id: str) -> str | None:
    resp = _send_ipc("get_run", {"run_id": run_id})
    if resp and resp.get("ok"):
        data = resp.get("data", {})
        if isinstance(data, dict):
            return data.get("result", "")
    return None


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def _clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _get_terminal_rows() -> int:
    try:
        import shutil

        return shutil.get_terminal_size().lines
    except Exception:  # noqa: BLE001
        return _TERMINAL_ROWS


def _read_key() -> str:
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    return "\x1b[" + ch3
                return ch
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, AttributeError):
        return input()[:1] or ""


def _wait_for_key() -> None:
    print(f"\n  {_DIM}Press any key to close...{_RESET}", end="", flush=True)
    _read_key()


def _show_error(msg: str) -> None:
    _clear_screen()
    print(f"\n  \033[1;31mError:\033[0m {msg}\n")
    _wait_for_key()


# ---------------------------------------------------------------------------
# Arg parser
# ---------------------------------------------------------------------------


def _parse_args(args: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    i = 1
    while i < len(args):
        arg = args[i]
        for flag, key in (
            ("--block-id", "block_id"),
            ("--run-id", "run_id"),
            ("--format", "format"),
            ("--title", "title"),
        ):
            if arg == flag and i + 1 < len(args):
                result[key] = args[i + 1]
                i += 2
                break
            elif arg.startswith(f"{flag}="):
                result[key] = arg.split("=", 1)[1]
                i += 1
                break
        else:
            # Positional argument = file path
            if not arg.startswith("--"):
                result["file"] = arg
            i += 1
    return result


__all__ = ["main", "handle_result"]
