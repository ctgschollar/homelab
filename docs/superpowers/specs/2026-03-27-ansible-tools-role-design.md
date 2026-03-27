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
  filter_plugins/
    resolve_deps.py               # custom Jinja2 filters (role-scoped placement not supported)
  roles/
    tools/
      defaults/
        main.yml                  # required_tools: [] (safe default)
      meta/
        main.yml                  # role metadata (galaxy_info)
      vars/
        main.yml                  # tool_definitions registry
      tasks/
        main.yml                  # resolve deps + conditional install blocks
        install-gh.yml            # apt key + repo + package for gh CLI
        install-cloudflared.yml   # apt key + repo + package for cloudflared (placeholder)
  install-default-tools.yml       # replaces install-basic-tools.yml
```

**Note:** `filter_plugins/` is placed beside the playbooks (`ansible/filter_plugins/`), not inside the role directory. Both locations work — Ansible auto-discovers filter plugins inside roles too — but the playbook-adjacent location makes the filters available to all playbooks and roles regardless of load order.

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

tasks: []   # required when there is no roles: block; omit if roles: is present
```

`required_tools` lists only the tools the playbook directly needs. Transitive dependencies are resolved by the role — the playbook author does not need to know that `gh` requires `gnupg`.

**`tasks: []` rule:** include it only in plays that have no `roles:` block and no other tasks. It is not needed — and should be omitted — when a `roles:` block is present.

**Single invocation per play:** the role uses `set_fact` to store `resolved_tools` and `_apt_tools`. These facts persist for the host for the duration of the play. Import this role at most once per play to avoid stale facts from a prior invocation.

---

## Tool Definitions

All tools are defined in `roles/tools/vars/main.yml` under `tool_definitions`. Each entry has:

- `deps` (list, required): direct prerequisites; may be empty
- One of:
  - `apt` (list): package names to install via apt
  - `pipx` (string): package name to install via `community.general.pipx`
  - `tasks` (string): bare filename (no path separators) under `roles/tools/tasks/` to `include_tasks`; subdirectory prefixes are not supported

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

## Custom Filters

`ansible/filter_plugins/resolve_deps.py` implements two Jinja2 filters:

### `resolve_tool_deps(required_tools, tool_definitions)`

Performs a depth-first topological sort over the `tool_definitions` dependency graph.

- **Input:** `required_tools` (list of tool name strings), `tool_definitions` (dict as above)
- **Output:** deduplicated ordered list of tool names; every dependency appears before its dependent
- **Deduplication:** first-visit wins — a tool that appears via multiple dependency paths is emitted at the position of its first DFS encounter and skipped on all subsequent encounters
- **Errors:** raises `AnsibleFilterError` if a tool name is not in `tool_definitions`, a `deps` entry references an undefined tool, or a dependency cycle is detected

Example: `['gh', 'hatch'] | resolve_tool_deps(tool_definitions)` → `['gnupg', 'gh', 'python3-pip', 'pipx', 'hatch']`

### `tools_with_key(resolved_tools, tool_definitions, key)`

Returns the subset of `resolved_tools` whose entry in `tool_definitions` contains the given key.

- **Input:** `resolved_tools` (list of tool name strings), `tool_definitions` (dict), `key` (string)
- **Output:** list of tool name strings from `resolved_tools` where `tool_definitions[name]` has `key`

Example: `['gnupg', 'gh', 'python3-pip', 'pipx', 'hatch'] | tools_with_key(tool_definitions, 'apt')` → `['gnupg', 'python3-pip', 'pipx']`

---

## Task Execution

`tasks/main.yml` resolves the dependency graph then handles each install type:

```yaml
- name: Resolve tool dependencies
  set_fact:
    resolved_tools: "{{ required_tools | resolve_tool_deps(tool_definitions) }}"

- name: Collect apt packages
  set_fact:
    _apt_tools: "{{ resolved_tools | tools_with_key(tool_definitions, 'apt') }}"

- name: Install apt packages
  apt:
    name: "{{ _apt_tools | map('extract', tool_definitions) | map(attribute='apt') | flatten | list }}"
    state: present
    update_cache: yes
  when: _apt_tools | length > 0

- name: Install pipx packages
  community.general.pipx:
    name: "{{ tool_definitions[item].pipx }}"
    state: present
  environment:
    PIPX_HOME: /opt/pipx
    PIPX_BIN_DIR: /usr/local/bin
  loop: "{{ resolved_tools | tools_with_key(tool_definitions, 'pipx') }}"

- name: Run per-tool task files
  include_tasks: "{{ tool_definitions[tool_name].tasks }}"
  loop: "{{ resolved_tools | tools_with_key(tool_definitions, 'tasks') }}"
  loop_control:
    loop_var: tool_name
```

**pipx scope:** `hatch` and other pipx-installed tools are installed system-wide via `PIPX_HOME=/opt/pipx` and `PIPX_BIN_DIR=/usr/local/bin`. The task runs as root (inherited from `become: yes` on the play). This makes tools available to all users without per-user installation.

