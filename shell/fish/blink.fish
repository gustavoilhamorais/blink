# Blink shell integration for Fish
#
# Source this file from your ~/.config/fish/config.fish:
#   source /path/to/blink/shell/fish/blink.fish
#
# OSC 133 semantic shell marks:
#   A = prompt start
#   B = prompt end
#   C = command start
#   D;exit_code = command end

# ---------------------------------------------------------------------------
# Guard: only run in interactive sessions
# ---------------------------------------------------------------------------
status is-interactive; or return

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function _blink_osc133 --description "Emit OSC 133 mark"
    printf '\033]133;%s\007' $argv[1]
end

function _blink_send_event --description "Send JSON event to Blink daemon socket"
    set -l payload $argv[1]
    set -l sock (test -n "$BLINK_SOCK"; and echo $BLINK_SOCK; or echo "$HOME/.blink/blink.sock")
    if test -S $sock
        printf '%s\n' $payload | socat -t1 - "UNIX-CONNECT:$sock" 2>/dev/null
    end
end

function _blink_json_escape --description "JSON-encode a string"
    printf '%s' $argv[1] | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null
    or echo '""'
end

# ---------------------------------------------------------------------------
# fish_prompt — wraps the existing prompt with OSC 133;A and B marks
# We only install this wrapper once.
# ---------------------------------------------------------------------------

# Save original fish_prompt if it exists and hasn't been wrapped yet
if not functions -q _blink_original_fish_prompt
    if functions -q fish_prompt
        functions --copy fish_prompt _blink_original_fish_prompt
    else
        function _blink_original_fish_prompt
            echo -n '> '
        end
    end
end

function fish_prompt --description "Blink-wrapped fish_prompt with OSC 133 marks"
    # Emit OSC 133;A at prompt start
    _blink_osc133 "A"

    # Call the original prompt
    _blink_original_fish_prompt

    # Emit OSC 133;B at prompt end (cursor is now at input position)
    _blink_osc133 "B"
end

# ---------------------------------------------------------------------------
# fish_preexec — emits OSC 133;C before command runs
# ---------------------------------------------------------------------------
function _blink_preexec --on-event fish_preexec --description "Blink preexec hook"
    set -g _BLINK_LAST_CMD $argv[1]
    set -g _BLINK_CMD_RUNNING 1
    _blink_osc133 "C"
    _blink_send_event "{\"cmd\":\"buffer_changed\",\"params\":{\"session_id\":\"$BLINK_SESSION_ID\",\"cwd\":\"$PWD\",\"command\":(_blink_json_escape $argv[1])}}"
end

# ---------------------------------------------------------------------------
# fish_postexec — emits OSC 133;D after command ends
# ---------------------------------------------------------------------------
function _blink_postexec --on-event fish_postexec --description "Blink postexec hook"
    set -l exit_code $argv[2]
    if set -q _BLINK_CMD_RUNNING
        _blink_osc133 "D;$exit_code"
        set -e _BLINK_CMD_RUNNING

        set -l cmd_json (_blink_json_escape "$_BLINK_LAST_CMD")
        _blink_send_event "{\"cmd\":\"record_block\",\"params\":{\"session_id\":\"$BLINK_SESSION_ID\",\"block_data\":{\"command\":$cmd_json,\"cwd\":\"$PWD\",\"exit_code\":$exit_code}}}"
        set -e _BLINK_LAST_CMD
    end
end

# ---------------------------------------------------------------------------
# Session registration
# ---------------------------------------------------------------------------
function _blink_register_session --description "Register this shell session with the Blink daemon"
    set -l window_id (test -n "$KITTY_WINDOW_ID"; and echo $KITTY_WINDOW_ID; or echo "null")
    set -l session_id (uuidgen 2>/dev/null; or python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null; or echo "")
    set -gx BLINK_SESSION_ID $session_id
    _blink_send_event "{\"cmd\":\"register_session\",\"params\":{\"window_id\":$window_id,\"cwd\":\"$PWD\",\"session_id\":\"$session_id\"}}"
end

_blink_register_session
