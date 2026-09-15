#!/usr/bin/env bash
# ==============================================================================
# clock-tick.sh - Metrology Benchmark: Cartesian Product of Cadence & Reference
#
# Orthogonal Dimensions:
#   Dimension 1: Reference Anchor (--ref=init | last)
#     - init: Absolute epoch T0 (self-correcting clock, memory of past delays)
#     - last: Relative to previous cycle start (memoryless, cumulative drift)
#
#   Dimension 2: Waiting Mechanism (--strategy=blind | computed | polling | harnessed-timer-sleep | harnessed-timer-shift | harnessed-clock | harnessed-scheduler)
#     - blind:                  Sleeps CADENCE ignoring work duration (/usr/bin/sleep)
#     - computed:               Measures remaining delta to target and sleeps delta (/usr/bin/sleep)
#     - polling:                Micro-sleeps in a loop until target is crossed (/usr/bin/sleep)
#     - harnessed-timer-sleep:  Stateless relative timer: in-shell delta + @harness.sleep
#     - harnessed-timer-shift:  Stateless grid timer: zero math in shell + @harness.shift
#     - harnessed-clock:        Isochronous metronome: phase-locked pacing via @harness.clock:wait
#     - harnessed-scheduler:    Calendar agenda: cron-like rule pacing via @harness.schedule:wait
# ==============================================================================
set -euo pipefail

# Default parameters
STRATEGY="computed"
REF="init"
POLICY="skip"
CADENCE=2.0
WORK_DURATION=0.5
POLL_STEP=0.05
CYCLES=5
CALC="bc"  # bc, awk, or harness
SLEEP_BACKEND=""  # bin or harness (auto-selected if empty)
DATE_ENGINE="bin"  # bin or harness
SPIKE_CYCLE=-1
SPIKE_DURATION=1.5
VERBOSE=0
ALIGN=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Dimensions:
  -s, --strategy=STRATEGY   Native bash: blind, computed, polling
                            Harness coproc:
                              - harnessed-timer (or timer)
                              - harnessed-shift (or shift)
                              - harnessed-clock (or clock)
                              - harnessed-scheduler (or scheduler)
                            (default: computed)
  -r, --ref=ANCHOR          init (T0 absolute) or last (cycle-relative) (default: init)
      --policy=POLICY       Overrun policy: skip (default) or catchup
  -b, --sleep=BACKEND       Sleep backend: bin (/usr/bin/sleep) or harness (# @harness.sleep)
                            (default: bin, or harness if strategy is harnessed-timer)
  -c, --calc=ENGINE         Math engine for calculations: bc, awk, or harness (default: bc)
  -d, --date=ENGINE         Clock/Date engine: bin (/usr/bin/date) or harness (# @harness.time) (default: bin)

Timing Parameters:
  -i, --cadence=SEC         Target cadence interval in seconds (default: 2.0)
  -w, --work-duration=SEC   Simulated work duration in seconds: fixed (0.5), disabled (0), or cyclic ('0.02,0.08,0.04')
  -p, --poll-step=SEC       Polling step in seconds for polling strategy (default: 0.05)
  -n, --cycles=N            Number of cycles (default: 5)
  -a, --align=PHASE         Phase grid alignment e.g. '*/1s', '*/100ms', '*/250ms' (default: none)
  -v, --verbose             Print detailed metrology telemetry (wall date vs monotonic ts)

Spike Injection (Overrun Testing):
  --spike-cycle=N           Cycle index where a latency spike occurs (default: -1, disabled)
  --spike-duration=SEC      Duration of the latency spike in seconds (default: 1.5)
  -h, --help                Show this help message
EOF
    exit 1
}

# Standard bash builtin getopts supporting short flags and --long-options
while getopts "s:r:i:w:p:n:c:b:d:a:vh-:" opt; do
    if [[ "$opt" == "-" ]]; then
        case "$OPTARG" in
            strategy=*) STRATEGY="${OPTARG#*=}" ;;
            ref=*) REF="${OPTARG#*=}" ;;
            policy=*) POLICY="${OPTARG#*=}" ;;
            cadence=*) CADENCE="${OPTARG#*=}" ;;
            work-duration=*) WORK_DURATION="${OPTARG#*=}" ;;
            poll-step=*) POLL_STEP="${OPTARG#*=}" ;;
            cycles=*) CYCLES="${OPTARG#*=}" ;;
            calc=*) CALC="${OPTARG#*=}" ;;
            sleep=*|sleep-backend=*) SLEEP_BACKEND="${OPTARG#*=}" ;;
            date=*|date-engine=*) DATE_ENGINE="${OPTARG#*=}" ;;
            align=*) ALIGN="${OPTARG#*=}" ;;
            spike-cycle=*) SPIKE_CYCLE="${OPTARG#*=}" ;;
            spike-duration=*) SPIKE_DURATION="${OPTARG#*=}" ;;
            verbose) VERBOSE=1 ;;
            help) usage ;;
            *) echo "Unknown option --$OPTARG" >&2; usage ;;
        esac
    else
        case "$opt" in
            s) STRATEGY="$OPTARG" ;;
            r) REF="$OPTARG" ;;
            i) CADENCE="$OPTARG" ;;
            w) WORK_DURATION="$OPTARG" ;;
            p) POLL_STEP="$OPTARG" ;;
            n) CYCLES="$OPTARG" ;;
            c) CALC="$OPTARG" ;;
            b) SLEEP_BACKEND="$OPTARG" ;;
            d) DATE_ENGINE="$OPTARG" ;;
            a) ALIGN="$OPTARG" ;;
            v) VERBOSE=1 ;;
            h) usage ;;
            *) usage ;;
        esac
    fi
