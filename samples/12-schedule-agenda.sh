#!/usr/bin/env bash
# ==============================================================================
# 12-schedule-agenda.sh - Multi-Rule Schedule & Resume Catchup Demonstration
# ==============================================================================
# Demonstrates:
#   1. Multi-rule agenda attaching independent cron/grid rules to a single schedule.
#   2. Comma-separated tags for shell routing via \`case ",$HARNESS_TAGS," in\`.
#   3. State checkpoint persistence and deterministic catchup replay upon resume.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source the fd-harness SDK
source "${REPO_ROOT}/harness/lib_harness.sh"

STATE_FILE="/tmp/demo-schedule-state.json"
COUNT=0
MAX_EVENTS=5

echo "=== [Worker] Starting Schedule Agenda Demo ==="
echo "=== [Worker] State checkpoint: ${STATE_FILE} ==="

# Initialize schedule with catchup policy and persistence
harness_schedule_init "agenda" "catchup" "${STATE_FILE}"

# Attach multiple rules with descriptive tags
harness_schedule_rule "agenda" "*/1s" "metrics,ping"
harness_schedule_rule "agenda" "*/3s" "backup,heavy"

echo "=== [Worker] Rules configured: */1s (metrics,ping) and */3s (backup,heavy) ==="

while harness_schedule_wait "agenda"; do
    COUNT=$((COUNT + 1))
    echo "[${HARNESS_STATUS}] at ${HARNESS_SCHEDULED} (lag: ${HARNESS_LAG_MS}ms) tags=[${HARNESS_TAGS}]"

    # Multi-tag shell dispatch pattern
    case ",${HARNESS_TAGS}," in
        *,metrics,*)
            echo "  -> [ACTION] Collecting metrics..."
            ;;
    esac

    case ",${HARNESS_TAGS}," in
        *,backup,*)
            echo "  -> [ACTION] Performing periodic backup..."
            ;;
    esac

    if [[ "$COUNT" -ge "$MAX_EVENTS" ]]; then
        echo "=== [Worker] Reached ${MAX_EVENTS} events, dumping state snapshot and exiting ==="
        harness_schedule_dump "agenda" "${STATE_FILE}"
        break
    fi
done

echo "=== [Worker] State file content: ==="
cat "${STATE_FILE}"
echo ""
echo "=== [Worker] Done ==="
