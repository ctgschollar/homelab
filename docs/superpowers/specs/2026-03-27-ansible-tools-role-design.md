# Design: Ansible Tools Role with Dependency Resolution

**Date:** 2026-03-27
**Scope:** `ansible/roles/tools/`, `ansible/install-default-tools.yml`
**Status:** Approved

---

## Overview

Replace ad-hoc per-role dependency installation with a shared `tools` role. Playbooks declare the tools they need in a `vars` section; the role resolves the full dependency graph (including transitive prerequisites) and installs each tool exactly once.

**Motivation:** The current approach bolts dependencies onto individual roles as they're discovered (e.g., `gnupg` added to `claude-runner` after `apt_repository` failed). This is fragile and causes duplication — `gnupg` would appear in every role that uses an apt repository. The tools role centralises all tool installation and encodes the dependency graph in one place.

---

## Directory Structure

```
ansible/
  roles/
    tools/
      vars/
        main.yml                  # tool_definitions registry
      tasks/
        main.yml                  # resolve deps + conditional install blocks
        install-gh.yml            # apt key + repo + package for gh CLI
        install-cloudflared.yml   # apt key + repo + package for cloudflared
      filter_plugins/
        resolve_deps.py           # topological sort + dedup filter
  install-default-tools.yml       # replaces install-basic-tools.yml
```

---

## Playbook Interface

Every playbook that needs tools declares `required_tools` in its `vars` block and invokes the role in `pre_tasks`:

```yaml
vars:
  required_tools: [gh, hatch]

pre_tasks:
  - name: Install required tools
    import_role:
      name: tools
```

`required_tools` lists only the tools the playbook directly needs. Transitive dependencies are resolved by the role — the playbook author does not need to know that `gh` requires `gnupg`.

---

## Tool Definitions

All tools are defined in `roles/tools/vars/main.yml` under `tool_definitions`. Each entry has:

- `deps` (list, required): direct prerequisites; may be empty
- One of:
  - `apt` (list): package names to install via apt
  - `pipx` (string): package name to install via `community.general.pipx`
  - `tasks` (string): filename under `roles/tools/tasks/` to `include_tasks`

```yaml
# roles/tools/vars/main.yml
tool_definitions:
  gnupg:
    apt: [gnupg]
    deps: []

  git:
    apt: [git]
    deps: []

  curl:
    apt: [curl]
    deps: []

  wget:
    apt: [wget]
    deps: []

  vim:
    apt: [vim]
    deps: []

  python3-pip:
    apt: [python3-pip]
    deps: []

  pipx:
    apt: [pipx]
    deps: [python3-pip]

  hatch:
    pipx: hatch
    deps: [pipx]

  gh:
    deps: [gnupg]
    tasks: install-gh.yml

  cloudflared:
    deps: [gnupg]
    tasks: install-cloudflared.yml
```

---

## Dependency Resolution

`filter_plugins/resolve_deps.py` implements a `resolve_tool_deps` Jinja2 filter. It takes the `required_tools` list and the `tool_definitions` dict, performs a depth-first topological sort with cycle detection, and returns a deduplicated ordered list where every dependency appears before its dependent.

Example: `[gh, hatch]` → `[gnupg, gh, python3-pip, pipx, hatch]`

The filter raises an error if:
- A tool name in `required_tools` is not in `tool_definitions`
- A `deps` entry references an undefined tool
- A dependency cycle is detected

---

## Task Execution

`tasks/main.yml`:

1. Load `tool_definitions` (already available via `vars/main.yml`)
2. Call the filter to produce `resolved_tools`
3. For each tool type, run a conditional block:

```yaml
- name: Resolve tool dependencies
  set_fact:
    resolved_tools: "{{ required_tools | resolve_tool_deps(tool_definitions) }}"

- name: Install apt packages
  apt:
    name: "{{ tool_definitions[item].apt }}"
    state: present
    update_cache: yes
  loop: "{{ resolved_tools | selectattr_in(tool_definitions, 'apt') }}"

- name: Install pipx packages
  community.general.pipx:
    name: "{{ tool_definitions[item].pipx }}"
    state: present
  loop: "{{ resolved_tools | selectattr_in(tool_definitions, 'pipx') }}"
  become: yes
  become_user: "{{ ansible_user }}"

- name: Run per-tool task files
  include_tasks: "{{ tool_definitions[item].tasks }}"
  loop: "{{ resolved_tools | selectattr_in(tool_definitions, 'tasks') }}"
```

The `selectattr_in` filter (also in `resolve_deps.py`) returns only items from `resolved_tools` that have the given key in their `tool_definitions` entry.

---

## Per-Tool Task Files

`install-gh.yml` (migrated from `claude-runner/tasks/main.yml`):

```yaml
- name: Add GitHub CLI apt signing key
  get_url:
    url: https://cli.github.com/packages/githubcli-archive-keyring.gpg
    dest: /usr/share/keyrings/githubcli-archive-keyring.gpg
    mode: '0644'

- name: Add GitHub CLI apt repository
  apt_repository:
    repo: "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main"
    filename: github-cli
    state: present

- name: Install gh CLI
  apt:
    name: gh
    state: present
    update_cache: yes
```

`install-cloudflared.yml` is a placeholder — content to be added when cloudflared is needed.

---

## `install-default-tools.yml`

Replaces `install-basic-tools.yml`:

```yaml
---
- name: Install default tools
  hosts: all
  become: yes

  vars:
    required_tools: [git, curl, wget, vim, hatch]

  pre_tasks:
    - name: Install required tools
      import_role:
        name: tools
```

`install-basic-tools.yml` is deleted.

---

## `deploy-claude-runner.yml` Changes

Remove the inline `gnupg` install task from `ansible/roles/claude-runner/tasks/main.yml` (the hotfix added after the `apt_repository` failure). Remove all GitHub CLI tasks from that file. They are now handled by the tools role.

`deploy-claude-runner.yml` adds:

```yaml
vars:
  required_tools: [gh]

pre_tasks:
  - name: Install required tools
    import_role:
      name: tools
```

---

## Files Changed

| File | Change |
|------|--------|
| `ansible/roles/tools/vars/main.yml` | New — tool registry |
| `ansible/roles/tools/tasks/main.yml` | New — resolve + install loop |
| `ansible/roles/tools/tasks/install-gh.yml` | New — migrated from claude-runner |
| `ansible/roles/tools/tasks/install-cloudflared.yml` | New — placeholder |
| `ansible/roles/tools/filter_plugins/resolve_deps.py` | New — dep resolution filter |
| `ansible/install-default-tools.yml` | New — replaces install-basic-tools.yml |
| `ansible/install-basic-tools.yml` | Deleted |
| `ansible/roles/claude-runner/tasks/main.yml` | Remove gnupg + gh CLI tasks |
| `ansible/deploy-claude-runner.yml` | Add `required_tools` var + tools pre_task |

---

## Out of Scope

- No changes to the `tools` role handler for service restarts — tools don't need restart notification
- `community.general` collection is assumed to be installed (`ansible-galaxy collection install community.general`)
- No version pinning for tool installations
- `install-cloudflared.yml` content is deferred — placeholder only
