#!/usr/bin/env bash
# consumer.sh - Subscribes to the coordinator in-memory event bus on FD 3

echo "[CONSUMER] Subscribing to bus topic 'telemetry:metric'..."
echo "# @harness.bus:subscribe topic='telemetry:metric'"

echo "[CONSUMER] Waiting for 3 events from peers on FD 3..."
count=0
while [ $count -lt 3 ]; do
    if read -u 3 -r tag topic payload; then
        if [ "$tag" = "bus:event" ]; then
            count=$((count + 1))
            echo "[CONSUMER] Received event #$count on topic '$topic' -> payload: '$payload'"
        fi
    fi
done

echo "[CONSUMER] Finished receiving 3 events. Exiting cleanly."
