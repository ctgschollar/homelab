# Gitea Act Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Ansible role and playbook to deploy the Gitea act_runner as a systemd service on a dedicated VM (`gt-runner-01`, 192.168.3.100).

**Architecture:** Docker is added to the `tools` role (consistent with the existing dep-resolution pattern). A new `gitea-runner` Ansible role downloads the act_runner binary, registers it with Gitea using a token placed manually on the host, and manages it via a systemd service. A dedicated playbook targets the new `gitea_runners` inventory group and installs Docker via `required_tools` before invoking the role.

**Tech Stack:** Ansible, act_runner binary (gitea/act_runner), Docker (apt via tools role), systemd

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `ansible/inventory.yml` | Add `gitea_runners` group with `gt-runner-01` |
| Modify | `ansible/roles/tools/vars/main.yml` | Add `docker` tool definition |
| Create | `ansible/roles/tools/tasks/install-docker.yml` | Docker apt repo + package install |
| Create | `ansible/deploy-gitea-runner.yml` | Playbook targeting `gitea_runners` |
| Create | `ansible/roles/gitea-runner/defaults/main.yml` | Configurable defaults |
| Create | `ansible/roles/gitea-runner/tasks/main.yml` | Orchestrates runner setup |
| Create | `ansible/roles/gitea-runner/tasks/install-act-runner.yml` | Download act_runner binary |
| Create | `ansible/roles/gitea-runner/tasks/register.yml` | Idempotent runner registration |
| Create | `ansible/roles/gitea-runner/templates/act-runner.service.j2` | systemd unit |
| Create | `ansible/roles/gitea-runner/templates/config.yaml.j2` | act_runner config file |
| Create | `ansible/roles/gitea-runner/handlers/main.yml` | Restart service on config change |
| Create | `ansible/roles/gitea-runner/README.md` | Token setup instructions |

---

### Task 1: Add Docker to the tools role

**Files:**
- Modify: `ansible/roles/tools/vars/main.yml`
- Create: `ansible/roles/tools/tasks/install-docker.yml`

- [ ] **Step 1: Add `docker` to tool_definitions in `ansible/roles/tools/vars/main.yml`**

Append under the existing `tool_definitions` map:

```yaml
  docker:
    deps: [gnupg, curl]
    tasks: install-docker.yml
```

- [ ] **Step 2: Create `ansible/roles/tools/tasks/install-docker.yml`**

```yaml
---
# ansible/roles/tools/tasks/install-docker.yml

- name: Create Docker apt keyring directory
  file:
    path: /etc/apt/keyrings
    state: directory
    mode: '0755'

- name: Download Docker GPG key
  get_url:
    url: https://download.docker.com/linux/debian/gpg
    dest: /etc/apt/keyrings/docker.asc
    mode: '0444'

- name: Add Docker apt repository
  apt_repository:
    repo: >-
      deb [arch={{ ansible_architecture | replace('x86_64', 'amd64') }}
      signed-by=/etc/apt/keyrings/docker.asc]
      https://download.docker.com/linux/debian
      {{ ansible_distribution_release }} stable
    filename: docker
    state: present

- name: Install Docker packages
  apt:
    name:
      - docker-ce
      - docker-ce-cli
      - containerd.io
    state: present
    update_cache: yes

- name: Enable and start Docker
  systemd:
    name: docker
    enabled: yes
    state: started
```

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/tools/vars/main.yml ansible/roles/tools/tasks/install-docker.yml
git commit -m "feat: add docker to tools role"
```

---

### Task 2: Add inventory group and playbook

**Files:**
- Modify: `ansible/inventory.yml`
- Create: `ansible/deploy-gitea-runner.yml`

- [ ] **Step 1: Add `gitea_runners` group to `ansible/inventory.yml`**

Add under `all.children`:

```yaml
    gitea_runners:
      hosts:
        gt-runner-01:
          ansible_host: 192.168.3.100
      vars:
        ansible_user: root
        ansible_python_interpreter: /usr/bin/python3
        ansible_ssh_private_key_file: "{{ inventory_dir }}/ansible_ssh_key"
```

- [ ] **Step 2: Create `ansible/deploy-gitea-runner.yml`**

```yaml
---
- name: Deploy Gitea act runner
  hosts: gitea_runners
  become: true

  vars:
    required_tools: [docker]

  pre_tasks:
    - name: Install required tools
      import_role:
        name: tools

  roles:
    - gitea-runner
```

- [ ] **Step 3: Commit**

```bash
git add ansible/inventory.yml ansible/deploy-gitea-runner.yml
git commit -m "feat: add gitea_runners inventory group and deploy playbook"
```

---

### Task 3: Role defaults

**Files:**
- Create: `ansible/roles/gitea-runner/defaults/main.yml`

- [ ] **Step 1: Create `ansible/roles/gitea-runner/defaults/main.yml`**

```yaml
---
# ansible/roles/gitea-runner/defaults/main.yml

