#!/usr/bin/env bash
# ==============================================================================
# provision-worker.sh - Sample deployment script with in-band @harness directives
# Featured in the blog post: "L'Exosquelette du Bâtisseur & Le Superviseur de Flux"
# ==============================================================================

echo "==> [INIT] Starting cluster provisioning..."

# 1. In-band directive emitted to stdout for 1:1 secret pseudonymization
echo '# @harness.filter:mask pattern="sec_[a-z0-9]{8}" action="alias" template="tok_{seq:02d}"'

# 2. In-band directive to hash static production API keys
echo '# @harness.filter:mask pattern="sk_live_[a-z0-9]{16}" action="hash" template="sk_{hash:8}"'

echo "==> [AUTH] Connecting to primary cluster with secret : sec_8819ab21"
echo "==> [API]  Verifying license with API key : sk_live_9948ab12cf345678"
echo "==> [AUTH] Refreshing session for backup key : sec_4410cd99"
echo "==> [AUTH] Reusing first session secret : sec_8819ab21"
echo "==> [DONE] Provisioning completed."
