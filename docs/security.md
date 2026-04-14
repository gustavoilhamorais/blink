# Blink Security Guide

## Capability Model

Blink uses a four-tier capability model to control what AI agents can do.
The tiers are ordered: **OBSERVE < SUGGEST < ACT < ADMIN**.

| Capability | CLI Flag | What It Allows |
|------------|----------|---------------|
| `observe` | `--capability observe` | Read terminal state only (default) |
| `suggest` | `--capability suggest` | Read + modify the prompt buffer |
| `act` | `--capability act` | Read + write + run commands (with confirmation) |
| `admin` | `--capability admin` | Full unrestricted access — **dangerous** |

### Default Behavior

When you run `blink mcp serve`, the server starts in **observe** mode.
No commands are ever run without explicit escalation.

To allow an agent to suggest completions:
```bash
blink mcp serve --capability suggest
```

To allow an agent to run commands (with confirmation):
```bash
blink mcp serve --capability act
```

### Confirmation Prompts

In `act` mode, every `run_command`, `write_stdin`, `send_signal`, and
`cancel_command` call will pause and show a confirmation prompt:

```
[blink-mcp] Tool 'run_command' wants to execute: 'rm -rf /tmp/test'
Allow? [y/N]
```

Non-interactive sessions (no TTY) default to **deny**.

### Auto-Approve Patterns

You can whitelist specific commands that should run without confirmation:

```python
from blink.security.capabilities import SecurityPolicy, Capability

policy = SecurityPolicy(
    granted_capability=Capability.ACT,
    require_confirmation=True,
    auto_approve=["git status", "git diff", "ls *", "cat *"],
)
```

Use specific patterns — avoid wildcards like `*` or `.*` that would approve
everything.

### Blocked Patterns

Commands matching blocked patterns are always denied, even in `admin` mode:

```python
policy = SecurityPolicy(
    granted_capability=Capability.ACT,
    blocked_patterns=["rm -rf /*", ":(){ :|:& };:", "dd if=/dev/zero of=/dev/sda"],
)
```

---

## Secret Handling

### What Gets Redacted

The `redact_secrets` function automatically scrubs the following from strings
before they are stored in the audit log or returned through MCP resources:

| Pattern | Example |
|---------|---------|
| Bearer/Basic/Token headers | `Authorization: Bearer eyJ…` |
| Key/secret assignments | `API_KEY=sk-abc123…` |
| OpenAI API keys | `sk-abcdefg…` |
| GitHub tokens | `ghp_abc123…`, `ghs_…`, `github_pat_…` |
| AWS access key IDs | `AKIA0123456789ABCDEF` |
| Slack tokens | `xoxb-…`, `xoxp-…` |
| Private key blocks | `-----BEGIN PRIVATE KEY-----` |
| JWT tokens | `eyJhbGc…` (three-segment base64url) |
| Stripe secret keys | `sk_live_…`, `sk_test_…` |
| Database DSNs with passwords | `postgres://user:pass@host/db` |
| Long hex strings (≥ 32 chars) | API tokens in hex encoding |
| Long base64 strings (≥ 40 chars) | Generic base64-encoded secrets |

### Environment Variables

`redact_env` redacts the **values** of any environment variable whose name
contains: `api_key`, `secret`, `password`, `passwd`, `token`, `credential`,
`auth`, `private_key`, `access_key`, `signing_key`, `encryption_key`,
`webhook_secret`, `database_url`, `db_pass`.

The variable names are preserved (so the agent knows what variables exist)
but the values are replaced with `[REDACTED]`.

### Best Practices for Users

1. **Never paste secrets into agent prompts.** The prompt is sent to your
   AI provider's API. Blink redacts common patterns but cannot guarantee
   100% coverage for all secret formats.

2. **Use environment variables** for API keys, not config files. Blink reads
   them at runtime and never stores them in plaintext.

3. **Store credentials via the keyring:**
   ```python
   from blink.security.keyring import CredentialStore
   store = CredentialStore()
   await store.store("providers", "openai_api_key", "sk-…")
   ```
   This uses your OS keyring (macOS Keychain, GNOME Keyring, KWallet).

4. **Review the audit log** periodically:
   ```sql
   SELECT timestamp, event, tool, arguments FROM audit_log
   WHERE event = 'tool_call_success' AND tool = 'run_command'
   ORDER BY timestamp DESC LIMIT 20;
   ```

5. **Start in `observe` mode** and only escalate to `act` when needed.

---

## Credential Storage

### System Keyring (Preferred)

Install the `keyring` Python package to enable system keyring integration:
```bash
pip install keyring
```

Blink will automatically use your system keyring to store and retrieve
provider API keys.  Keys are namespaced as `blink:<service>`.

### File Fallback

If the system keyring is unavailable, Blink falls back to
`~/.blink/credentials.json`.  Values are XOR-obfuscated with a
machine-specific key (hostname + UID).

