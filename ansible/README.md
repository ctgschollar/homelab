# Ansible Configuration for Homelab

This directory contains Ansible playbooks and utilities for managing the homelab infrastructure.

## Directory Structure

```
ansible/
├── inventory.yml              # Inventory file (dks01-dks05.schollar.dev)
├── ansible_ssh_key            # SSH private key for Ansible (gitignored)
├── ansible_ssh_key.pub        # SSH public key for Ansible (gitignored)
├── utils/                     # Utility scripts
│   ├── deploy-key-to-node.sh  # Deploy SSH key to one node
│   ├── deploy-all-keys.sh     # Deploy SSH keys to all nodes
│   └── setup-ssh-keys.sh      # Test SSH access to all nodes
├── linstor-backup/            # Linstor volume backup playbook
│   ├── playbook.yml           # Main playbook
│   ├── files/                 # Files deployed by playbook
│   └── README.md              # Full documentation
└── README.md                  # This file
```

## Quick Start

### 1. Prerequisites

- Ansible installed on your control machine
- SSH access to all nodes with a user that has sudo privileges
- Docker and linstor-docker-volume driver installed on nodes

### 2. Initial Setup - Deploy SSH Keys

Before running any playbook, deploy the SSH keys to root on all nodes:

```bash
cd /home/chris/Documents/git/homelab/ansible

# Deploy to all nodes
./utils/deploy-all-keys.sh

# Verify access
./utils/setup-ssh-keys.sh
```

### 3. Run a Playbook

Example: Deploy the linstor backup system

```bash
ansible-playbook -i inventory.yml linstor-backup/playbook.yml
```

## Available Playbooks

### Linstor Backup

Automated backup system for Docker volumes using linstor driver.

- **Location:** `linstor-backup/`
- **Purpose:** Daily backups of all linstor volumes to local storage (included in Proxmox Backup)
- **Documentation:** See `linstor-backup/README.md`
- **Run:** `ansible-playbook -i inventory.yml linstor-backup/playbook.yml`

## Inventory

The `inventory.yml` file contains all Docker Swarm nodes:

- dks01.schollar.dev
- dks02.schollar.dev
- dks03.schollar.dev
- dks04.schollar.dev
- dks05.schollar.dev

## SSH Keys

The `ansible_ssh_key` and `ansible_ssh_key.pub` files are used for passwordless SSH access to root on all nodes. These are gitignored for security.

**Your public key:**
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIITg97h8kLcMhQINKhboAMl5vSfx/mWc3Z7XpeUb7fMT ansible@homelab
```

## Adding New Playbooks

When adding a new playbook:

1. Create a new directory for your playbook (e.g., `my-playbook/`)
2. Add a `playbook.yml` file in that directory
3. Add any files the playbook needs in `my-playbook/files/`
4. Add a `README.md` with documentation
5. Update this README to list the new playbook

Example structure:
```
ansible/
├── my-playbook/
│   ├── playbook.yml
│   ├── files/
│   │   └── config.txt
│   └── README.md
```

Run with:
```bash
ansible-playbook -i inventory.yml my-playbook/playbook.yml
```

## Utility Scripts

All utility scripts are in the `utils/` directory:

- **deploy-key-to-node.sh** - Deploy SSH key to a single node
- **deploy-all-keys.sh** - Deploy SSH keys to all nodes at once
- **setup-ssh-keys.sh** - Test SSH access to all nodes

Run from the utils directory or with full path:
```bash
cd utils
./deploy-all-keys.sh

# or
./utils/deploy-all-keys.sh
```

## Common Tasks

### Test connectivity to all nodes
```bash
ansible -i inventory.yml docker_swarm_nodes -m ping
```

### Run ad-hoc command on all nodes
```bash
ansible -i inventory.yml docker_swarm_nodes -a "uptime"
```

### Check disk space on all nodes
```bash
ansible -i inventory.yml docker_swarm_nodes -a "df -h"
```

### Update apt cache on all nodes
```bash
ansible -i inventory.yml docker_swarm_nodes -m apt -a "update_cache=yes"
```
