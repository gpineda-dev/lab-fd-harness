#!/usr/bin/env bash
# ==============================================================================
# run-demo.sh - Demonstrates in-band stdout directive interception
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

chmod +x "${SCRIPT_DIR}/provision-worker.sh"

echo "========================================================================"
echo "1. Exécution DIRECTE (Sans fd-harness) - Les secrets fuitent en clair"
echo "========================================================================"
"${SCRIPT_DIR}/provision-worker.sh"

echo ""
echo "========================================================================"
echo "2. Exécution SOUS fd-harness (Interception in-band sur stdout)"
echo "========================================================================"
echo "Commande: python3 -m harness.cli run ${SCRIPT_DIR}/provision-worker.sh"
echo "------------------------------------------------------------------------"
PYTHONPATH="${ROOT_DIR}" python3 -m harness.cli run "${SCRIPT_DIR}/provision-worker.sh"

echo ""
echo "========================================================================"
echo "3. Ce qui s'est produit :"
echo "   - Les lignes '# @harness.filter:mask...' ont été consommées et masquées."
echo "   - 'sec_8819ab21' a été pseudonymisé en 'tok_01' (1:1 bijective)."
echo "   - 'sk_live_9948ab12cf345678' a été haché en 'sk_<hash>'."
echo "   - La réutilisation de 'sec_8819ab21' redonne fidèlement 'tok_01'."
echo "========================================================================"

