# claude-runner Ansible Role Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `infrastructure/` into `ansible/roles/claude-runner/` as a proper Ansible role, with a `deploy-claude-runner.yml` playbook targeting the `claude` inventory host, then delete `infrastructure/`.

**Architecture:** Pure structural migration — no logic changes. The standalone playbook is decomposed into the standard Ansible role layout (`defaults/`, `tasks/`, `handlers/`, `files/`, `templates/`). A thin playbook at `ansible/deploy-claude-runner.yml` applies the role to the `claude` host.

**Tech Stack:** Ansible, systemd, Jinja2 templates

---

## File Map

| Action | Path |
|--------|------|
| Create | `ansible/roles/claude-runner/defaults/main.yml` |
| Create | `ansible/roles/claude-runner/tasks/main.yml` |
| Create | `ansible/roles/claude-runner/handlers/main.yml` |
| Create | `ansible/roles/claude-runner/files/repos.conf` |
| Create | `ansible/roles/claude-runner/templates/claude-runner.j2` |
| Create | `ansible/roles/claude-runner/templates/claude-runner@.service.j2` |
| Create | `ansible/roles/claude-runner/templates/run.sh.j2` |
| Create | `ansible/roles/claude-runner/README.md` |
| Create | `ansible/deploy-claude-runner.yml` |
| Delete | `infrastructure/` (entire directory) |

---

### Task 1: Create role defaults

**Files:**
- Create: `ansible/roles/claude-runner/defaults/main.yml`

- [ ] **Step 1: Create the defaults file**

```yaml
---
# claude-runner Role - Default Variables

claude_runner_base_dir: /opt/claude-runner
claude_user: claude
claude_user_home: /home/claude
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/claude-runner/defaults/main.yml
git commit -m "feat: add claude-runner role defaults"
```

---

### Task 2: Create role handlers

**Files:**
- Create: `ansible/roles/claude-runner/handlers/main.yml`

- [ ] **Step 1: Create the handlers file**

```yaml
---
# claude-runner Role - Handlers

- name: reload systemd
  systemd:
    daemon_reload: yes
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/claude-runner/handlers/main.yml
git commit -m "feat: add claude-runner role handlers"
```

---

### Task 3: Create role tasks

**Files:**
- Create: `ansible/roles/claude-runner/tasks/main.yml`

Note: `src:` paths are shortened — Ansible resolves them relative to the role's `files/` and `templates/` directories automatically.

- [ ] **Step 1: Create the tasks file**

```yaml
---
# claude-runner Role - Main Tasks

- name: Create claude user
  user:
    name: "{{ claude_user }}"
    home: "{{ claude_user_home }}"
    shell: /bin/bash
    state: present
    create_home: yes

- name: Add claude user to docker group
  user:
    name: "{{ claude_user }}"
    groups: docker
    append: yes

- name: Create /opt/claude-runner base directory
  file:
    path: "{{ claude_runner_base_dir }}"
    state: directory
    owner: root
    group: root
    mode: '0755'

- name: Create env directory for per-instance repo configs
  file:
    path: "{{ claude_runner_base_dir }}/env"
    state: directory
    owner: root
    group: root
    mode: '0755'

- name: Install repos.conf (skip if already populated)
  copy:
    src: repos.conf
    dest: "{{ claude_runner_base_dir }}/repos.conf"
    owner: root
    group: root
    mode: '0644'
    force: no

- name: Install run.sh loop script
  template:
    src: run.sh.j2
    dest: "{{ claude_runner_base_dir }}/run.sh"
    owner: root
    group: root
    mode: '0755'

- name: Install claude-runner CLI
  template:
    src: claude-runner.j2
    dest: /usr/local/bin/claude-runner
    owner: root
    group: root
    mode: '0755'

- name: Install systemd template unit
  template:
    src: claude-runner@.service.j2
    dest: /etc/systemd/system/claude-runner@.service
    owner: root
    group: root
    mode: '0644'
  notify: reload systemd
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/claude-runner/tasks/main.yml
git commit -m "feat: add claude-runner role tasks"
```

---

### Task 4: Copy static file

**Files:**
- Create: `ansible/roles/claude-runner/files/repos.conf`

- [ ] **Step 1: Create the file**

```
# claude-runner repo registry
# Managed by the claude-runner CLI — do not edit manually.
# Format: name=path  (one per line)
# Example: myrepo=/home/claude/repos/myrepo
```

(Include a trailing newline after the last line.)

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/claude-runner/files/repos.conf
git commit -m "feat: add claude-runner role static files"
```

---

### Task 5: Create templates

**Files:**
- Create: `ansible/roles/claude-runner/templates/run.sh.j2`
- Create: `ansible/roles/claude-runner/templates/claude-runner@.service.j2`
- Create: `ansible/roles/claude-runner/templates/claude-runner.j2`

- [ ] **Step 1: Create `run.sh.j2`**

```bash
#!/bin/bash
# Managed by Ansible — do not edit directly.
# Source: ansible/roles/claude-runner/templates/run.sh.j2

REPO_PATH="${1:?Usage: run.sh <repo-path>}"

cd "$REPO_PATH"

while true; do
    claude --resume --dangerously-skip-permissions
    sleep 5
done
```

- [ ] **Step 2: Create `claude-runner@.service.j2`**

```ini
[Unit]
Description=Claude Code runner for %i
After=network-online.target
Wants=network-online.target

