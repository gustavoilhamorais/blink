#!/usr/bin/env bash
# Blink shell integration for Bash
#
# Source this file from your ~/.bashrc:
#   source /path/to/blink/shell/bash/blink.bash
#
# OSC 133 semantic shell marks:
#   A = prompt start
#   B = prompt end   (user starts typing)
#   C = command start (Enter pressed)
#   D;exit_code = command end

# ---------------------------------------------------------------------------
# Guard: skip if not running interactively or inside Blink-managed session
# ---------------------------------------------------------------------------
[[ $- == *i* ]] || return 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Emit an OSC 133 mark: ESC ] 133 ; <mark> BEL
_blink_osc133() {
    printf '\033]133;%s\007' "$1"
}

# Send a JSON-lines event to the Blink daemon via Unix socket
_blink_send_event() {
    local payload="$1"
    local sock="${BLINK_SOCK:-${HOME}/.blink/blink.sock}"
    if [[ -S "$sock" ]]; then
        printf '%s\n' "$payload" | socat -t1 - "UNIX-CONNECT:${sock}" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
_BLINK_LAST_CMD=""
_BLINK_CMD_RUNNING=0

# ---------------------------------------------------------------------------
# preexec equivalent via DEBUG trap
# Called before each command is executed
# ---------------------------------------------------------------------------
_blink_preexec() {
    # Only fire once per command (DEBUG trap fires multiple times)
    if [[ $_BLINK_CMD_RUNNING -eq 0 ]]; then
        _BLINK_CMD_RUNNING=1
        _BLINK_LAST_CMD="$BASH_COMMAND"

        # OSC 133;C — command start
        _blink_osc133 "C"

        # Notify daemon of command start
        _blink_send_event "{\"cmd\":\"buffer_changed\",\"params\":{\"session_id\":\"${BLINK_SESSION_ID:-}\",\"cwd\":\"${PWD}\",\"command\":$(printf '%s' "$BASH_COMMAND" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')}}"
    fi
    return 0
}

# Install DEBUG trap only if not already set by another tool
if [[ -z "${_BLINK_TRAP_INSTALLED:-}" ]]; then
    trap '_blink_preexec' DEBUG
    _BLINK_TRAP_INSTALLED=1
fi

# ---------------------------------------------------------------------------
# precmd via PROMPT_COMMAND
# Called before each prompt is drawn (i.e. after a command finishes)
# ---------------------------------------------------------------------------
_blink_precmd() {
    local exit_code=$?

    # OSC 133;D;<exit_code> — command end (only if a command was running)
    if [[ $_BLINK_CMD_RUNNING -eq 1 ]]; then
        _blink_osc133 "D;${exit_code}"
        _BLINK_CMD_RUNNING=0

        # Record block in daemon
        _blink_send_event "{\"cmd\":\"record_block\",\"params\":{\"session_id\":\"${BLINK_SESSION_ID:-}\",\"block_data\":{\"command\":$(printf '%s' "$_BLINK_LAST_CMD" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""'),\"cwd\":\"${PWD}\",\"exit_code\":${exit_code}}}}"
    fi

    # OSC 133;A — prompt start
    _blink_osc133 "A"
}

# OSC 133;B — prompt end (injected into PS1 via \[ \] non-printing sequence)
_blink_prompt_end() {
    printf '\033]133;B\007'
}

# ---------------------------------------------------------------------------
# Wire up PROMPT_COMMAND
# ---------------------------------------------------------------------------
if [[ -z "$PROMPT_COMMAND" ]]; then
    PROMPT_COMMAND="_blink_precmd"
else
    # Prepend to existing PROMPT_COMMAND
    PROMPT_COMMAND="_blink_precmd; ${PROMPT_COMMAND}"
fi

# Append the OSC 133;B marker to PS1 (marks end of prompt / start of input)
# We only do this if PS1 doesn't already contain our marker.
if [[ "${PS1:-}" != *'133;B'* ]]; then
    PS1="${PS1:-\$ }"$'\033]133;B\007'
fi

# ---------------------------------------------------------------------------
# Session registration
# Called once when this script is sourced.
# ---------------------------------------------------------------------------
_blink_register_session() {
    # Use Kitty window ID if available, fall back to empty
    local window_id="${KITTY_WINDOW_ID:-}"
    local session_id
    session_id="$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || echo '')"
    export BLINK_SESSION_ID="$session_id"

    _blink_send_event "{\"cmd\":\"register_session\",\"params\":{\"window_id\":${window_id:-null},\"cwd\":\"${PWD}\",\"session_id\":\"${session_id}\"}}"
}

_blink_register_session
