#!/usr/bin/env bash
# ==============================================================================
# clock-tick.sh - Metrology Benchmark: Cartesian Product of Cadence & Reference
#
# Orthogonal Dimensions:
#   Dimension 1: Reference Anchor (--ref=init | last)
#     - init: Absolute epoch T0 (self-correcting clock, memory of past delays)
#     - last: Relative to previous cycle start (memoryless, cumulative drift)
#
#   Dimension 2: Waiting Mechanism (--strategy=blind | computed | polling | harnessed-fork | harnessed-pure)
#     - blind:          Sleeps CADENCE ignoring work duration
#     - computed:       Measures remaining delta to target and sleeps delta
#     - polling:        Micro-sleeps in a loop until target is crossed
#     - harnessed-fork: Cadence driven by external supervisor, work in shell
#     - harnessed-pure: Cadence and work sleep delegated to external supervisor
# ==============================================================================
set -euo pipefail

# Default parameters
STRATEGY="computed"
REF="init"
CADENCE=2.0
WORK_DURATION=0.5
POLL_STEP=0.05
CYCLES=5
CALC="bc"  # bc or awk
SPIKE_CYCLE=-1
SPIKE_DURATION=1.5
VERBOSE=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Dimensions:
  -s, --strategy=STRATEGY   blind, computed, polling, harnessed-fork, harnessed-pure, harnessed-pull (default: computed)
  -r, --ref=ANCHOR          init (T0 absolute) or last (cycle-relative) (default: init)

Timing Parameters:
  -i, --cadence=SEC         Target cadence interval in seconds (default: 2.0)
  -w, --work-duration=SEC   Simulated work duration in seconds (default: 0.5)
  -p, --poll-step=SEC       Polling step in seconds for polling strategy (default: 0.05)
  -n, --cycles=N            Number of cycles (default: 5)
  -c, --calc=ENGINE         Math engine for calculations: bc or awk (default: bc)
  -v, --verbose             Print detailed metrology telemetry (wall date vs monotonic ts)

Spike Injection (Overrun Testing):
  --spike-cycle=N           Cycle index where a latency spike occurs (default: -1, disabled)
  --spike-duration=SEC      Duration of the latency spike in seconds (default: 1.5)
  -h, --help                Show this help message
EOF
    exit 1
}

# Standard bash builtin getopts supporting short flags and --long-options
while getopts "s:r:i:w:p:n:c:vh-:" opt; do
    if [[ "$opt" == "-" ]]; then
        case "$OPTARG" in
            strategy=*) STRATEGY="${OPTARG#*=}" ;;
            ref=*) REF="${OPTARG#*=}" ;;
            cadence=*) CADENCE="${OPTARG#*=}" ;;
            work-duration=*) WORK_DURATION="${OPTARG#*=}" ;;
            poll-step=*) POLL_STEP="${OPTARG#*=}" ;;
            cycles=*) CYCLES="${OPTARG#*=}" ;;
            calc=*) CALC="${OPTARG#*=}" ;;
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
            v) VERBOSE=1 ;;
            h) usage ;;
            *) usage ;;
        esac
    fi
done
shift $((OPTIND - 1))

# Validation
case "$STRATEGY" in
    blind|computed|polling|harnessed-fork|harnessed-pure|harnessed-pull) ;;
    *) echo "Invalid strategy: $STRATEGY (choose blind, computed, polling, harnessed-fork, harnessed-pure, harnessed-pull)" >&2; exit 1 ;;
esac

case "$REF" in
    init|last) ;;
    *) echo "Invalid ref: $REF (choose init or last)" >&2; exit 1 ;;
esac

case "$CALC" in
    bc|awk) ;;
    *) echo "Invalid calc engine: $CALC (choose bc or awk)" >&2; exit 1 ;;
esac

# Arithmetic helper dispatching to selected calc engine
calc_to_ns() {
    local sec="$1"
    if [[ "$CALC" == "awk" ]]; then
        awk -v s="$sec" 'BEGIN { printf "%.0f", s * 1000000000 }'
    else
        echo "$sec * 1000000000 / 1" | bc
    fi
}

calc_ns_to_sec() {
    local ns="$1"
    if [[ "$CALC" == "awk" ]]; then
        awk -v n="$ns" 'BEGIN { printf "%.4f", n / 1000000000 }'
    else
        echo "scale=4; $ns / 1000000000" | bc
    fi
}

calc_diff_ms() {
    local actual="$1"
    local theoretical="$2"
    if [[ "$CALC" == "awk" ]]; then
        awk -v a="$actual" -v t="$theoretical" 'BEGIN { printf "%.3f", (a - t) / 1000000 }'
    else
        echo "scale=3; ($actual - $theoretical) / 1000000" | bc
    fi
}

# Precompute nanosecond constants
CADENCE_NS=$(calc_to_ns "$CADENCE")
WORK_NS=$(calc_to_ns "$WORK_DURATION")

