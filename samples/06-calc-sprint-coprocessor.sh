#!/usr/bin/env bash
# ==============================================================================
# Sample 06: Pratt Coprocessor (calc & sprint string interpolation)
# Demonstrates zero-fork arithmetic, physical units, and bracketed templating.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../harness/lib_harness.sh"

echo "=== Pratt Arithmetic & Sprint Coprocessor Demo ==="

# 1. Pure float calculation without bc or subshells
BASH_VAR=6
echo "[SH] Requesting calc for '5 * ${BASH_VAR}'..."
harness_calc "5 * ${BASH_VAR}"
echo "[SH] Result received: HARNESS_VAL=${HARNESS_VAL}"

# 2. Float division and unit resolution
echo "[SH] Requesting calc for '1s / 60' (60 FPS delta)..."
harness_calc "1s / 60"
echo "[SH] 60 FPS delta: HARNESS_VAL=${HARNESS_VAL}s"

# 3. String interpolation with [brackets] via sprint
echo "[SH] Requesting sprint interpolation..."
harness_sprint "Transaction id=[2 ** 8] latency=[100ms * 2 + 50ms] status=ok"
echo "[SH] Rendered string: ${HARNESS_VAL}"

echo "=== Demo completed successfully! ==="