**Included task files:** files referenced by `tasks:` (e.g., `install-gh.yml`) are fully self-contained. The outer loop uses `loop_var: tool_name` (not `item`) to avoid variable shadowing. If any included task file ever uses a loop, that inner loop must also declare a distinct `loop_var` (e.g., `loop_var: pkg`) — using the default `item` inside an included file works, but adding an explicit `loop_var` on inner loops is required for correctness.

---

## Per-Tool Task Files

`install-gh.yml`:

```yaml
- name: Add GitHub CLI apt signing key
  get_url:
    url: https://cli.github.com/packages/githubcli-archive-keyring.gpg
    dest: /usr/share/keyrings/githubcli-archive-keyring.gpg
    mode: '0644'

- name: Add GitHub CLI apt repository
  apt_repository:
    repo: "deb [arch={{ 'amd64' if ansible_architecture == 'x86_64' else ansible_architecture }} signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main"
    filename: github-cli
    state: present

- name: Install gh CLI
  apt:
    name: gh
    state: present
    update_cache: yes
```

**Note:** `ansible_architecture` returns `x86_64` on amd64 hosts but GitHub CLI's apt repo uses Debian arch names (`amd64`). The mapping `{{ 'amd64' if ansible_architecture == 'x86_64' else ansible_architecture }}` handles this. The homelab inventory is currently amd64-only.

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

  tasks: []
```

`install-basic-tools.yml` is deleted.

---

## `deploy-claude-runner.yml` Changes

Remove the inline `gnupg` install task and all GitHub CLI tasks from `ansible/roles/claude-runner/tasks/main.yml` (the hotfix added after the `apt_repository` failure). They are now handled by the tools role.

The complete resulting `deploy-claude-runner.yml` (no `tasks: []` needed — the `roles:` block is present):

```yaml
---
- name: Deploy claude-runner service manager
  hosts: claude
  become: true

  vars:
    required_tools: [gh]

  pre_tasks:
    - name: Install required tools
      import_role:
        name: tools

  roles:
    - claude-runner
```

Execution order: `pre_tasks` (tools role) run before `roles` (claude-runner), ensuring `gh` is installed before the claude-runner role tasks execute.

---

## Files Changed

| File | Change |
|------|--------|
| `ansible/filter_plugins/resolve_deps.py` | New — `resolve_tool_deps` and `tools_with_key` filters |
| `ansible/roles/tools/defaults/main.yml` | New — `required_tools: []` default |
| `ansible/roles/tools/meta/main.yml` | New — basic role metadata |
| `ansible/roles/tools/vars/main.yml` | New — tool registry |
| `ansible/roles/tools/tasks/main.yml` | New — resolve + install loop |
| `ansible/roles/tools/tasks/install-gh.yml` | New — migrated from claude-runner |
| `ansible/roles/tools/tasks/install-cloudflared.yml` | New — placeholder |
| `ansible/install-default-tools.yml` | New — replaces install-basic-tools.yml |
| `ansible/install-basic-tools.yml` | Deleted |
| `ansible/roles/claude-runner/tasks/main.yml` | Remove gnupg + gh CLI tasks; retain `nodejs`, `npm`, `expect` apt task |
| `ansible/deploy-claude-runner.yml` | Add `required_tools` var + tools pre_task |

---

## Role Defaults and Metadata

`roles/tools/defaults/main.yml`:
```yaml
required_tools: []
```

A play that imports the tools role without setting `required_tools` will resolve an empty list and install nothing. No error is raised.

`roles/tools/meta/main.yml`:
```yaml
galaxy_info:
  role_name: tools
  author: homelab
  description: Install system tools with dependency resolution
```

**Collection requirement:** `community.general` must be installed on the Ansible controller before running any playbook that uses this role:
```
ansible-galaxy collection install community.general
```
There is no supported mechanism to declare collection dependencies within a role's `meta/main.yml` that triggers automatic installation. This is an operator prerequisite, documented in Out of Scope.

---

## Migration Note: `hatch` Install Location

`install-basic-tools.yml` installs `hatch` per-user via pipx (under `~/.local/bin/hatch` for whoever runs the playbook). `install-default-tools.yml` installs it system-wide via `PIPX_HOME=/opt/pipx` / `PIPX_BIN_DIR=/usr/local/bin`.

Hosts that ran the old playbook will retain the user-scoped `hatch` binary. This does not cause conflicts (the system-wide install takes precedence in `PATH` for most shell configurations), but the old binary can be cleaned up manually with `pipx uninstall hatch` as the original user.

---

## Out of Scope

- No changes to the `tools` role handler for service restarts — tools don't need restart notification
- `community.general` collection is assumed to be installed (`ansible-galaxy collection install community.general`)
- No version pinning for tool installations
- `install-cloudflared.yml` content is deferred — placeholder only
