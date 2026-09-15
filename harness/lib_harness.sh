#!/usr/bin/env bash
# ==============================================================================
# lib_harness.sh - Shell Client SDK for fd-harness
# ==============================================================================
# Sourced by bash scripts running under fd-harness supervisor.
# Provides clean, idiomatic shell functions wrapping # @harness directives
# and event parsing without requiring direct protocol formatting.
# ==============================================================================

# Guard against double-sourcing
if [[ -n "${_LIB_HARNESS_SOURCED:-}" ]]; then
    return 0 2>/dev/null || exit 0
fi
_LIB_HARNESS_SOURCED=1

# Default event FD passed by harness (default: FD 3)
HARNESS_EVENT_FD="${HARNESS_EVENT_FD:-3}"

# Output variables populated by wait and read operations
HARNESS_TAG=""
HARNESS_ID=""
HARNESS_CYCLE=0
HARNESS_SKIPPED=0
HARNESS_LAG_MS=0.0
HARNESS_STATUS=""
HARNESS_SCHEDULED=""
HARNESS_TAGS=""

# ------------------------------------------------------------------------------
# Clock Primitives (Control Plane - stdin/stdout)
# ------------------------------------------------------------------------------

# Initialize a periodic clock
# Usage: harness_clock_init <id> <interval> [cycles] [policy] [align]
harness_clock_init() {
    local id="$1"
    local interval="$2"
    local cycles="${3:-}"
    local policy="${4:-skip}"
    local align="${5:-}"

    local cmd="# @harness.clock:init id=${id} interval=${interval} policy=${policy}"
    if [[ -n "$cycles" && "$cycles" != "0" ]]; then
        cmd="${cmd} cycles=${cycles}"
    fi
    if [[ -n "$align" ]]; then
        cmd="${cmd} align=\"${align}\""
    fi
    echo "$cmd"
}

# Start an initialized or paused clock
# Usage: harness_clock_start <id>
harness_clock_start() {
    local id="$1"
    echo "# @harness.clock:start id=${id}"
}

# Update a running clock's interval or expire_at
# Usage: harness_clock_update <id> [interval=<val>] [expire_at=<val>]
harness_clock_update() {
    local id="$1"
    shift
    local args="$*"
    echo "# @harness.clock:update id=${id} ${args}"
}

# Synchronous wait for next clock tick
# Usage: while harness_clock_wait <id>; do ... done
# Returns: 0 if valid tick received, 1 if clock finished (status=done) or error
harness_clock_wait() {
    local id="$1"
    echo "# @harness.clock:wait id=${id}"

    # Read positional response from control plane (stdin)
    # Format: tick <id> <cycle> <skipped> <lag_ms> <monotonic_ts> <status>
    if ! read -r HARNESS_TAG HARNESS_ID HARNESS_CYCLE HARNESS_SKIPPED HARNESS_LAG_MS HARNESS_MONO_TS HARNESS_STATUS; then
        return 1
    fi

    # Return 1 if clock reached cycle limit or expired
    if [[ "$HARNESS_STATUS" == "done" ]]; then
        return 1
    fi

    return 0
}

# Request clock status telemetry
# Usage: harness_clock_status <id>
harness_clock_status() {
    local id="$1"
    echo "# @harness.clock:status id=${id}"
    read -r HARNESS_TAG HARNESS_ID HARNESS_STATUS HARNESS_CYCLE HARNESS_SKIPPED HARNESS_LAG_MS
}

# ------------------------------------------------------------------------------
# Timer Primitives (One-shot and intervals)
# ------------------------------------------------------------------------------

# Sleep without drift via harness
# Usage: harness_sleep <duration>
harness_sleep() {
    local duration="$1"
    echo "# @harness.timer:sleep duration=${duration}"
    read -r HARNESS_TAG HARNESS_ID
}

# Shift execution forward until next grid boundary (e.g. '*/1s', '*/1min')
# Usage: harness_shift [to_expr]
harness_shift() {
    local to="${1:-*/1s}"
    echo "# @harness.shift to=\"${to}\""
    read -r HARNESS_TAG HARNESS_ID
}

# Register an interval stream to dedicated FD
# Usage: harness_interval_stream <id> <interval> [on_fd]
harness_interval_stream() {
    local id="$1"
    local interval="$2"
    local fd="${3:-$HARNESS_EVENT_FD}"
    echo "# @harness.timer:interval id=${id} interval=${interval} on_fd=${fd}"
}

# Cancel any timer or clock
# Usage: harness_cancel <id>
harness_cancel() {
    local id="$1"
    echo "# @harness.timer:cancel id=${id}"
}

# ------------------------------------------------------------------------------
# Schedule Primitives (Calendar / Agenda Orchestrator)
# ------------------------------------------------------------------------------

# Initialize a schedule
# Usage: harness_schedule_init <id> [policy] [state_file]
harness_schedule_init() {
    local id="$1"
    local policy="${2:-skip}"
    local state_file="${3:-}"

    local cmd="# @harness.schedule:init id=${id} policy=${policy}"
    if [[ -n "$state_file" ]]; then
        cmd="${cmd} state_file=\"${state_file}\""
    fi
    echo "$cmd"
}