**This is obfuscation, not encryption.** It prevents accidental exposure in
log tails and `ps` output but is not resistant to a determined attacker with
read access to your home directory.

Set `chmod 600 ~/.blink/credentials.json` to restrict access.

---

## Audit Log

Every MCP tool call is recorded in `~/.blink/blink.db`, table `audit_log`.

### Schema

```sql
CREATE TABLE audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,   -- ISO 8601 UTC
    event     TEXT NOT NULL,   -- see Event Types below
    tool      TEXT NOT NULL,   -- tool name
    arguments TEXT,            -- JSON (secrets redacted before storage)
    result    TEXT,            -- truncated to 4096 chars
    allowed   INTEGER NOT NULL -- 1 = allowed, 0 = denied
);
```

### Event Types

| Event | Meaning |
|-------|---------|
| `tool_call_success` | Tool ran successfully |
| `tool_call_denied` | Capability check or user rejected the call |
| `tool_call_error` | Tool ran but raised an exception |

### Querying the Audit Log

```bash
# Show all denied tool calls
sqlite3 ~/.blink/blink.db \
  "SELECT timestamp, tool, arguments FROM audit_log WHERE allowed=0 ORDER BY id DESC LIMIT 20"

# Show all run_command calls today
sqlite3 ~/.blink/blink.db \
  "SELECT timestamp, arguments FROM audit_log
   WHERE tool='run_command' AND date(timestamp) = date('now')
   ORDER BY id DESC"

# Count calls by tool
sqlite3 ~/.blink/blink.db \
  "SELECT tool, COUNT(*) as n FROM audit_log GROUP BY tool ORDER BY n DESC"
```

### Running the Security Audit

```bash
# Via Python (in a script or REPL)
import asyncio
from blink.security.audit import audit_report
from blink.mcp.server import MCPServer, run_server

# Inspect a running server's audit status
# (see docs/architecture.md for setup details)
```

The audit checks:
- ADMIN capability should never be the default granted level
- ACT tools should require confirmation
- All tools should have explicit capability mappings
- Tool schemas must be valid (`type: object`)
- Auto-approve patterns must not be catch-all wildcards

---

## Rate Limiting

MCP tool calls are rate-limited to prevent abuse:

```python
from blink.security.ratelimit import RateLimiter

# Default: 100 calls per 60-second window (per tool, per session)
limiter = RateLimiter(max_calls=100, window_seconds=60)

# With per-tool overrides
limiter = RateLimiter(
    max_calls=100,
    window_seconds=60,
    per_tool_limits={
        "run_command": 10,          # tighter limit for dangerous tools
        "get_visible_screen": 500,  # looser for cheap read tools
    },
)
```

A rate-limited call returns `False` from `limiter.check(tool, session_id)`.
The MCP server raises `PermissionError` which maps to a JSON-RPC `-32001`
(not authorised) error.

---

## Input Sanitization

All tool arguments are sanitized before use:

```python
from blink.security.sanitize import sanitize_command, sanitize_path

# Remove null bytes, control characters, overly-long strings
clean_cmd = sanitize_command("ls -la\x00; rm -rf /")  # → "ls -la; rm -rf /"

# Prevent path traversal
safe_path = sanitize_path("../../etc/passwd", base_dir="/home/user")
# raises ValueError: Path escapes the allowed base directory

# Check without raising
from blink.security.sanitize import is_safe_path
is_safe_path("../../etc/passwd", base_dir="/home/user")  # → False
```

### What `sanitize_command` Does

1. Truncates to 8192 characters
2. Removes null bytes (`\x00`) and non-printable control characters
3. Normalises Unicode to NFC form
4. Optionally strips shell metacharacters (`strict=True`)

### What `sanitize_path` Does

1. Truncates to 4096 characters
2. Removes null bytes and control characters
3. Normalises via `PurePosixPath` (resolves `..` and redundant slashes)
4. If `base_dir` is provided, resolves symlinks and verifies containment

---

## Threat Model

| Threat | Mitigation |
|--------|-----------|
| Agent runs malicious commands | Capability model + confirmation prompts |
| Agent exfiltrates secrets via tool args | Redaction before audit logging |
| Path traversal in file tools | `sanitize_path` with `base_dir` |
| Shell injection via crafted tool args | `sanitize_command`, `shlex.quote` |
| Denial-of-service via excessive tool calls | Rate limiter |
| Credential theft from config files | System keyring + file obfuscation |
| Catch-all auto-approve bypasses all checks | Audit tool warns on wildcard patterns |

### Out of Scope

- **Network-level security** — Blink communicates via a Unix domain socket
  (local-only by default). HTTP transport security is the caller's
  responsibility.
- **Kernel-level sandboxing** — Blink does not use seccomp, namespaces, or
  other kernel-level isolation. ACT-level tools run with the user's full
  permissions.
- **Cryptographic credential encryption** — the file fallback uses XOR
  obfuscation, not AES. For strong encryption, use the system keyring or
  an external secrets manager.
