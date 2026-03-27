# Design: Migrate claude-runner to ansible/roles/claude-runner

**Date:** 2026-03-25
**Status:** Approved

## Goal

Move the `infrastructure/` directory into `ansible/` as a proper Ansible role, consolidating all Ansible roles and playbooks under a single directory.

## Background

The claude-runner service manager currently lives in `infrastructure/` as a standalone playbook (`deploy.yml`) with templates and files alongside it. All other Ansible roles live under `ansible/roles/`. This migration brings claude-runner in line with the existing role structure.

## Approach

Straight lift-and-shift (Option A): move all content from `infrastructure/` into a new `ansible/roles/claude-runner/` role following the existing role pattern, create a playbook targeting the `claude` inventory host, and delete `infrastructure/`.

No logic changes are made — this is purely a restructuring.

## Target Directory Structure

```
ansible/
├── deploy-claude-runner.yml
├── roles/
│   └── claude-runner/
│       ├── defaults/main.yml
│       ├── tasks/main.yml
│       ├── handlers/main.yml
│       ├── files/repos.conf
│       ├── templates/
│       │   ├── claude-runner.j2
│       │   ├── claude-runner@.service.j2
│       │   └── run.sh.j2
│       └── README.md
```

## Component Details

### `defaults/main.yml`
Variables extracted from the inline `vars:` block in `infrastructure/deploy.yml`:
- `claude_runner_base_dir: /opt/claude-runner`
- `claude_user: claude`
- `claude_user_home: /home/claude`

### `tasks/main.yml`
All tasks from `infrastructure/deploy.yml` moved across. The `src:` values in `copy` and `template` tasks are updated: `files/repos.conf` → `repos.conf` and `templates/run.sh.j2` → `run.sh.j2` etc., since Ansible resolves these relative to the role's `files/` and `templates/` directories automatically when inside a role.

### `handlers/main.yml`
The `reload systemd` handler extracted from `infrastructure/deploy.yml`.

### `files/repos.conf`
Copied verbatim from `infrastructure/files/repos.conf`. The `files/` subdirectory is intentionally added to this role — it is not present in the reference roles (`cloudflared`, `traefik-edge`) but is a standard Ansible role directory.

### `templates/`
All three templates copied verbatim, with source comment lines updated from `infrastructure/templates/...` to `ansible/roles/claude-runner/templates/...`.

### `deploy-claude-runner.yml`
```yaml
---
- name: Deploy claude-runner service manager
  hosts: claude
  become: true
  roles:
    - claude-runner
```

Targets the `claude` host (192.168.3.79) defined in `ansible/inventory.yml`. All variables come from the role's `defaults/main.yml`.

Note: the source playbook used `hosts: "{{ target | default('localhost') }}"` to allow runtime host override. This is intentionally replaced with a hardcoded `hosts: claude` — the inventory host is the correct and only deployment target, and the override mechanism is no longer needed.

### `README.md`
Copied from `infrastructure/README.md` with the run instructions updated: replace references to `../infrastructure/deploy.yml` with `deploy-claude-runner.yml`, and update the source comment in the "Running the playbook" section to reflect the new location.

## Deletions

The entire `infrastructure/` directory is removed after the role is created.

## Out of Scope

- No changes to playbook logic, task ordering, or template content
- No changes to `ansible/inventory.yml`
