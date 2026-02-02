#!/bin/bash
# Helper script to deploy SSH key to root on a single node
# Requires: chris user with sudo access on the target node
# Usage: ./deploy-key-to-node.sh dks01.schollar.dev

set -e

if [ $# -ne 1 ]; then
    echo "Usage: $0 <node-hostname>"
    echo "Example: $0 dks01.schollar.dev"
    exit 1
fi

NODE=$1
SCRIPT_DIR="$(dirname "$0")"
ANSIBLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PUBKEY=$(cat "$ANSIBLE_DIR/ansible_ssh_key.pub")

echo "=== Deploying Ansible SSH Key to root@$NODE ==="
echo ""
echo "Public key: $PUBKEY"
echo ""
echo "This requires chris user to have sudo access on $NODE"
echo ""

# SSH to the node and deploy the key using sudo
ssh -t chris@"$NODE" "sudo mkdir -p /root/.ssh && \
    sudo chmod 700 /root/.ssh && \
    echo '$PUBKEY' | sudo tee -a /root/.ssh/authorized_keys > /dev/null && \
    sudo chmod 600 /root/.ssh/authorized_keys && \
    echo '✓ SSH key successfully added to root authorized_keys'"

if [ $? -ne 0 ]; then
    echo ""
    echo "✗ FAILED: Could not deploy key to $NODE"
    echo "  Make sure chris user has sudo access on the node"
    exit 1
fi

echo ""
echo "Testing root SSH access..."
if ssh -i "$ANSIBLE_DIR/ansible_ssh_key" -o BatchMode=yes -o ConnectTimeout=5 root@"$NODE" "echo OK" 2>/dev/null; then
    echo "✓ SUCCESS: Can now SSH as root@$NODE with key"
else
    echo "✗ FAILED: Cannot SSH as root yet. Please verify the key was added correctly."
    exit 1
fi