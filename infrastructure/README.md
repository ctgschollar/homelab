# claude-runner infrastructure

Deploys a systemd-based loop runner that keeps Claude Code sessions alive
continuously across multiple repos in parallel. Each repo runs as a separate
`claude-runner@<name>` template service under a dedicated `claude` system user.

## How it works

- **`claude-runner@.service`** — systemd template unit; one instance per repo
- **`run.sh`** — inner loop: `claude --resume --dangerously-skip-permissions`, sleep 5, repeat
- **`repos.conf`** — registry of `name=path` entries at `/opt/claude-runner/repos.conf`
- **`claude-runner`** CLI — manages instances (add / remove / list / status)

## Prerequisites

- Ansible installed on the control machine
- Claude Code already installed on the target host
- SSH access (or localhost) with `become: true` (root/sudo)
- Docker installed on the target host (the `claude` user is added to the `docker` group)

## Running the playbook

### Deploy to localhost

```sh
cd ansible
ansible-playbook ../infrastructure/deploy.yml -i inventory.yml -e target=localhost -c local
```

### Deploy to a remote host

Add the host to `ansible/inventory.yml` under an appropriate group, then:

```sh
cd ansible
ansible-playbook ../infrastructure/deploy.yml -i inventory.yml -e target=<hostname>
```

The playbook is idempotent — safe to run multiple times. It will not overwrite
an already-populated `repos.conf`.

## claude-runner CLI

All commands must be run as root (or with `sudo`).

### Add a repo

```sh
claude-runner add <name> <path>
```

Registers the repo in `repos.conf`, writes a per-instance env file, and
enables + starts `claude-runner@<name>.service` immediately.

```sh
claude-runner add homelab /home/claude/repos/homelab
claude-runner add myapp   /home/claude/repos/myapp
```

### List all runners

```sh
claude-runner list
```

```
NAME                 PATH                                     STATUS
----                 ----                                     ------
homelab              /home/claude/repos/homelab               active
myapp                /home/claude/repos/myapp                 inactive
```

### Tail logs for a runner

```sh
claude-runner status <name>
```

Equivalent to `journalctl -u claude-runner@<name>.service -f`.

### Remove a runner

```sh
claude-runner remove <name>
```

Stops and disables the service, removes the entry from `repos.conf`.

## File layout on the target host

```
/opt/claude-runner/
├── repos.conf          # name=path registry
├── run.sh              # inner loop script
└── env/
    ├── homelab         # REPO_PATH=/home/claude/repos/homelab
    └── myapp           # REPO_PATH=/home/claude/repos/myapp

/etc/systemd/system/
└── claude-runner@.service

/usr/local/bin/
└── claude-runner
```