[Service]
User={{ claude_user }}
EnvironmentFile={{ claude_runner_base_dir }}/env/%i
WorkingDirectory=${REPO_PATH}
ExecStart={{ claude_runner_base_dir }}/run.sh ${REPO_PATH}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Create `claude-runner.j2`**

```bash
#!/bin/bash
# claude-runner — manage Claude Code loop runner instances
# Managed by Ansible — do not edit directly.
# Source: ansible/roles/claude-runner/templates/claude-runner.j2

set -euo pipefail

REPOS_CONF="{{ claude_runner_base_dir }}/repos.conf"
ENV_DIR="{{ claude_runner_base_dir }}/env"

usage() {
    cat <<EOF
Usage: claude-runner <command> [args]

Commands:
  add <name> <path>   Register a repo and start its runner service
  remove <name>       Stop and remove a runner
  list                Show all runners and their service status
  status <name>       Tail the journal for a runner
EOF
    exit 1
}

cmd_add() {
    local name="${1:?add requires a name}"
    local path="${2:?add requires a path}"

    if ! [[ -d "$path" ]]; then
        echo "Error: directory does not exist: $path" >&2
        exit 1
    fi

    # Canonicalize path
    path="$(realpath "$path")"

    # Reject duplicate names
    if grep -q "^${name}=" "$REPOS_CONF" 2>/dev/null; then
        echo "Error: '${name}' already exists in ${REPOS_CONF}" >&2
        exit 1
    fi

    echo "${name}=${path}" >> "$REPOS_CONF"
    echo "REPO_PATH=${path}" > "${ENV_DIR}/${name}"
    chmod 644 "${ENV_DIR}/${name}"

    systemctl daemon-reload
    systemctl enable --now "claude-runner@${name}.service"
    echo "Started claude-runner@${name} for ${path}"
}

cmd_remove() {
    local name="${1:?remove requires a name}"

    if systemctl is-active --quiet "claude-runner@${name}.service" 2>/dev/null; then
        systemctl stop "claude-runner@${name}.service"
    fi
    systemctl disable "claude-runner@${name}.service" 2>/dev/null || true

    # Remove from repos.conf
    sed -i "/^${name}=/d" "$REPOS_CONF"

    # Remove env file
    rm -f "${ENV_DIR}/${name}"

    echo "Removed claude-runner@${name}"
}

cmd_list() {
    if [[ ! -s "$REPOS_CONF" ]]; then
        echo "No repos configured. Use: claude-runner add <name> <path>"
        return
    fi

    printf "%-20s %-40s %s\n" "NAME" "PATH" "STATUS"
    printf "%-20s %-40s %s\n" "----" "----" "------"

    while IFS='=' read -r name path; do
        [[ -z "$name" || "$name" == \#* ]] && continue
        local status
        status="$(systemctl is-active "claude-runner@${name}.service" 2>/dev/null || echo inactive)"
        printf "%-20s %-40s %s\n" "$name" "$path" "$status"
    done < "$REPOS_CONF"
}

cmd_status() {
    local name="${1:?status requires a name}"
    exec journalctl -u "claude-runner@${name}.service" -f --output short-iso
}

case "${1:-}" in
    add)    shift; cmd_add "$@" ;;
    remove) shift; cmd_remove "$@" ;;
    list)   cmd_list ;;
    status) shift; cmd_status "$@" ;;
    *)      usage ;;
esac
```

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/claude-runner/templates/
git commit -m "feat: add claude-runner role templates"
```

---

### Task 6: Create the playbook

**Files:**
- Create: `ansible/deploy-claude-runner.yml`

- [ ] **Step 1: Create the playbook**

```yaml
---
- name: Deploy claude-runner service manager
  hosts: claude
  become: true
  roles:
    - claude-runner
```

- [ ] **Step 2: Commit**

```bash
git add ansible/deploy-claude-runner.yml
git commit -m "feat: add deploy-claude-runner playbook targeting claude host"
```

---

### Task 7: Run syntax check

- [ ] **Step 1: Validate the playbook**

```bash
cd /home/chris/src/homelab/ansible && ansible-playbook --syntax-check deploy-claude-runner.yml -i inventory.yml
```

Expected output:
```
playbook: deploy-claude-runner.yml
```

If there are errors, fix them before continuing.

---

### Task 8: Create role README

**Files:**
- Create: `ansible/roles/claude-runner/README.md`

- [ ] **Step 1: Create the README with the full content below**

```markdown
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
- SSH access with `become: true` (root/sudo)
- Docker installed on the target host (the `claude` user is added to the `docker` group)

## Running the playbook

```sh
cd ansible
ansible-playbook deploy-claude-runner.yml -i inventory.yml
```

The playbook targets the `claude` host (`192.168.3.79`) defined in `ansible/inventory.yml`.

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
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/claude-runner/README.md
git commit -m "docs: add claude-runner role README"
```

---

### Task 9: Delete infrastructure/

- [ ] **Step 1: Remove the directory**

```bash
git rm -r /home/chris/src/homelab/infrastructure
```

- [ ] **Step 2: Verify it's staged for deletion**

```bash
git status
```

Confirm `infrastructure/` files are listed as `deleted`.

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: remove infrastructure/ directory (migrated to ansible/roles/claude-runner)"
```
