#!/bin/bash
# Deploy SSH keys to all nodes
# Requires: chris user with sudo access on all nodes

set -e

SCRIPT_DIR="$(dirname "$0")"
NODES="dks01.schollar.dev dks02.schollar.dev dks03.schollar.dev dks04.schollar.dev dks05.schollar.dev"

echo "╔══════════════════════════════════════════════════════════════════════════════╗"
echo "║              Deploy Ansible SSH Keys to All Nodes                            ║"
echo "╚══════════════════════════════════════════════════════════════════════════════╝"
echo ""
echo "This will deploy the Ansible SSH key to root@<node> using chris user's sudo access"
echo ""
echo "Nodes to configure:"
for NODE in $NODES; do
    echo "  • $NODE"
done
echo ""
read -p "Press Enter to continue or Ctrl+C to cancel..."
echo ""

SUCCESS_COUNT=0
FAIL_COUNT=0
FAILED_NODES=()

for NODE in $NODES; do
    echo "═══════════════════════════════════════════════════════════════════════════"
    echo "Deploying to: $NODE"
    echo "═══════════════════════════════════════════════════════════════════════════"

    if "$SCRIPT_DIR/deploy-key-to-node.sh" "$NODE"; then
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        FAILED_NODES+=("$NODE")
    fi
    echo ""
done

echo "═══════════════════════════════════════════════════════════════════════════"
echo "Deployment Summary"
echo "═══════════════════════════════════════════════════════════════════════════"
echo "Success: $SUCCESS_COUNT"
echo "Failed:  $FAIL_COUNT"

if [ $FAIL_COUNT -gt 0 ]; then
    echo ""
    echo "Failed nodes:"
    for NODE in "${FAILED_NODES[@]}"; do
        echo "  ✗ $NODE"
    done
fi

echo ""
if [ $FAIL_COUNT -eq 0 ]; then
    echo "✓ All keys deployed successfully!"
    echo ""
    echo "Next step:"
    echo "  cd $(dirname "$SCRIPT_DIR") && ansible-playbook -i inventory.yml linstor-backup/playbook.yml"
else
    echo "✗ Some deployments failed. Please check the errors above."
    exit 1
fi
