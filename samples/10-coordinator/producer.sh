#!/usr/bin/env bash
# producer.sh - Publishes events onto the coordinator in-memory event bus

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/../../harness/lib_harness.sh"

echo "[PRODUCER] Starting producer using deterministic 100ms clock..."
echo "# @harness.clock:init id=prod interval=100ms cycles=3"
echo "# @harness.clock:start id=prod"

for cycle in 1 2 3; do
    echo "# @harness.clock:wait id=prod"
    read -r status id c s lag ts st
    echo "[PRODUCER] Pulsing metric #$cycle at tick=$c..."
    echo "# @harness.bus:emit topic='telemetry:metric' payload='sensor=temp_cpu value=4${cycle}.5'"
done

echo "[PRODUCER] Completed 3 emissions. Exiting cleanly."
