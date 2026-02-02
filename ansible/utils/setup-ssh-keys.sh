#!/bin/bash
# Test SSH access to all nodes as root using the Ansible SSH key

set -e

SCRIPT_DIR="$(dirname "$0")"
ANSIBLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NODES="dks01.schollar.dev dks02.schollar.dev dks03.schollar.dev dks04.schollar.dev dks05.schollar.dev"

echo "╔══════════════════════════════════════════════════════════════════════════════╗"
echo "║              Test SSH Access to All Nodes                                    ║"
echo "╚══════════════════════════════════════════════════════════════════════════════╝"
echo ""
echo "Testing SSH access as root using Ansible SSH key..."
echo ""

SUCCESS_COUNT=0
FAIL_COUNT=0

for NODE in $NODES; do
    echo -n "Testing root@$NODE... "
    if ssh -i "$ANSIBLE_DIR/ansible_ssh_key" -o BatchMode=yes -o ConnectTimeout=5 root@"$NODE" "echo OK" 2>/dev/null; then
        echo "✓ SUCCESS"
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    else
        echo "✗ FAILED"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "Results: $SUCCESS_COUNT/$((SUCCESS_COUNT + FAIL_COUNT)) nodes accessible"
echo "═══════════════════════════════════════════════════════════════════════════"

if [ $FAIL_COUNT -eq 0 ]; then
    echo ""
    echo "✓ All nodes are accessible!"
    echo ""
    echo "You can now run the Ansible playbook:"
    echo "  cd $ANSIBLE_DIR && ansible-playbook -i inventory.yml linstor-backup/playbook.yml"
else
    echo ""
    echo "✗ Some nodes are not accessible"
    echo ""
    echo "To deploy keys to all nodes, run:"
    echo "  cd $SCRIPT_DIR && ./deploy-all-keys.sh"
    exit 1
fi