echo "=== Cadence Drift Benchmark ==="
echo "Strategy:      $STRATEGY"
echo "Reference:     $REF"
echo "Cadence:       ${CADENCE}s"
echo "Work duration: ${WORK_DURATION}s"
echo "Calc engine:   $CALC"
echo "Cycles:        $CYCLES"
echo "Started at:    $(date +"%T.%3N")"
echo "-------------------------------------------------------------"

# Anchor epoch T0 (nanoseconds)
T0_NS=$(date +%s%N)

simulate_work() {
    local c="$1"
    local dur="$WORK_DURATION"
    if (( c == SPIKE_CYCLE )); then
        dur="$SPIKE_DURATION"
        echo "  [!] OVERRUN SPIKE INJECTED: duration=${dur}s (cadence=${CADENCE}s)"
    fi

    if [[ "$STRATEGY" == "harnessed-pure" || "$STRATEGY" == "harnessed-pull" ]]; then
        # Pure mode: work sleep delegated to the harness (zero subshell fork)
        echo "# @harness.sleep duration=$dur"
        read -r _WAKEUP
    else
        # Standard work: simulated by /usr/bin/sleep
        /usr/bin/sleep "$dur"
    fi
}

echo "@@@ CLAP:START @@@"

if [[ "$STRATEGY" == "harnessed-pull" ]]; then
    # --------------------------------------------------------------------------
    # Harnessed Pull Strategy: In-shell loop paced by clock:wait with telemetry
    # --------------------------------------------------------------------------
    echo "# @harness.clock:init id=bench interval=$CADENCE cycles=$CYCLES policy=skip"

    while true; do
        echo "# @harness.clock:wait id=bench"
        read -r _TAG ID GRID_CYCLE SKIPPED LAG_MS MONO_TS STATUS

        if [[ "$STATUS" == "done" ]]; then
            break
        fi

        echo "[Cycle $GRID_CYCLE] $(date +"%T.%3N")"
        if (( VERBOSE )); then
            echo "  [METROLOGY] wall_date=$(date +"%T.%3N") | harness_mono=${MONO_TS}s | lag=${LAG_MS}ms"
        fi
        simulate_work "$GRID_CYCLE"

        if (( SKIPPED > 0 )); then
            echo "  [TELEMETRY] Overrun: skipped $SKIPPED cycle(s) (lag=${LAG_MS}ms)"
        fi
    done
elif [[ "$STRATEGY" == "harnessed-fork" || "$STRATEGY" == "harnessed-pure" ]]; then
    # --------------------------------------------------------------------------
    # Harnessed Push Strategies: Cadence paced by external supervisor via stdin
    # --------------------------------------------------------------------------
    echo "# @harness.clock:every interval=$CADENCE cycles=$CYCLES"

    cycle=0
    while read -r _TAG TICK_CYCLE MONO_TS _REST; do
        echo "[Cycle $cycle] $(date +"%T.%3N")"
        if (( VERBOSE )); then
            if [[ -n "${MONO_TS:-}" ]]; then
                echo "  [METROLOGY] wall_date=$(date +"%T.%3N") | harness_mono=${MONO_TS}s | lag=0.000ms"
            fi
        fi
        simulate_work "$cycle"

        cycle=$(( cycle + 1 ))
        if (( cycle >= CYCLES )); then
            break
        fi
    done
else
    # --------------------------------------------------------------------------
    # In-Shell Strategies: Unified loop using Cartesian (Reference x Strategy)
    # --------------------------------------------------------------------------
    for ((i=0; i<CYCLES; i++)); do
        echo "[Cycle $i] $(date +"%T.%3N")"
        cycle_start_ns=$(date +%s%N)

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
                    /usr/bin/sleep "$CADENCE"
                    ;;

                computed)
                    work_done_ns=$(date +%s%N)
                    remaining_ns=$(( next_target_ns - work_done_ns ))
                    if (( remaining_ns > 0 )); then
                        sleep_sec=$(calc_ns_to_sec "$remaining_ns")
                        /usr/bin/sleep "$sleep_sec"
                    fi
                    ;;

                polling)
                    while (( $(date +%s%N) < next_target_ns )); do
                        /usr/bin/sleep "$POLL_STEP"
                    done
                    ;;
            esac
        fi
    done
fi

echo "@@@ CLAP:END @@@"

# Final benchmark report
T_END_NS=$(date +%s%N)
TOTAL_ACTUAL_NS=$(( T_END_NS - T0_NS ))
TOTAL_THEORETICAL_NS=$(( (CYCLES - 1) * CADENCE_NS + WORK_NS ))
NET_DRIFT_MS=$(calc_diff_ms "$TOTAL_ACTUAL_NS" "$TOTAL_THEORETICAL_NS")
THEO_SEC=$(calc_ns_to_sec "$TOTAL_THEORETICAL_NS")
ACTUAL_SEC=$(calc_ns_to_sec "$TOTAL_ACTUAL_NS")

echo "-------------------------------------------------------------"
echo "Finished at:            $(date +"%T.%3N")"
echo "Theoretical duration:   ${THEO_SEC}s"
echo "Actual duration:        ${ACTUAL_SEC}s"
printf "Net cumulative drift:   %+.3f ms\n" "$NET_DRIFT_MS"
echo "============================================================="
