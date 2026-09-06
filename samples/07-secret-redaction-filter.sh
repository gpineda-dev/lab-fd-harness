#!/usr/bin/env bash
# ==============================================================================
# Sample 07: Live Secret Redaction & Output Filtering
# Demonstrates how python supervisor proxies stdout, redacting sensitive tokens
# while silencing # @harness directives and formatting supervisor logs.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../harness/lib_harness.sh"

echo "=== Security Filter & Output Proxy Demo ==="

# 1. Register redaction masks for GitHub tokens and Bearer tokens
# Notice: These directives are intercepted by Python and NEVER printed on terminal!
harness_filter_mask "ghp_[a-zA-Z0-9]{20,}" "[REDACTED_GH_TOKEN]"
harness_filter_mask "ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}" "[REDACTED_JWT_TOKEN]"

# 2. Print structured supervisor log with interpolated expression
harness_log "Security firewall activated. Monitoring child stdout..." "SECURITY"

# 3. Simulate accidental secret leaks by bash commands or sub-processes
echo "Normal bash output: Starting sync process..."
echo "Simulating curl output with GitHub PAT: Authorization: token ghp_ABCDefgh1234567890abcdef1234567890"
echo "Simulating env dump: JWT_SECRET=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
echo "Normal bash output: Sync complete."

# 4. Supervisor log with computation
harness_log "Completed audit at cycle=[1 + 0] with [100ms / 2]ms budget" "AUDIT"

echo "=== Demo completed successfully! ==="