done
shift $((OPTIND - 1))

is_harness_active() {
    [[ -n "${HARNESS_ACTIVE:-}" || -e /proc/$$/fd/3 ]]
}

# Normalize strategy names (support shorthands & backward-compat aliases)
case "$STRATEGY" in
    timer|harnessed-timer-sleep|harnessed-pure)  STRATEGY="harnessed-timer" ;;
    shift|harnessed-timer-shift)                 STRATEGY="harnessed-shift" ;;
    clock|harnessed-pull|harnessed-fork)         STRATEGY="harnessed-clock" ;;
    schedule|scheduler|harnessed-schedule)       STRATEGY="harnessed-scheduler" ;;
esac

# Default sleep backend based on strategy if not specified
if [[ -z "$SLEEP_BACKEND" ]]; then
    if [[ "$STRATEGY" == "harnessed-timer" ]]; then
        SLEEP_BACKEND="harness"
    else
        SLEEP_BACKEND="bin"
    fi
fi

# Validation
case "$STRATEGY" in
    blind|computed|polling|harnessed-timer|harnessed-shift|harnessed-clock|harnessed-scheduler) ;;
    *) echo "Invalid strategy: $STRATEGY (choose blind, computed, polling, harnessed-timer, harnessed-shift, harnessed-clock, harnessed-scheduler)" >&2; exit 1 ;;
esac

if [[ "$STRATEGY" == harnessed-* ]] && ! is_harness_active; then
    echo "Error: Strategy '$STRATEGY' requires execution under the fd-harness supervisor." >&2
    echo "       Run with: uv run fd-harness run $0 [OPTIONS] -s $STRATEGY" >&2
    exit 1
fi

case "$SLEEP_BACKEND" in
    bin|harness) ;;
    *) echo "Invalid sleep backend: $SLEEP_BACKEND (choose bin or harness)" >&2; exit 1 ;;
esac

if [[ "$SLEEP_BACKEND" == "harness" ]] && ! is_harness_active; then
    echo "Error: Sleep backend 'harness' requires execution under the fd-harness supervisor." >&2
    echo "       Run with: uv run fd-harness run $0 [OPTIONS] -b harness" >&2
    exit 1
fi

case "$DATE_ENGINE" in
    bin|harness) ;;
    *) echo "Invalid date engine: $DATE_ENGINE (choose bin or harness)" >&2; exit 1 ;;
esac

if [[ "$DATE_ENGINE" == "harness" ]] && ! is_harness_active; then
    echo "Error: Date engine 'harness' requires execution under the fd-harness supervisor." >&2
    echo "       Run with: uv run fd-harness run $0 [OPTIONS] -d harness" >&2
    exit 1
fi

