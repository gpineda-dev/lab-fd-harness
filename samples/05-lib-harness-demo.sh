#!/usr/bin/env bash
# ==============================================================================
# Sample 05: Clean Shell SDK with lib_harness.sh
# Demonstrates clean while-loop cadence and dynamic update without raw directives.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../harness/lib_harness.sh"

echo "[SH] Initializing clock 'cadence' for 5 cycles at 100ms..."
harness_clock_init "cadence" "100ms" 5 "skip"

echo "[SH] Entering loop with 'while harness_clock_wait'..."
while harness_clock_wait "cadence"; do
    echo "[SH] Tick received! Cycle=${HARNESS_CYCLE} Lag=${HARNESS_LAG_MS}ms Status=${HARNESS_STATUS}"
    
    # Simulate dynamic update on cycle 3: slow down to 200ms
    if [[ "$HARNESS_CYCLE" -eq 3 ]]; then
        echo "[SH] Dynamic update: slowing down interval to 200ms"
        harness_clock_update "cadence" "interval=200ms"
    fi
done

echo "[SH] Loop exited cleanly. Final cycle reached: ${HARNESS_CYCLE}"