gitea_runner_user: act-runner
gitea_runner_base_dir: /opt/act-runner
gitea_runner_config_dir: /etc/gitea-runner

# Token file placed manually on the host before running this playbook.
# See README.md for instructions.
gitea_runner_token_file: /etc/gitea-runner/runner-token

gitea_instance_url: https://gitea.schollar.dev

# Runner identity
gitea_runner_name: "{{ inventory_hostname }}"
gitea_runner_labels: "debian-latest:host,native:host"

# act_runner binary version — check https://gitea.com/gitea/act_runner/releases
gitea_act_runner_version: "0.2.11"
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/gitea-runner/defaults/main.yml
git commit -m "feat: add gitea-runner role defaults"
```

---

### Task 4: act_runner binary installation task

**Files:**
- Create: `ansible/roles/gitea-runner/tasks/install-act-runner.yml`

- [ ] **Step 1: Create `ansible/roles/gitea-runner/tasks/install-act-runner.yml`**

```yaml
---
# ansible/roles/gitea-runner/tasks/install-act-runner.yml

- name: Set act_runner architecture
  set_fact:
    _act_runner_arch: "{{ 'amd64' if ansible_architecture == 'x86_64' else ansible_architecture }}"

- name: Download act_runner binary
  get_url:
    url: >-
      https://gitea.com/gitea/act_runner/releases/download/v{{ gitea_act_runner_version }}/act_runner-{{ gitea_act_runner_version }}-linux-{{ _act_runner_arch }}
    dest: /usr/local/bin/act_runner
    mode: '0755'
    force: yes
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/gitea-runner/tasks/install-act-runner.yml
git commit -m "feat: add act_runner binary install task"
```

---

### Task 5: Templates

**Files:**
- Create: `ansible/roles/gitea-runner/templates/config.yaml.j2`
- Create: `ansible/roles/gitea-runner/templates/act-runner.service.j2`

- [ ] **Step 1: Create `ansible/roles/gitea-runner/templates/config.yaml.j2`**

```jinja2
# Managed by Ansible — do not edit manually
log:
  level: info

runner:
  capacity: 1
  envs: {}
  timeout: 3h
  insecure: false

cache:
  enabled: false

container:
  network: host
  privileged: false
  options:
  workdir_parent:

host:
  workdir_parent:
```

- [ ] **Step 2: Create `ansible/roles/gitea-runner/templates/act-runner.service.j2`**

```jinja2
[Unit]
Description=Gitea act runner
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
User={{ gitea_runner_user }}
WorkingDirectory={{ gitea_runner_base_dir }}
ExecStart=/usr/local/bin/act_runner daemon --config {{ gitea_runner_config_dir }}/config.yaml
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/gitea-runner/templates/
git commit -m "feat: add act-runner config and systemd unit templates"
```

---

### Task 6: Runner registration task

**Files:**
- Create: `ansible/roles/gitea-runner/tasks/register.yml`

- [ ] **Step 1: Create `ansible/roles/gitea-runner/tasks/register.yml`**

```yaml
---
# ansible/roles/gitea-runner/tasks/register.yml
# Registration is idempotent: skipped if .runner state file already exists.

- name: Check if runner is already registered
  stat:
    path: "{{ gitea_runner_base_dir }}/.runner"
  register: _runner_state

- name: Read registration token
  slurp:
    src: "{{ gitea_runner_token_file }}"
  register: _runner_token
  when: not _runner_state.stat.exists

- name: Register runner with Gitea
  command: >
    /usr/local/bin/act_runner register
    --no-interactive
    --instance {{ gitea_instance_url }}
    --token {{ _runner_token.content | b64decode | trim }}
    --name {{ gitea_runner_name }}
    --labels {{ gitea_runner_labels }}
    --config {{ gitea_runner_config_dir }}/config.yaml
  args:
    chdir: "{{ gitea_runner_base_dir }}"
  become_user: "{{ gitea_runner_user }}"
  when: not _runner_state.stat.exists
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/gitea-runner/tasks/register.yml
git commit -m "feat: add idempotent act_runner registration task"
```

---

### Task 7: Handlers

**Files:**
- Create: `ansible/roles/gitea-runner/handlers/main.yml`

- [ ] **Step 1: Create `ansible/roles/gitea-runner/handlers/main.yml`**

```yaml
---
# ansible/roles/gitea-runner/handlers/main.yml

- name: reload systemd
  systemd:
    daemon_reload: yes

- name: restart act-runner
  systemd:
    name: act-runner
    state: restarted
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/gitea-runner/handlers/main.yml
git commit -m "feat: add gitea-runner role handlers"
```

---

### Task 8: Main tasks orchestration

**Files:**
- Create: `ansible/roles/gitea-runner/tasks/main.yml`

- [ ] **Step 1: Create `ansible/roles/gitea-runner/tasks/main.yml`**

```yaml
---
# ansible/roles/gitea-runner/tasks/main.yml

- name: Create act-runner system user
  user:
    name: "{{ gitea_runner_user }}"
    system: yes
    shell: /usr/sbin/nologin
    home: "{{ gitea_runner_base_dir }}"
    create_home: yes
    state: present