# Date/Time helpers (zero-fork via # @harness.time and printf -v when using harness)
get_time() {
    local _out_ns="$1"
    local _out_wall="${2:-}"
    if [[ "$DATE_ENGINE" == "harness" ]]; then
        echo "# @harness.time"
        read -r _TAG _ns _wall _mono
        printf -v "$_out_ns" "%s" "$_ns"
        if [[ -n "$_out_wall" ]]; then
            printf -v "$_out_wall" "%s" "$_wall"
        fi
    else
        printf -v "$_out_ns" "%s" "$(date +%s%N)"
        if [[ -n "$_out_wall" ]]; then
            printf -v "$_out_wall" "%s" "$(date +"%T.%3N")"
        fi
    fi
}

get_wall_time() {
    local _out_wall="$1"
    if [[ "$DATE_ENGINE" == "harness" ]]; then
        echo "# @harness.time"
        read -r _TAG _ns _wall _mono
        printf -v "$_out_wall" "%s" "$_wall"
    else
        printf -v "$_out_wall" "%s" "$(date +"%T.%3N")"
    fi
}

case "$REF" in
    init|last) ;;
    *) echo "Invalid ref: $REF (choose init or last)" >&2; exit 1 ;;
esac

case "$POLICY" in
    skip|catchup) ;;
    *) echo "Invalid policy: $POLICY (choose skip or catchup)" >&2; exit 1 ;;
esac

case "$CALC" in
    bc|awk|harness) ;;
    *) echo "Invalid calc engine: $CALC (choose bc, awk, or harness)" >&2; exit 1 ;;
esac

if [[ "$CALC" == "harness" ]] && ! is_harness_active; then
    echo "Error: Calc engine 'harness' requires execution under the fd-harness supervisor." >&2
    echo "       Run with: uv run fd-harness run $0 [OPTIONS] -c harness" >&2
    exit 1
fi

# Sleep helper dispatching to chosen sleep backend
do_sleep() {
    local dur="$1"
    if [[ "$SLEEP_BACKEND" == "harness" ]]; then
        echo "# @harness.sleep duration=$dur"
        read -r _SLEEP_WAKEUP
    else
        /usr/bin/sleep "$dur"
    fi
}

# Arithmetic helper dispatching to selected calc engine (zero-fork via printf -v when using harness)
calc_to_ns() {
    local _out_var="$1"
    local sec="$2"
    if [[ "$CALC" == "harness" ]]; then
        echo "# @harness.calc expr=\"$sec * 1000000000\""
        read -r _TAG _res
        printf -v "$_out_var" "%.0f" "$_res"
    elif [[ "$CALC" == "awk" ]]; then
        printf -v "$_out_var" "%.0f" "$(awk -v s="$sec" 'BEGIN { printf "%.0f", s * 1000000000 }')"
    else
        printf -v "$_out_var" "%.0f" "$(echo "$sec * 1000000000 / 1" | bc)"
    fi
}

calc_ns_to_sec() {
    local _out_var="$1"
    local ns="$2"
    if [[ "$CALC" == "harness" ]]; then
        echo "# @harness.calc expr=\"$ns / 1000000000\""
        read -r _TAG _res
        printf -v "$_out_var" "%.4f" "$_res"
    elif [[ "$CALC" == "awk" ]]; then
        printf -v "$_out_var" "%.4f" "$(awk -v n="$ns" 'BEGIN { printf "%.4f", n / 1000000000 }')"
    else
        printf -v "$_out_var" "%.4f" "$(echo "scale=4; $ns / 1000000000" | bc)"
    fi
}

calc_diff_ms() {
    local _out_var="$1"
    local actual="$2"
    local theoretical="$3"
    if [[ "$CALC" == "harness" ]]; then
        echo "# @harness.calc expr=\"($actual - $theoretical) / 1000000\""
        read -r _TAG _res
        printf -v "$_out_var" "%.3f" "$_res"
    elif [[ "$CALC" == "awk" ]]; then
        printf -v "$_out_var" "%.3f" "$(awk -v a="$actual" -v t="$theoretical" 'BEGIN { printf "%.3f", (a - t) / 1000000 }')"
    else
        printf -v "$_out_var" "%.3f" "$(echo "scale=3; ($actual - $theoretical) / 1000000" | bc)"
    fi
}

