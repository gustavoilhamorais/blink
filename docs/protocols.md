# Blink Protocol Reference

## Overview

Blink uses three protocols for inter-component communication:

1. **Daemon IPC** — JSON over a Unix domain socket (`~/.blink/blink.sock`)
2. **MCP (Model Context Protocol)** — JSON-RPC 2.0 for AI tool/resource access
3. **ACP (Agent Communication Protocol)** — HTTP-based agent run management

---

## 1. Daemon IPC Protocol

### Message Format

Requests and responses are newline-terminated JSON objects.

**Request:**
```json
{"cmd": "<command>", "params": {…}}
```

**Response:**
```json
{"ok": true, "data": {…}}
{"ok": false, "error": "Human-readable error message"}
```

### Commands

#### `ping`
Check daemon liveness.

```json
→ {"cmd": "ping", "params": {}}
← {"ok": true, "data": "pong"}
```

#### `get_recent_blocks`
Fetch recent command blocks for a session.

```json
→ {"cmd": "get_recent_blocks", "params": {"session_id": "…", "limit": 20}}
← {"ok": true, "data": [
    {"id": "…", "command": "ls -la", "exit_code": 0, "cwd": "/home/…",
     "started_at": "2026-04-14T10:00:00Z", "ended_at": "2026-04-14T10:00:01Z"}
  ]}
```

#### `get_block`
Fetch a single block by ID.

```json
→ {"cmd": "get_block", "params": {"block_id": "…"}}
← {"ok": true, "data": {"id": "…", "command": "…", "output": "…", "exit_code": 0, …}}
```

#### `list_sessions`
List all known sessions.

```json
→ {"cmd": "list_sessions", "params": {}}
← {"ok": true, "data": [
    {"id": "…", "kitty_window_id": 3, "cwd": "/home/…",
     "created_at": "…", "last_active": "…"}
  ]}
```

#### `get_session`
Fetch a single session.

```json
→ {"cmd": "get_session", "params": {"session_id": "…"}}
← {"ok": true, "data": {"id": "…", "kitty_window_id": 3, "cwd": "…"}}
```

#### `block_start`
Record the start of a command block (called by shell preexec).

```json
→ {"cmd": "block_start", "params": {
    "session_id": "…", "command": "git status", "cwd": "/repo"}}
← {"ok": true, "data": {"block_id": "…"}}
```

#### `block_end`
Record the end of a command block (called by shell precmd).

```json
→ {"cmd": "block_end", "params": {
    "block_id": "…", "exit_code": 0, "output": "…"}}
← {"ok": true, "data": null}
```

#### `resolve_pending`
Approve or deny a pending agent action.

```json
→ {"cmd": "resolve_pending", "params": {"run_id": "…", "approved": true}}
← {"ok": true, "data": {"state": "in_progress"}}
```

#### `get_run`
Fetch an agent run by ID.

```json
→ {"cmd": "get_run", "params": {"run_id": "…"}}
← {"ok": true, "data": {"id": "…", "state": "completed", "result": "…"}}
```

---

## 2. MCP Tool Reference

The MCP server is started with: `blink mcp serve`

Default transport: **stdio** (JSON-RPC 2.0, newline-delimited).

### Initialization

```json
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{
    "protocolVersion":"2024-11-05",
    "clientInfo":{"name":"my-agent","version":"1.0"}}}
← {"jsonrpc":"2.0","id":1,"result":{
    "protocolVersion":"2024-11-05",
    "capabilities":{"tools":{"listChanged":false},"resources":{"subscribe":false}},
    "serverInfo":{"name":"blink-mcp","version":"0.1.0"},
    "instructions":"This MCP server exposes Blink terminal capabilities…"}}
```

### Tools

#### OBSERVE Tools (read-only, always allowed)

##### `get_active_session`
Returns the most recently active session.
```json
→ {"method":"tools/call","params":{"name":"get_active_session","arguments":{}}}
← {"result":{"content":[{"type":"text","text":"{\"id\":\"…\",\"cwd\":\"/home/…\"}"}]}}
```

##### `list_sessions`
Returns all sessions.

##### `list_blocks`
```json
arguments: {"session_id": "…", "limit": 20}
```

##### `get_block`
```json
arguments: {"block_id": "…"}
```

##### `get_visible_screen`
```json
arguments: {"window_id": 3}   // optional, defaults to focused window
```

##### `get_selection`
```json
arguments: {"window_id": 3}   // optional
```

##### `get_prompt_buffer`
```json
arguments: {"session_id": "…"}
```

#### SUGGEST Tools (prompt buffer mutation)

##### `replace_prompt_buffer`
```json
arguments: {"session_id": "…", "text": "git commit -m 'fix: …'"}
```

