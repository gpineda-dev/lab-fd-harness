#!/usr/bin/env bash
# run-demo.sh - Comprehensive demo for enriched DLP (fd-harness dlp redact, fd-harness dlp unmask)

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHONPATH="$(cd "$DIR/../.." && pwd)"
export PYTHONPATH

VAULT_FILE="$DIR/vault.jsonl"
rm -f "$VAULT_FILE"

echo "========================================================================"
echo "1. Stream Sanitization via 'fd-harness dlp redact' (alias, hash, mask)"
echo "========================================================================"
python3 -m harness.cli dlp redact --rules "$DIR/dlp-rules.toml" --vault "$VAULT_FILE" < "$DIR/demo-server-logs.txt" > "$DIR/sanitized.log"

echo ""
echo "--- Sanitized Output (sanitized.log) ---"
cat "$DIR/sanitized.log"

echo ""
echo "========================================================================"
echo "2. Generated BiMap Vault (vault.jsonl - Append-Only WAL without redundancy)"
echo "========================================================================"
cat "$VAULT_FILE"
echo ""

echo ""
echo "========================================================================"
echo "3. Reversible Unmasking in Secured Enclave via 'fd-harness dlp unmask'"
echo "========================================================================"
python3 -m harness.cli dlp unmask --vault "$VAULT_FILE" "$DIR/sanitized.log" > "$DIR/restored.log"

echo "--- Restored Output (restored.log) ---"
cat "$DIR/restored.log"

echo ""
echo "--- Diff between Original and Restored (aliases restored loss-free) ---"
diff -u "$DIR/demo-server-logs.txt" "$DIR/restored.log" || true
echo "Note: mask & hash are one-way (unmasked as-is), while alias rules (CUST-*, 10.0.0.*) are 100% restored!"

echo ""
echo "========================================================================"
echo "4. Dynamic In-Band Redaction Directive (# @harness.filter:mask)"
echo "========================================================================"
cat << 'EOF' | python3 -m harness.cli dlp redact
# @harness.filter:mask pattern="worker-[0-9]+" action="alias" template="srv_{seq.node:02d}" name="node"
Starting job on worker-99
Connecting to database from worker-99
Spawning secondary task on worker-12
Worker worker-99 task finished cleanly
EOF

# Clean up temp outputs
rm -f "$DIR/sanitized.log" "$DIR/restored.log" "$VAULT_FILE"
echo ""
echo "Demo finished successfully."
