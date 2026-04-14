# Blink shell integration for Zsh
#
# Source this file from your ~/.zshrc:
#   source /path/to/blink/shell/zsh/blink.zsh
#
# OSC 133 semantic shell marks:
#   A = prompt start
#   B = prompt end   (user starts typing)
#   C = command start (Enter pressed)
#   D;exit_code = command end

# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------
[[ -o interactive ]] || return 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Emit an OSC 133 mark
_blink_osc133() {
    printf '\033]133;%s\007' "$1"
}

# Send a JSON-lines event to the Blink daemon
_blink_send_event() {
    local payload="$1"
    local sock="${BLINK_SOCK:-${HOME}/.blink/blink.sock}"
    if [[ -S "$sock" ]]; then
        printf '%s\n' "$payload" | socat -t1 - "UNIX-CONNECT:${sock}" 2>/dev/null || true
    fi
}

_blink_json_escape() {
    printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""'
}

# ---------------------------------------------------------------------------
# Zsh hooks
# ---------------------------------------------------------------------------

# precmd — runs before each prompt
_blink_precmd() {
    local exit_code=$?

    # OSC 133;D;<exit_code> if a command was running
    if [[ -n "${_BLINK_CMD_RUNNING:-}" ]]; then
        _blink_osc133 "D;${exit_code}"
        unset _BLINK_CMD_RUNNING

        # Record completed block
        _blink_send_event "{\"cmd\":\"record_block\",\"params\":{\"session_id\":\"${BLINK_SESSION_ID:-}\",\"block_data\":{\"command\":$(_blink_json_escape "${_BLINK_LAST_CMD:-}"),\"cwd\":\"${PWD}\",\"exit_code\":${exit_code}}}}"
        unset _BLINK_LAST_CMD
    fi

    # OSC 133;A — prompt start
    _blink_osc133 "A"
}

# preexec — runs just before a command is executed (with the command text)
_blink_preexec() {
    local cmd="$1"
    _BLINK_LAST_CMD="$cmd"
    _BLINK_CMD_RUNNING=1

    # OSC 133;C — command start
    _blink_osc133 "C"

    _blink_send_event "{\"cmd\":\"buffer_changed\",\"params\":{\"session_id\":\"${BLINK_SESSION_ID:-}\",\"cwd\":\"${PWD}\",\"command\":$(_blink_json_escape "$cmd")}}"
}

# Register hooks via Zsh's add-zsh-hook if autoload is available
if autoload -Uz add-zsh-hook 2>/dev/null; then
    add-zsh-hook precmd  _blink_precmd
    add-zsh-hook preexec _blink_preexec
else
    # Fallback: prepend to existing hook arrays
    precmd_functions=(_blink_precmd "${precmd_functions[@]}")
    preexec_functions=(_blink_preexec "${preexec_functions[@]}")
fi

# ---------------------------------------------------------------------------
# Inject OSC 133;B at the end of PROMPT (marks end-of-prompt / start-of-input)
# ---------------------------------------------------------------------------
if [[ "${PROMPT:-}" != *'133;B'* ]]; then
    PROMPT="${PROMPT:-$ }"$'\033]133;B\007'
fi

# ---------------------------------------------------------------------------
# Session registration
# ---------------------------------------------------------------------------
_blink_register_session() {
    local window_id="${KITTY_WINDOW_ID:-}"
    local session_id
    session_id="$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || echo '')"
    export BLINK_SESSION_ID="$session_id"

    _blink_send_event "{\"cmd\":\"register_session\",\"params\":{\"window_id\":${window_id:-null},\"cwd\":\"${PWD}\",\"session_id\":\"${session_id}\"}}"
}

_blink_register_session