# Attach a calendar / cron rule to a schedule
# Usage: harness_schedule_rule <id> <expr> [tags]
harness_schedule_rule() {
    local id="$1"
    local expr="$2"
    local tags="${3:-}"

    local cmd="# @harness.schedule:rule id=${id} expr=\"${expr}\""
    if [[ -n "$tags" ]]; then
        cmd="${cmd} tags=\"${tags}\""
    fi
    echo "$cmd"
}

# Synchronous wait for next scheduled occurrence
# Usage: while harness_schedule_wait <id>; do ... done
# Returns: 0 if occurrence fired or replayed, 1 on EOF / termination
harness_schedule_wait() {
    local id="$1"
    echo "# @harness.schedule:wait id=${id}"

    # Read positional response from control plane (stdin)
    # Format: schedule <id> <scheduled_iso> <lag_ms> <tags> <status>
    if ! read -r HARNESS_TAG HARNESS_ID HARNESS_SCHEDULED HARNESS_LAG_MS HARNESS_TAGS HARNESS_STATUS; then
        return 1
    fi

    if [[ "$HARNESS_STATUS" == "no_rules" || "$HARNESS_STATUS" == "done" ]]; then
        return 1
    fi

    return 0
}

# Dump schedule state snapshot to file
# Usage: harness_schedule_dump <id> [file_path]
harness_schedule_dump() {
    local id="$1"
    local file_path="${2:-}"

    local cmd="# @harness.schedule:dump id=${id}"
    if [[ -n "$file_path" ]]; then
        cmd="${cmd} file=\"${file_path}\""
    fi
    echo "$cmd"
}

# Cancel a schedule
# Usage: harness_schedule_cancel <id>
harness_schedule_cancel() {
    local id="$1"
    echo "# @harness.schedule:cancel id=${id}"
}

# ------------------------------------------------------------------------------
# Event Stream Primitives (Data Plane - FD 3)
# ------------------------------------------------------------------------------

# Read one event from event stream FD
# Usage: harness_read_event [fd]
harness_read_event() {
    local fd="${1:-$HARNESS_EVENT_FD}"
    read -u "$fd" -r HARNESS_TAG HARNESS_ID HARNESS_CYCLE HARNESS_SKIPPED HARNESS_LAG_MS HARNESS_STATUS
}

# ------------------------------------------------------------------------------
# Pratt Coprocessor Primitives (calc & sprint)
# ------------------------------------------------------------------------------

# Evaluate arithmetic / physical unit expression via coprocessor
# Usage: harness_calc "5 * 6" [var_name] -> sets $HARNESS_VAL
harness_calc() {
    local expr="$1"
    local store="${2:-}"
    if [[ -n "$store" ]]; then
        echo "# @harness.calc expr=\"${expr}\" store=\"${store}\""
    else
        echo "# @harness.calc expr=\"${expr}\""
    fi
    read -r HARNESS_TAG HARNESS_VAL
}

# Query registered variables from the Pratt coprocessor
# Usage: harness_calc_list -> sets $HARNESS_VARS and $HARNESS_COUNT
harness_calc_list() {
    echo "# @harness.calc:list"
    read -r HARNESS_TAG HARNESS_VARS HARNESS_COUNT
}

# Interpolate template string with bracketed [expressions]
# Usage: harness_sprint "Hello val=[5 * 6] ms=[100ms * 2]" -> sets $HARNESS_VAL
harness_sprint() {
    local template="$1"
    echo "# @harness.sprint text=\"${template}\""
    read -r HARNESS_TAG HARNESS_VAL
}

# ------------------------------------------------------------------------------
# I/O Stream Proxy Primitives (Filters & Structured Supervisor Logs)
# ------------------------------------------------------------------------------

# Register a regex redaction mask filter on child stdout
# Usage: harness_filter_mask <regex_pattern> [replacement]
harness_filter_mask() {
    local pattern="$1"
    local replacement="${2:-[REDACTED]}"
    echo "# @harness.filter:mask pattern=\"${pattern}\" replacement=\"${replacement}\""
}

# Print formatted log directly from supervisor to user terminal
# Usage: harness_log <text_template> [level]
harness_log() {
    local text="$1"
    local level="${2:-INFO}"
    echo "# @harness.log text=\"${text}\" level=\"${level}\""
}

# ------------------------------------------------------------------------------
# User Input Stream Primitives (Human Terminal Proxy - FD 4)
# ------------------------------------------------------------------------------
HARNESS_USER_FD="${HARNESS_USER_FD:-4}"

# Read one line of user keyboard/terminal input proxied by Python
# Usage: harness_user_read <var_name>
# Returns: 0 on successful line read, 1 on EOF / close
harness_user_read() {
    local _var_name="$1"
    local _input=""
    if read -u "$HARNESS_USER_FD" -r _input; then
        printf -v "$_var_name" '%s' "$_input"
        return 0
    fi
    return 1
}
