#!/usr/bin/env bash
# ==============================================================================
# 01-event-timers.sh - Demonstration of Event-Driven Timers via fd-harness
#
# Shows:
#   1. Periodic timer (setInterval): # @harness.timer:interval id=heartbeat every=1.0s
#   2. One-shot timer (setTimeout):  # @harness.timer:once id=alarm after=3.5s
#   3. Universal Event Loop dispatcher in pure Bash:
#      while read -r KIND ID PAYLOAD; do case "$KIND:$ID" in ... esac; done
#   4. Dynamic timer cancellation:   # @harness.timer:cancel id=heartbeat
# ==============================================================================
set -euo pipefail

echo "=== Event-Driven Timers Demo ==="
echo "Started at $(date +%T.%3N)"

# Schedule a recurring heartbeat (setInterval)
echo "# @harness.timer:interval id=heartbeat every=1.0s"

# Schedule a one-shot watchdog / alarm (setTimeout)
echo "# @harness.timer:once id=alarm after=3.5s"

# Universal Event Loop in Bash
while read -r EVENT_TYPE EVENT_ID PAYLOAD; do
    case "$EVENT_TYPE:$EVENT_ID" in
        "timer:tick:heartbeat")
            echo "[$(date +%T.%3N)] <EVENT> Heartbeat tick: $PAYLOAD"
            ;;

        "timer:timeout:alarm")
            echo "[$(date +%T.%3N)] <EVENT> ALARM! One-shot timer fired!"
            echo "[$(date +%T.%3N)] Cancelling recurring heartbeat..."
            echo "# @harness.timer:cancel id=heartbeat"
            echo "[$(date +%T.%3N)] Exiting event loop cleanly."
            break
            ;;

        *)
            echo "[$(date +%T.%3N)] Unknown event: $EVENT_TYPE $EVENT_ID $PAYLOAD"
            ;;
    esac
done

echo "Finished at $(date +%T.%3N)"