##### `insert_at_cursor`
```json
arguments: {"session_id": "…", "text": "--verbose"}
```

##### `accept_completion`
```json
arguments: {"session_id": "…", "text": "kubectl get pods"}
```

#### ACT Tools (execute code — requires confirmation by default)

##### `run_command`
```json
arguments: {"session_id": "…", "cmd": "ls -la", "cwd": "/tmp"}
```

##### `write_stdin`
```json
arguments: {"session_id": "…", "data": "yes\n"}
```

##### `send_signal`
```json
arguments: {"session_id": "…", "signal": "SIGINT"}
// signal: SIGINT | SIGTERM | SIGKILL | SIGSTOP | SIGCONT
```

##### `cancel_command`
```json
arguments: {"session_id": "…"}
```

### Resources

Resources are read with:
```json
{"method":"resources/read","params":{"uri":"terminal://session/<id>/screen"}}
```

| URI Pattern | MIME | Description |
|-------------|------|-------------|
| `terminal://session/<id>/screen` | text/plain | Current visible screen content |
| `terminal://session/<id>/prompt` | text/plain | Active command-line buffer |
| `terminal://session/<id>/blocks/<block_id>` | application/json | A specific block |
| `terminal://session/<id>/history/recent` | application/json | Recent 50 commands |
| `terminal://session/<id>/env/redacted` | application/json | Environment (secrets redacted) |
| `terminal://session/<id>/cwd` | text/plain | Current working directory |
| `terminal://session/<id>/git/status` | text/plain | `git status --short` output |
| `terminal://session/<id>/policy` | application/json | Active security policy |

### Capability Levels

| Level | Value | Tools Allowed |
|-------|-------|--------------|
| OBSERVE | `observe` | All read-only tools |
| SUGGEST | `suggest` | Read + prompt buffer tools |
| ACT | `act` | All tools (with confirmation) |
| ADMIN | `admin` | All tools (no confirmation) |

Pass `--capability act` to `blink mcp serve` to enable ACT-level access.

### Audit Log Format

Every tool call is recorded in SQLite (`~/.blink/blink.db`, table `audit_log`):

```sql
CREATE TABLE audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,   -- ISO 8601 UTC
    event     TEXT NOT NULL,   -- tool_call_success | tool_call_denied | tool_call_error
    tool      TEXT NOT NULL,   -- tool name
    arguments TEXT,            -- JSON (secrets redacted)
    result    TEXT,            -- truncated to 4096 chars
    allowed   INTEGER NOT NULL -- 1 = allowed, 0 = denied
);
```

---

## 3. ACP Integration Guide

Blink's ACP Gateway provides a lightweight implementation of the
[Agent Communication Protocol](https://agentcommunicationprotocol.dev/).

### Creating a Run

```python
import asyncio
from blink.acp.gateway import ACPGateway, RunMode

gateway = ...  # built via CLI or programmatically

run = await gateway.create_run(
    prompt="Explain the last error in my terminal",
    session_id="blink-session-id",
    mode=RunMode.STREAM,
)
```

### Streaming Events

```python
async for event in await gateway.stream_run(run):
    if event.type == "text":
        print(event.data, end="")
    elif event.type == "tool_call":
        print(f"[tool] {event.data['tool']}")
    elif event.type == "awaiting":
        # Agent needs user approval
        run_id = event.data["run_id"]
        await run_manager.resolve_pending(run_id, approved=True)
    elif event.type == "completed":
        break
    elif event.type == "error":
        print(f"Error: {event.data['message']}")
```

### Event Types

| Type | Data | Description |
|------|------|-------------|
| `text` | `str` | Streamed text fragment |
| `tool_call` | `{"tool": "…", "args": {…}}` | Tool being called |
| `awaiting` | `{"run_id": "…", "action": {…}}` | Waiting for user approval |
| `completed` | `{}` | Run finished successfully |
| `error` | `{"message": "…"}` | Run failed |
| `cancelled` | `{}` | Run was cancelled |

### Run States

```
created → in_progress → completed
                     → failed
                     → cancelled
                     → awaiting_input → in_progress (after approval)
                                     → cancelled (after denial)
```

### Wire Protocol Examples

#### Start a run (programmatic API)

```python
run = await gateway.create_run(prompt="…", session_id="…")
# run.id: "f47ac10b-…"
# run.state: "created"
```

#### Stream a run

```python
async for event in await gateway.stream_run(run):
    # event.type: "text" | "tool_call" | "awaiting" | "completed" | "error"
    # event.data: varies by type
    pass
```

#### Approve a pending action

```python
await run_manager.resolve_pending(run_id, approved=True)
```

#### Cancel a run

```python
await run_manager.cancel(run_id)
```
