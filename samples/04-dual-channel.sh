#!/usr/bin/env bash
# ==============================================================================
# 04-dual-channel.sh - Dual-Channel Architecture (FTP-like)
#
# Shows:
#   1. Event Stream Plane on dedicated FD 3: # @harness.timer:interval id=pulse every=0.3s on_fd=3
#   2. Control RPC Plane on stdin/stdout (FD 0/1): # @harness.clock:status
#   3. Zero collision: RPC commands executed while ticks stream on FD 3!
# ==============================================================================
set -euo pipefail

echo "=== Dual-Channel (Control vs Event Stream) Demo ==="
echo "Started at: $(date +%T.%3N)"

# 1. Start a high-speed background pulse on the dedicated Event Stream (FD 3)
echo "# @harness.timer:interval id=pulse every=0.2s on_fd=3"

# 2. Consume 3 ticks from the Event Channel (FD 3)
for ((i=0; i<3; i++)); do
    read -u 3 -r _TAG TIMER_ID CYCLE LAG_MS
    echo "[FD 3 Event] Received tick $CYCLE from $TIMER_ID (lag=${LAG_MS}ms) at $(date +%T.%3N)"
done

# 3. In parallel, issue a synchronous RPC command on the Control Channel (stdin/stdout)
echo "-------------------------------------------------------------"
echo "[Control RPC] Querying clock:list on standard stdin/stdout..."
echo "# @harness.clock:list"
read -r _TAG CLOCKS COUNT
echo "[Control RPC] Response received without interference! Clocks: $CLOCKS (count=$COUNT)"

# 4. Consume 2 more ticks from FD 3 to prove the stream was unhindered
echo "-------------------------------------------------------------"
for ((i=0; i<2; i++)); do
    read -u 3 -r _TAG TIMER_ID CYCLE LAG_MS
    echo "[FD 3 Event] Continued tick $CYCLE from $TIMER_ID at $(date +%T.%3N)"
done

# 5. Stop the timer
echo "# @harness.timer:cancel id=pulse"
echo "============================================================="
echo "Finished cleanly at $(date +%T.%3N)"
