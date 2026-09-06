#!/usr/bin/env bash
# ==============================================================================
# 03-dynamic-clock.sh - Explicit clock:start, Dynamic clock:update, and Introspection
#
# Shows:
#   1. Definition without starting: # @harness.clock:init id=main interval=0.3
#   2. Heavy setup work before loop without stealing time from the clock.
#   3. Explicit anchor: # @harness.clock:start id=main
#   4. Dynamic Backoff: At cycle 2, slow down cadence: # @harness.clock:update interval=0.8
#   5. Dynamic Early Exit: At cycle 4, stop mission: # @harness.clock:update expire_at=now
#   6. Live Introspection: Query clock:status and clock:list with clean positional read.
# ==============================================================================
set -euo pipefail

echo "=== Dynamic Clock Lifecycle Demo ==="

# 1. Define clock
echo "# @harness.clock:init id=main interval=0.3 policy=skip"
echo "[Setup] Preparing environment (simulated delay)..."
/usr/bin/sleep 0.3
echo "[Setup] Ready!"

# Introspection: List registered clocks
echo "# @harness.clock:list"
read -r _TAG CLOCKS_LIST COUNT
echo "[Introspection] Registered clocks ($COUNT): $CLOCKS_LIST"

# 2. Explicitly anchor T0 right before starting the loop
echo "# @harness.clock:start id=main"
echo "[Start] Clock started at $(date +%T.%3N)"

while true; do
    echo "# @harness.clock:wait id=main"
    read -r _TAG ID GRID_CYCLE SKIPPED LAG_MS MONO_TS STATUS

    if [[ "$STATUS" == "done" ]]; then
        echo "-------------------------------------------------------------"
        echo "[Exit] Mission terminated dynamically! (terminal grid cycle=$GRID_CYCLE)"
        break
    fi

    echo "-------------------------------------------------------------"
    echo "[Cycle $GRID_CYCLE] Tick received at $(date +%T.%3N) (status=$STATUS, lag=${LAG_MS}ms)"

    # Dynamic Mutation 1: At cycle 2, slow down cadence from 0.3s to 0.8s (Backoff)
    if (( GRID_CYCLE == 2 )); then
        echo "  [MUTATION] High load detected! Slowing cadence down: interval=0.8s"
        echo "# @harness.clock:update id=main interval=0.8"
    fi

    # Dynamic Mutation 2: At cycle 4, request immediate clean termination
    if (( GRID_CYCLE == 4 )); then
        echo "  [MUTATION] Threshold reached! Requesting immediate clean exit..."
        echo "# @harness.clock:update id=main expire_at=now"
    fi

    # Small simulated work
    echo "# @harness.sleep duration=0.05"
    read -r _WAKEUP
done

# Post-run Introspection: Query final clock status
echo "# @harness.clock:status id=main"
read -r _TAG ID INTERVAL ELAPSED LAST_CYCLE MAX_CYCLES STATUS
echo "============================================================="
echo "[Final Status] Clock $ID: interval=${INTERVAL}s, elapsed=${ELAPSED}s, cycles=$LAST_CYCLE, status=$STATUS"
echo "Finished at $(date +%T.%3N)"