# Parse work duration: support fixed scalar ("0.5", "0") or cyclic pattern ("0.02,0.08,0.04")
IFS=',:' read -r -a WORK_SERIES <<< "$WORK_DURATION"
if (( ${#WORK_SERIES[@]} == 0 )); then
    WORK_SERIES=("0")
fi

# Precompute nanosecond constants (without subshells)
calc_to_ns CADENCE_NS "$CADENCE"
LAST_CYCLE_IDX=$(( (CYCLES - 1) % ${#WORK_SERIES[@]} ))
calc_to_ns LAST_WORK_NS "${WORK_SERIES[LAST_CYCLE_IDX]}"

get_wall_time START_WALL
echo "=== Cadence Drift Benchmark ==="
echo "Strategy:      $STRATEGY"
echo "Reference:     $REF"
echo "Policy:        $POLICY"
echo "Cadence:       ${CADENCE}s"
if (( ${#WORK_SERIES[@]} > 1 )); then
    echo "Work duration: ${WORK_DURATION}s (cyclic pattern of ${#WORK_SERIES[@]} steps)"
else
    echo "Work duration: ${WORK_DURATION}s"
fi
echo "Calc engine:   $CALC"
echo "Sleep backend: $SLEEP_BACKEND"
echo "Date engine:   $DATE_ENGINE"
echo "Cycles:        $CYCLES"
echo "Started at:    $START_WALL"
echo "-------------------------------------------------------------"

simulate_work() {
    local c="$1"
    local dur="${WORK_SERIES[c % ${#WORK_SERIES[@]}]}"
    if (( c == SPIKE_CYCLE )); then
        dur="$SPIKE_DURATION"
        echo "  [!] OVERRUN SPIKE INJECTED: duration=${dur}s (cadence=${CADENCE}s)"
    fi

    # If work duration is zero, no-op immediately (pure cadence pacing, zero fork)
    local dur_ns
    calc_to_ns dur_ns "$dur"
    (( dur_ns <= 0 )) && return 0

    echo "  working for ${dur}s"

    # Workload simulation dispatched to chosen sleep backend
    do_sleep "$dur"
}

# Optional pre-benchmark grid alignment via shift
if [[ -n "$ALIGN" ]]; then
    if ! is_harness_active; then
        echo "Error: Grid alignment (--align / shift) is only supported under fd-harness supervision." >&2
        echo "       The harness provides a neutral, zero-drift comparative baseline to ensure a fair startup." >&2
        echo "       Run with: uv run fd-harness run $0 [OPTIONS] -a \"$ALIGN\"" >&2
        echo "       Or run standalone without -a / --align." >&2
        exit 1
    fi
    echo "# @harness.shift to=\"$ALIGN\""
    read -r _SHIFT_WAKEUP
fi

echo "@@@ CLAP:START @@@"

# Anchor epoch T0 (nanoseconds) at benchmark start (after alignment)
get_time T0_NS

if [[ "$STRATEGY" == "harnessed-clock" ]]; then
    # --------------------------------------------------------------------------
    # Harnessed Clock Strategy: In-shell loop paced by clock:wait with telemetry
    # --------------------------------------------------------------------------
    echo "# @harness.clock:init id=bench interval=$CADENCE cycles=$CYCLES policy=$POLICY"

    while true; do
        echo "# @harness.clock:wait id=bench"
        read -r _TAG ID GRID_CYCLE SKIPPED LAG_MS MONO_TS STATUS

        if [[ "$STATUS" == "done" ]]; then
            break
        fi

        get_wall_time GRID_WALL
        echo "[Cycle $GRID_CYCLE] $GRID_WALL"
        if (( VERBOSE )); then
            echo "  [METROLOGY] wall_date=$GRID_WALL | harness_mono=${MONO_TS}s | lag=${LAG_MS}ms"
        fi
        simulate_work "$GRID_CYCLE"

        if (( SKIPPED > 0 )); then
            echo "  [TELEMETRY] Overrun: skipped $SKIPPED cycle(s) (lag=${LAG_MS}ms)"
        fi
    done
elif [[ "$STRATEGY" == "harnessed-scheduler" ]]; then
    # --------------------------------------------------------------------------
    # Harnessed Scheduler Strategy: Paced by schedule:wait on step expression
    # --------------------------------------------------------------------------
    echo "# @harness.schedule:init id=bench policy=$POLICY"
    echo "# @harness.schedule:rule id=bench expr=\"*/${CADENCE}s\" tags=\"bench\""

    for ((i=0; i<CYCLES; i++)); do
        if (( i > 0 )); then
            echo "# @harness.schedule:wait id=bench"
            read -r _TAG ID SCHEDULED_ISO LAG_MS TAGS STATUS
        fi

        get_wall_time CYCLE_WALL
        echo "[Cycle $i] $CYCLE_WALL"
        if (( VERBOSE && i > 0 )); then
            echo "  [METROLOGY] scheduled=${SCHEDULED_ISO} | lag=${LAG_MS}ms | tags=${TAGS} | status=${STATUS}"
        fi
        simulate_work "$i"
    done
else
    # --------------------------------------------------------------------------
    # In-Shell Strategies: Unified loop using Cartesian (Reference x Strategy)
    # --------------------------------------------------------------------------
    for ((i=0; i<CYCLES; i++)); do
        get_time cycle_start_ns cycle_wall
        echo "[Cycle $i] $cycle_wall"

        # 1. Determine next target timestamp based on Reference Anchor
        if [[ "$REF" == "last" ]]; then
            next_target_ns=$(( cycle_start_ns + CADENCE_NS ))
        else
            next_target_ns=$(( T0_NS + (i + 1) * CADENCE_NS ))
        fi

        # 2. Execute workload
        simulate_work "$i"

        # 3. Wait until next target tick using chosen Strategy
        if (( i < CYCLES - 1 )); then
            case "$STRATEGY" in
                blind)
                    do_sleep "$CADENCE"
                    ;;

                computed|harnessed-timer)
                    get_time work_done_ns
                    remaining_ns=$(( next_target_ns - work_done_ns ))

                    # Overrun handling with policy
                    if (( remaining_ns <= 0 )) && [[ "$POLICY" == "skip" ]]; then
                        overrun_ns=$(( work_done_ns - next_target_ns ))
                        skipped=$(( (overrun_ns / CADENCE_NS) + 1 ))
                        next_target_ns=$(( next_target_ns + skipped * CADENCE_NS ))
                        remaining_ns=$(( next_target_ns - work_done_ns ))
                        calc_diff_ms lag_ms "$work_done_ns" "$(( next_target_ns - skipped * CADENCE_NS ))"
                        echo "  [TELEMETRY] Overrun: skipped $skipped cycle(s) (lag=${lag_ms}ms)"
                    fi

                    if (( remaining_ns > 0 )); then
                        calc_ns_to_sec sleep_sec "$remaining_ns"
                        if [[ "$STRATEGY" == "harnessed-timer" || "$SLEEP_BACKEND" == "harness" ]]; then
                            echo "# @harness.sleep duration=$sleep_sec"
                            read -r _WAKEUP
                        else
                            /usr/bin/sleep "$sleep_sec"
                        fi
                    fi
                    ;;

                polling)
                    while true; do
                        get_time poll_now_ns
                        (( poll_now_ns >= next_target_ns )) && break
                        do_sleep "$POLL_STEP"
                    done
                    ;;

                harnessed-shift)
                    echo "# @harness.shift to=\"*/${CADENCE}s\""
                    read -r _WAKEUP
                    ;;
            esac
        fi
    done
fi

echo "@@@ CLAP:END @@@"

# Final benchmark report
get_time T_END_NS END_WALL
TOTAL_ACTUAL_NS=$(( T_END_NS - T0_NS ))
TOTAL_THEORETICAL_NS=$(( (CYCLES - 1) * CADENCE_NS + LAST_WORK_NS ))
calc_diff_ms NET_DRIFT_MS "$TOTAL_ACTUAL_NS" "$TOTAL_THEORETICAL_NS"
calc_ns_to_sec THEO_SEC "$TOTAL_THEORETICAL_NS"
calc_ns_to_sec ACTUAL_SEC "$TOTAL_ACTUAL_NS"

echo "-------------------------------------------------------------"
echo "Finished at:            $END_WALL"
echo "Theoretical duration:   ${THEO_SEC}s"
echo "Actual duration:        ${ACTUAL_SEC}s"
printf "Net cumulative drift:   %+.3f ms\n" "$NET_DRIFT_MS"
echo "============================================================="
