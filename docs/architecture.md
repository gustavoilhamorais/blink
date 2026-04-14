# Blink Architecture

## System Overview

Blink is an open-source Warp.dev alternative built on the [Kitty terminal](https://sw.kovidgoyal.net/kitty/).
It adds AI-powered autocompletions, command block tracking, an inline agent,
and an MCP server — all while remaining a thin layer on top of existing shell
tooling.

```
┌───────────────────────────────────────────────────────────────┐
│                        User Interface                         │
│                                                               │
│   ┌─────────────────────┐    ┌────────────────────────────┐   │
│   │   Kitty Terminal    │    │     Blink CLI (blink …)    │   │
│   │  + shell integration│    │  daemon / block / agent /  │   │
│   │  (bash/zsh/fish)    │    │  history / config / mcp    │   │
│   └────────┬────────────┘    └──────────────┬─────────────┘   │
│            │ BLINK_SESSION_ID                │ IPC (Unix sock) │
└────────────┼─────────────────────────────────┼────────────────┘
             │                                 │
             ▼                                 ▼
┌────────────────────────────────────────────────────────────────┐
│                      Blink Daemon                              │
│                   (src/blink/daemon/)                          │
│                                                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ Session Mgr  │  │  Block Store │  │   IPC Request Loop   │ │
│  │  (sessions)  │  │  (blocks DB) │  │  (Unix domain socket)│ │
│  └──────────────┘  └──────────────┘  └──────────────────────┘ │
│                                                                │
│                  SQLite via aiosqlite                          │
│           ~/.blink/blink.db  (~/.blink/blink.log)             │
└───────────────────────┬────────────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        │               │               │
        ▼               ▼               ▼
┌──────────────┐ ┌───────────────┐ ┌───────────────────────────┐
│  MCP Server  │ │  ACP Gateway  │ │   Completions Broker      │
│ (mcp/server) │ │ (acp/gateway) │ │ (completions/broker)      │
│              │ │               │ │                           │
│ JSON-RPC 2.0 │ │ Agent runs    │ │ ┌──────┐ ┌──────┐        │
│ stdio / HTTP │ │ Run manager   │ │ │Anthr.│ │OpenAI│ …      │
│              │ │ Session mgr   │ │ └──────┘ └──────┘        │
└──────┬───────┘ └───────┬───────┘ └───────────────────────────┘
       │                 │
       │         ┌───────┴───────┐
       │         │  AI Providers │
       │         │ (providers/)  │
       │         │ anthropic /   │
       │         │ openai /      │
       │         │ ollama        │
       │         └───────────────┘
       │
       ▼
┌──────────────────────┐
│  Security Layer      │
│ (security/)          │
│                      │
│ • CapabilityChecker  │
│ • RateLimiter        │
│ • Redaction          │
│ • Sanitize           │
│ • CredentialStore    │
│ • AuditLogger        │
└──────────────────────┘
```

## Component Responsibilities

### CLI (`src/blink/cli/`)

The `blink` command entry point.  Sub-commands:

| Sub-command | Responsibility |
|-------------|---------------|
| `daemon start/stop/status/logs` | Lifecycle management of the background daemon |
| `block list/show/explain/retry` | Inspect and re-run command blocks |
| `history search/stats/export` | Query shell command history |
| `agent ask/cancel/status/list/approve/deny` | Inline agent interactions |
| `provider list/add/remove/test` | AI provider configuration |
| `config show/set/get/reset` | User configuration |
| `mcp serve` | Launch MCP server for external AI tools |

### Daemon (`src/blink/daemon/`)

A long-lived background process that:

- Manages a Unix domain socket at `~/.blink/blink.sock`
- Stores sessions, blocks, and history in SQLite
- Responds to IPC commands from the CLI and shell integration
- Tracks the Kitty window ID for each session

**IPC protocol**: JSON-RPC-lite over a Unix socket.  Each message is a
newline-terminated JSON object: `{"cmd": "…", "params": {…}}`.
Responses: `{"ok": true, "data": …}` or `{"ok": false, "error": "…"}`.

### MCP Server (`src/blink/mcp/`)

Implements the [Model Context Protocol](https://modelcontextprotocol.io/)
over stdio (and optionally HTTP/SSE).  Exposes:

- **Tools** — read terminal state, modify prompt buffer, run commands
- **Resources** — screen content, history, environment, git status, policy

All tool calls are security-checked (capability model) and audit-logged.

### ACP Gateway (`src/blink/acp/`)

Implements a lightweight subset of the
[Agent Communication Protocol](https://agentcommunicationprotocol.dev/)
for inline agent interactions:

- **`ACPGateway`** — creates and streams agent runs
- **`RunManager`** — persists run state to SQLite, handles approval flow
- **`SessionManager`** — maps ACP sessions to Blink sessions

### Completions (`src/blink/completions/`)

The completion pipeline:

1. **`ContextBuilder`** — assembles the context object (history, CWD, blocks)
2. **`CompletionBroker`** — dispatches to the active AI provider
3. **`CompletionRanker`** — scores and deduplicates candidate completions
4. **`CompletionValidator`** — validates suggestions against shell syntax

### Kitty Integration (`src/blink/kitty/`)

- **`KittyRCClient`** — wraps `kitty @` remote-control commands
- **`BlockTracker`** — uses Kitty OSC codes to detect prompt/command/output boundaries

### Security (`src/blink/security/`)

- **`CapabilityChecker`** — enforces the OBSERVE/SUGGEST/ACT/ADMIN hierarchy
- **`RateLimiter`** — sliding-window rate limiting per (tool, session)
- **`redact_secrets` / `redact_env`** — regex-based secret scrubbing
- **`sanitize_command` / `sanitize_path`** — input sanitization
- **`CredentialStore`** — system keyring with file fallback
- **`AuditLogger`** — append-only SQLite audit log
- **`audit_tools`** — static security audit of tool registrations

### Storage (`src/blink/storage/`)

Async SQLite wrapper.  Tables:

| Table | Contents |
|-------|----------|
| `sessions` | Kitty session ↔ Blink session mapping |
| `blocks` | Captured command blocks (prompt, output, exit code, timing) |
| `history` | Shell command history (subset of blocks, optimised for search) |
| `audit_log` | Immutable record of every MCP tool call |

### Kittens (`kittens/`)

Python modules loaded by Kitty's `+kitten` mechanism:

| Kitten | Purpose |
|--------|---------|
| `agent_overlay` | Approval prompts, block inspector, run status overlay |
| `onboarding` | First-run wizard (provider setup, shell integration, tour) |
| `session_picker` | Interactive session switcher |
| `artifact_viewer` | Scrollable JSON/table/code viewer |

## Data Flow

### Shell Command Execution

```
User types command → Shell preexec hook fires
    → Sends "block_start" IPC to daemon
        → Daemon creates Block record (started_at)
Command runs → Shell precmd hook fires
    → Sends "block_end" IPC (command, exit_code, output)
        → Daemon updates Block record (ended_at, output)
            → Block is now queryable via MCP / CLI
```

### Agent Interaction

```
blink agent ask "help me fix this"
    → CLI builds ACPGateway
        → Creates Run (state: created)
            → Streams events:
                text → print to stdout
                tool_call → execute via MCPServer
                    → CapabilityChecker validates
                    → Tool runs
                    → AuditLogger records
                awaiting_input → prompt user for approval
                    → RunManager resolves_pending
                completed → done
```

### MCP Tool Call

```
External AI → JSON-RPC tools/call → MCPServer._handle_message
    → MCPServer._dispatch → handle_tools_call
        → CapabilityChecker.check(tool, args)
            → blocked pattern check
            → capability level check
            → confirmation prompt (ACT-level)
        → ToolExecutor.execute(tool, args)
        → AuditLogger.log(event, tool, args, result, allowed)
        → Return MCP content response
```

## Key Design Decisions

1. **SQLite over a network database** — Blink targets single-user local
   deployments.  SQLite gives zero-dependency persistence, atomic writes,
   and trivial backup (just copy the file).

2. **Unix domain socket IPC** — faster than HTTP for local communication,
   avoids port conflicts, and has straightforward file-system permissions.

3. **Capability tiers instead of ACLs** — a four-level hierarchy
   (OBSERVE → SUGGEST → ACT → ADMIN) is simple enough for users to
   understand and reason about without per-tool configuration.

4. **Kitten-based UI** — Kitty kittens run in-process inside Kitty, sharing
   its terminal multiplexing and rendering.  No separate GUI toolkit needed.

5. **Provider abstraction** — all AI calls go through `BaseProvider`.
   Swapping providers (or adding new ones) requires only a new subclass.

6. **Graceful degradation** — every component that depends on Kitty or an
   AI provider checks availability at runtime and degrades gracefully.
   Blink works as a pure CLI tool even without Kitty or any API key.
