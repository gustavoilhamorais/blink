# Shell Integration

Blink's shell integration hooks let the daemon track commands, record blocks,
and provide AI features in context.  It uses
[OSC 133 semantic shell marks](https://sw.kovidgoyal.net/kitty/shell-integration/)
(the same standard used by Kitty itself) plus a small IPC protocol over a
Unix domain socket.

## What Shell Integration Does

1. **Assigns a session ID** (`BLINK_SESSION_ID`) to each shell session.
2. **Emits OSC 133 marks** so Kitty (and the block tracker) know where
   prompts, commands, and outputs begin and end.
3. **Notifies the daemon** of command start/end, recording each command as
   a *block* with its CWD and exit code.
4. Optionally **exports completions** — if the Blink completion engine is
   enabled, it wires into the shell's tab-complete mechanism.

---

## Installation

### Quick Install (Recommended)

Run the onboarding kitten from inside Kitty:

```bash
kitty +kitten blink_onboarding
```

The wizard will detect your shell and append the appropriate `source` line
to your RC file.

### Manual Install — Bash

Add to `~/.bashrc`:

```bash
# Blink shell integration
source "/path/to/blink/shell/bash/blink.bash"
```

Then reload:

```bash
source ~/.bashrc
```

### Manual Install — Zsh

Add to `~/.zshrc`:

```zsh
# Blink shell integration
source "/path/to/blink/shell/zsh/blink.zsh"
```

Then reload:

```zsh
source ~/.zshrc
```

### Manual Install — Fish

Add to `~/.config/fish/config.fish`:

```fish
# Blink shell integration
source "/path/to/blink/shell/fish/blink.fish"
```

Then reload:

```fish
source ~/.config/fish/config.fish
```

---

## Configuration Options

Shell integration respects the following environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `BLINK_SESSION_ID` | Auto-generated UUID | Session identifier. Set automatically on source. |
| `BLINK_SOCK` | `~/.blink/blink.sock` | Path to the daemon Unix socket. |
| `BLINK_DIR` | `~/.blink` | Blink data directory. |
| `BLINK_DISABLE` | *(unset)* | Set to any value to disable integration silently. |
| `BLINK_COMPLETIONS` | `1` | Set to `0` to disable AI completions. |
| `BLINK_BLOCKS` | `1` | Set to `0` to disable block recording. |

### Example: Custom Socket Path

```bash
export BLINK_SOCK=/run/user/1000/blink.sock
source ~/blink/shell/bash/blink.bash
```

### Example: Disable Completions Only

```bash
export BLINK_COMPLETIONS=0
source ~/blink/shell/bash/blink.bash
```

---

## How It Works

### Session Registration

When the integration script is sourced, it:

1. Generates a UUID (`BLINK_SESSION_ID`).
2. Sends a `register_session` IPC message to the daemon with the Kitty
   window ID (`KITTY_WINDOW_ID`), CWD, and session ID.

### OSC 133 Marks

The shell emits marks at four points:

| Mark | When | Meaning |
|------|------|---------|
| `A` | Before prompt is drawn | Prompt start |
| `B` | After prompt ends (injected into `PS1`) | Prompt end / input start |
| `C` | Before command runs (`DEBUG` trap / `preexec`) | Command start |
| `D;<exit_code>` | After command finishes (`PROMPT_COMMAND` / `precmd`) | Command end |

Kitty uses these marks to implement clickable output blocks.  Blink uses
them to track block boundaries.

### Block Recording

After each command completes, the shell sends a `record_block` IPC message:

```json
{
  "cmd": "record_block",
  "params": {
    "session_id": "…",
    "block_data": {
      "command": "git status",
      "cwd": "/home/user/project",
      "exit_code": 0
    }
  }
}
```

The daemon stores this in SQLite.  The block is then queryable via:
- `blink block list`
- `blink block show <id>`
- MCP tool `list_blocks` / `get_block`

---

## Shell-Specific Notes

### Bash

- Uses the `DEBUG` trap for `preexec`.
- Uses `PROMPT_COMMAND` for `precmd`.
- Compatible with existing `PROMPT_COMMAND` (prepends, does not replace).
- Compatible with `bash-preexec` if already installed.
- Requires `socat` for daemon IPC (falls back silently if absent).

### Zsh

- Uses `preexec` / `precmd` hooks from Zsh's hook system.
- Works alongside `oh-my-zsh`, `prezto`, and `starship`.
- If `zsh-autosuggestions` is installed, Blink completions integrate with it.

### Fish

- Uses Fish's `fish_preexec` / `fish_postexec` event hooks.
- No `PROMPT_COMMAND` equivalent needed.
- Completions registered as Fish completions functions.

---

## Troubleshooting

### Daemon Not Running

**Symptom:** No block recording, no completions.

**Fix:**
```bash
blink daemon start
blink daemon status
```

### `socat` Not Found (Bash/Zsh)

**Symptom:** `_blink_send_event: command not found` errors.

**Fix:**
```bash
# macOS
brew install socat
# Ubuntu/Debian
sudo apt-get install socat
```

Alternatively, the integration degrades gracefully — blocks will not be
recorded but completions still work if the daemon is reachable.

### Session ID Not Set

**Symptom:** `blink block list` says "No session ID provided."

**Fix:** Source the integration in your current shell:

```bash
source ~/blink/shell/bash/blink.bash
echo $BLINK_SESSION_ID   # should now be set
```

### Completions Not Appearing

**Symptom:** Tab does not trigger Blink completions.

**Checks:**
1. Is the daemon running? `blink daemon status`
2. Is `BLINK_COMPLETIONS` set to `0`? Unset it.
3. Is the AI provider configured? `blink provider list`
4. Is there an API key? `echo $OPENAI_API_KEY` (or equivalent).

### Kitty Window ID Not Detected

**Symptom:** `blink session list` shows sessions with no window ID.

**Fix:** Ensure you are running inside Kitty.  `$KITTY_WINDOW_ID` is set
automatically by Kitty for each window.

### Conflicting `PROMPT_COMMAND` or `preexec` Hooks

If another tool (e.g. `bash-preexec`, `starship`) also modifies
`PROMPT_COMMAND` or installs a `DEBUG` trap, source Blink's integration
**after** those tools to avoid conflicts.

---

## Verifying Installation

```bash
# Check that the session ID was registered
echo $BLINK_SESSION_ID

# Check daemon received the session
blink daemon status
blink block list --session $BLINK_SESSION_ID

# Run a test command and verify it was recorded
ls /tmp
blink block list --session $BLINK_SESSION_ID --limit 1
```

---

## Uninstalling

Remove the `source` line from your RC file (`~/.bashrc`, `~/.zshrc`, or
`~/.config/fish/config.fish`) and reload your shell.

To remove all Blink data:

```bash
blink daemon stop
rm -rf ~/.blink
```