- name: Add act-runner user to docker group
  user:
    name: "{{ gitea_runner_user }}"
    groups: docker
    append: yes

- name: Create config directory
  file:
    path: "{{ gitea_runner_config_dir }}"
    state: directory
    owner: "{{ gitea_runner_user }}"
    group: "{{ gitea_runner_user }}"
    mode: '0750'

- name: Install act_runner binary
  import_tasks: install-act-runner.yml

- name: Template act_runner config
  template:
    src: config.yaml.j2
    dest: "{{ gitea_runner_config_dir }}/config.yaml"
    owner: "{{ gitea_runner_user }}"
    group: "{{ gitea_runner_user }}"
    mode: '0640'
  notify: restart act-runner

- name: Register runner
  import_tasks: register.yml

- name: Install systemd service
  template:
    src: act-runner.service.j2
    dest: /etc/systemd/system/act-runner.service
    owner: root
    group: root
    mode: '0644'
  notify:
    - reload systemd
    - restart act-runner

- name: Enable and start act-runner service
  systemd:
    name: act-runner
    enabled: yes
    state: started
    daemon_reload: yes
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/gitea-runner/tasks/main.yml
git commit -m "feat: add gitea-runner role main tasks"
```

---

### Task 9: README

**Files:**
- Create: `ansible/roles/gitea-runner/README.md`

- [ ] **Step 1: Create `ansible/roles/gitea-runner/README.md`**

```markdown
# gitea-runner Role

Deploys the Gitea act_runner as a systemd service on a dedicated host.

## Prerequisites

1. A Debian 13 VM provisioned and reachable at the IP in `inventory.yml`.
2. SSH access as root using the shared `ansible_ssh_key`.

## Before Running the Playbook

The runner must be registered with a token from Gitea. This is a one-time step.

### 1. Generate a runner token in Gitea

- Log in to Gitea as an admin.
- Go to **Site Administration → Runners → Create Runner Token**.
- Copy the token.

### 2. Place the token on the target host

SSH into the runner VM and write the token to the expected location:

\`\`\`bash
ssh root@gt-runner-01.home
mkdir -p /etc/gitea-runner
echo 'YOUR_TOKEN_HERE' > /etc/gitea-runner/runner-token
chmod 600 /etc/gitea-runner/runner-token
\`\`\`

### 3. Run the playbook

From the `ansible/` directory:

\`\`\`bash
ansible-playbook -i inventory.yml deploy-gitea-runner.yml
\`\`\`

## Re-registration

If the runner needs to be re-registered (e.g. after a token rotation), delete the state file and re-run the playbook:

\`\`\`bash
ssh root@gt-runner-01.home rm /opt/act-runner/.runner
ansible-playbook -i inventory.yml deploy-gitea-runner.yml
\`\`\`

## Defaults

| Variable | Default | Description |
|----------|---------|-------------|
| `gitea_runner_user` | `act-runner` | System user to run the service |
| `gitea_runner_base_dir` | `/opt/act-runner` | Working directory |
| `gitea_runner_config_dir` | `/etc/gitea-runner` | Config and token location |
| `gitea_runner_token_file` | `/etc/gitea-runner/runner-token` | Token file path |
| `gitea_instance_url` | `https://gitea.schollar.dev` | Gitea instance URL |
| `gitea_runner_name` | `{{ inventory_hostname }}` | Runner name shown in Gitea |
| `gitea_runner_labels` | `debian-latest:host,native:host` | Runner labels |
| `gitea_act_runner_version` | `0.2.11` | act_runner binary version |
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/gitea-runner/README.md
git commit -m "docs: add gitea-runner role README with token setup instructions"
```

---

### Task 10: Verify

- [ ] **Step 1: Confirm all role files are present**

```bash
find ansible/roles/gitea-runner ansible/roles/tools/tasks/install-docker.yml -type f | sort
```

Expected:
```
ansible/roles/gitea-runner/defaults/main.yml
ansible/roles/gitea-runner/handlers/main.yml
ansible/roles/gitea-runner/README.md
ansible/roles/gitea-runner/tasks/install-act-runner.yml
ansible/roles/gitea-runner/tasks/main.yml
ansible/roles/gitea-runner/tasks/register.yml
ansible/roles/gitea-runner/templates/act-runner.service.j2
ansible/roles/gitea-runner/templates/config.yaml.j2
ansible/roles/tools/tasks/install-docker.yml
```

- [ ] **Step 2: Dry-run the playbook (once VM is provisioned and token is placed)**

```bash
cd ansible
ansible-playbook -i inventory.yml deploy-gitea-runner.yml --check
```

Expected: tasks listed with no fatal errors.

- [ ] **Step 3: Verify runner appears in Gitea after a real run**

After provisioning the VM, placing the token, and running the playbook:
- Go to **Gitea → Site Administration → Runners**
- Confirm `gt-runner-01` appears with status **Idle**
