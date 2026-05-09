# LLM Stack Design

**Date:** 2026-05-09
**Status:** Approved

## Overview

Deploy a local LLM stack on a standalone Ubuntu 26.04 laptop with an 8GB NVIDIA GPU and 24GB RAM. The stack consists of Ollama (native systemd service) and Open WebUI (Docker container), accessible on the local network by IP and port. The laptop is not part of the Docker Swarm.

This spec also covers an enhancement to the existing `tools` Ansible role to support OS-specific installation logic, with Debian 12 as the default.

---

## Tools Role: OS-Aware Installation

### Problem

The `tools` role currently uses flat task files that assume Debian. Docker's install script hardcodes the Debian apt repo URL. NVIDIA drivers are Ubuntu-specific and have no existing entry. As hosts diverge by distro, the role needs a clean way to select the right install logic per OS.

### Scope

All managed hosts remain Debian-based (Debian 12 or Ubuntu). No other distros are in scope.

### Directory Structure

Task files move from flat into a `debian/` subdirectory. OS-specific variants live in their own subdirectory alongside it.

```
roles/tools/tasks/
  main.yml                        # role entry point — unchanged
  debian/
    install-docker.yml            # moved from flat (Debian repo URL)
    install-tea.yml               # moved from flat
    install-act.yml               # moved from flat
    install-cloudflared.yml       # moved from flat
    install-gh.yml                # moved from flat
    install-nvm.yml               # moved from flat
    install-node.yml              # moved from flat
  ubuntu/
    install-docker.yml            # new — Ubuntu Docker repo URL
    install-nvidia-drivers.yml    # new — Ubuntu only
```

### Resolution Order

When the role runs a `tasks:`-type tool, it resolves the task file via `first_found`:

1. `tasks/{{ ansible_distribution | lower }}/{{ task_file }}`
2. `tasks/debian/{{ task_file }}`

Tools with no OS variance (e.g. `tea`, `act`) only exist in `debian/` and are found via fallback. Tools with OS variance have entries in the matching OS directory.

### vars/main.yml Changes

No structural changes. The `tasks:` key continues to hold a filename. Two new tool definitions are added:

```yaml
nvidia-drivers:
  deps: []
  tasks: install-nvidia-drivers.yml   # only exists under ubuntu/
```

Docker's existing entry is unchanged — the new resolution logic picks up `ubuntu/install-docker.yml` automatically on Ubuntu hosts.

### Filter Plugins

No changes to `resolve_deps.py` or `tools_with_key`. The OS resolution is handled at task-include time in `main.yml`.

---

## New Role: `llama`

Installs and configures Ollama as a systemd service.

### Files

```
ansible/roles/llama/
  defaults/main.yml
  tasks/main.yml
```

### Defaults

```yaml
ollama_host: "0.0.0.0"
ollama_port: 11434
```

### Tasks

1. Download and run the official Ollama install script (`curl -fsSL https://ollama.com/install.sh | sh`), which installs the binary and creates the `ollama` systemd service
2. Create `/etc/systemd/system/ollama.service.d/` directory
3. Template a drop-in override at `/etc/systemd/system/ollama.service.d/override.conf` setting `Environment=OLLAMA_HOST={{ ollama_host }}:{{ ollama_port }}`
4. Reload systemd, enable and start the `ollama` service

Model selection and loading is out of scope — done manually after deploy.

---

## New Role: `webui`

Runs Open WebUI as a Docker container managed by a systemd unit.

### Files

```
ansible/roles/webui/
  defaults/main.yml
  tasks/main.yml
  templates/open-webui.service.j2
```

### Defaults

```yaml
webui_port: 3000
webui_image: "ghcr.io/open-webui/open-webui:main"
ollama_base_url: "http://localhost:11434"
webui_data_dir: "/opt/open-webui"
```

### Tasks

1. Create `{{ webui_data_dir }}` for persistent container data
2. Pull the Open WebUI Docker image
3. Template `/etc/systemd/system/open-webui.service` from `open-webui.service.j2`
4. Enable and start the `open-webui` systemd service

### Systemd Unit (template)

The unit runs `docker run` with:
- `--name open-webui` (so systemd can stop it by name)
- `-p {{ webui_port }}:8080`
- `-v {{ webui_data_dir }}:/app/backend/data`
- `-e OLLAMA_BASE_URL={{ ollama_base_url }}`

No `--restart` flag — restarts are handled by `Restart=always` in the `[Service]` section of the systemd unit to avoid Docker and systemd fighting over container lifecycle.

No Traefik integration. Accessible directly at `http://<host-ip>:{{ webui_port }}`.

---

## New Playbook: `deploy-llm.yml`

```yaml
- name: Deploy LLM stack
  hosts: llm_hosts
  vars:
    required_tools: [docker, nvidia-drivers]
  pre_tasks:
    - import_role:
        name: tools
  roles:
    - llama
    - webui
```

Run with:
```bash
ansible-playbook -i inventory.yml deploy-llm.yml
```

---

## Inventory

Add a new `llm_hosts` group to `ansible/inventory.yml`:

```yaml
llm_hosts:
  hosts:
    llm:
      ansible_host: <laptop-ip>
      ansible_user: root
```

---

## Post-Deploy Access

| Service   | URL                              |
|-----------|----------------------------------|
| Ollama    | `http://<laptop-ip>:11434`       |
| Open WebUI | `http://<laptop-ip>:3000`       |

Model configuration (loading models, assigning GPU vs CPU) is done manually via Ollama CLI after deploy.
