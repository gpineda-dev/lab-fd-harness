#!/usr/bin/env bash
# ==============================================================================
# 02-wait-telemetry.sh - Pull Cadence Synchronization with In-Band Telemetry
#
# Shows:
#   1. Clean while-true loop paced entirely by # @harness.clock:wait
#   2. Bounded clock: cycles=5 (total time strictly bounded to 5 x 0.5s = 2.5s)
#   3. Automatic status=done termination when grid horizon is reached
#   4. Dynamic telemetry: Positional columns, ZERO eval!
# ==============================================================================
set -euo pipefail

CADENCE=0.5
CYCLES=5

echo "=== Pull Cadence Telemetry Demo ==="
echo "Cadence:       ${CADENCE}s"
echo "Max Cycles:    $CYCLES"
echo "Started at:    $(date +%T.%3N)"

# Initialize clock reference grid bounded to CYCLES
echo "# @harness.clock:init id=main interval=$CADENCE cycles=$CYCLES policy=skip"

while true; do
    echo "# @harness.clock:wait id=main"
    read -r _TAG ID GRID_CYCLE SKIPPED LAG_MS MONO_TS STATUS

    if [[ "$STATUS" == "done" ]]; then
        echo "-------------------------------------------------------------"
        echo "Mission Finished: Horizon of $CYCLES cycles reached (terminal grid cycle=$GRID_CYCLE)."
        break
    fi

    echo "-------------------------------------------------------------"
    echo "[Cycle $GRID_CYCLE] Started work at $(date +%T.%3N)"

    # Simulate an overrun spike at cycle 2
    if (( GRID_CYCLE == 2 )); then
        echo "  [!] INJECTING 0.8s OVERRUN SPIKE (Cadence is ${CADENCE}s)..."
        echo "# @harness.sleep duration=0.8"
        read -r _WAKEUP
    else
        echo "# @harness.sleep duration=0.1"
        read -r _WAKEUP
    fi

    echo "[Cycle $GRID_CYCLE] Work finished at $(date +%T.%3N)"

    if (( SKIPPED > 0 )); then
        echo "  [TELEMETRY ALERT] $SKIPPED cycle(s) were lost during overrun! Status=$STATUS, Lag=${LAG_MS}ms"
    else
        echo "  [TELEMETRY OK] Status=$STATUS, Lag=${LAG_MS}ms"
    fi
done

echo "============================================================="
echo "Finished cleanly at $(date +%T.%3N)"
