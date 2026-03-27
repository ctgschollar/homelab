# Ansible Tools Role Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a shared `tools` Ansible role that installs system tools with automatic transitive dependency resolution, replacing ad-hoc per-role dependency installation.

**Architecture:** A custom Jinja2 filter plugin performs a depth-first topological sort on the `tool_definitions` registry to expand a playbook's `required_tools` list into an ordered, deduplicated install sequence. The tools role iterates this resolved list and dispatches to apt, pipx, or per-tool task files as appropriate. Playbooks declare only direct tool requirements; the role handles the rest.

**Tech Stack:** Ansible (roles, filter_plugins, tasks), Python 3 (filter plugin), `community.general.pipx` module

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `ansible/filter_plugins/resolve_deps.py` | Create | `resolve_tool_deps` and `tools_with_key` Jinja2 filters |
| `ansible/filter_plugins/test_resolve_deps.py` | Create | Unit tests for filter plugin |
| `ansible/roles/tools/defaults/main.yml` | Create | `required_tools: []` safe default |
| `ansible/roles/tools/meta/main.yml` | Create | Role metadata |
| `ansible/roles/tools/vars/main.yml` | Create | `tool_definitions` registry |
| `ansible/roles/tools/tasks/main.yml` | Create | Resolve deps + conditional install blocks |
| `ansible/roles/tools/tasks/install-gh.yml` | Create | gh CLI apt key + repo + package |
| `ansible/roles/tools/tasks/install-cloudflared.yml` | Create | Placeholder |
| `ansible/install-default-tools.yml` | Create | Replaces `install-basic-tools.yml` |
| `ansible/install-basic-tools.yml` | Delete | Replaced by above |
| `ansible/roles/claude-runner/tasks/main.yml` | Modify | Remove gnupg + gh CLI tasks (lines 27-48) |
| `ansible/deploy-claude-runner.yml` | Modify | Add `required_tools: [gh]` + tools `pre_tasks` |

---

## Task 1: Filter Plugin — Tests

**Files:**
- Create: `ansible/filter_plugins/test_resolve_deps.py`

- [ ] **Step 1: Create the test file**

```python
# ansible/filter_plugins/test_resolve_deps.py
import pytest
from ansible.errors import AnsibleFilterError


TOOL_DEFS = {
    "gnupg": {"apt": ["gnupg"], "deps": []},
    "python3-pip": {"apt": ["python3-pip"], "deps": []},
    "pipx": {"apt": ["pipx"], "deps": ["python3-pip"]},
    "hatch": {"pipx": "hatch", "deps": ["pipx"]},
    "gh": {"deps": ["gnupg"], "tasks": "install-gh.yml"},
}


def resolve(tools):
    from resolve_deps import resolve_tool_deps
    return resolve_tool_deps(tools, TOOL_DEFS)


def with_key(tools, key):
    from resolve_deps import tools_with_key
    return tools_with_key(tools, TOOL_DEFS, key)


class TestResolveToolDeps:
    def test_single_tool_no_deps(self):
        assert resolve(["gnupg"]) == ["gnupg"]

    def test_single_tool_with_dep(self):
        assert resolve(["gh"]) == ["gnupg", "gh"]

    def test_chain(self):
        assert resolve(["hatch"]) == ["python3-pip", "pipx", "hatch"]

    def test_multiple_tools_shared_dep_deduped(self):
        # gh needs gnupg; gnupg should appear once
        result = resolve(["gnupg", "gh"])
        assert result.count("gnupg") == 1
        assert result.index("gnupg") < result.index("gh")

    def test_ordering_deps_before_dependents(self):
        result = resolve(["gh", "hatch"])
        assert result.index("gnupg") < result.index("gh")
        assert result.index("python3-pip") < result.index("pipx")
        assert result.index("pipx") < result.index("hatch")

    def test_full_example_from_spec(self):
        result = resolve(["gh", "hatch"])
        assert result == ["gnupg", "gh", "python3-pip", "pipx", "hatch"]

    def test_empty_input(self):
        assert resolve([]) == []

    def test_unknown_tool_raises(self):
        with pytest.raises(AnsibleFilterError, match="Unknown tool"):
            resolve(["nonexistent"])

    def test_cycle_raises(self):
        cyclic = {
            "a": {"deps": ["b"], "apt": ["a"]},
            "b": {"deps": ["a"], "apt": ["b"]},
        }
        from resolve_deps import resolve_tool_deps
        with pytest.raises(AnsibleFilterError, match="cycle"):
            resolve_tool_deps(["a"], cyclic)


class TestToolsWithKey:
    def test_apt_tools(self):
        resolved = ["gnupg", "gh", "python3-pip", "pipx", "hatch"]
        assert with_key(resolved, "apt") == ["gnupg", "python3-pip", "pipx"]

    def test_pipx_tools(self):
        resolved = ["gnupg", "gh", "python3-pip", "pipx", "hatch"]
        assert with_key(resolved, "pipx") == ["hatch"]

    def test_tasks_tools(self):
        resolved = ["gnupg", "gh", "python3-pip", "pipx", "hatch"]
        assert with_key(resolved, "tasks") == ["gh"]

    def test_empty_resolved(self):
        assert with_key([], "apt") == []

    def test_key_not_present_in_any(self):
        assert with_key(["gnupg"], "tasks") == []
```

- [ ] **Step 2: Run the tests to verify they fail (implementation doesn't exist yet)**

```bash
cd ansible/filter_plugins
python -m pytest test_resolve_deps.py -v 2>&1 | head -20
```

Expected: errors like `ModuleNotFoundError` or import failures — not a clean pass.

---

## Task 2: Filter Plugin — Implementation

**Files:**
- Create: `ansible/filter_plugins/resolve_deps.py`

- [ ] **Step 1: Create the filter plugin**

```python
# ansible/filter_plugins/resolve_deps.py
from __future__ import annotations

from ansible.errors import AnsibleFilterError


def resolve_tool_deps(required_tools: list, tool_definitions: dict) -> list:
    """
    Depth-first topological sort of required_tools using tool_definitions deps.
    Returns a deduplicated ordered list where every dependency precedes its dependent.
    First-visit wins for deduplication.
    """
    visited: set = set()
    result: list = []

    def visit(name: str, stack: list) -> None:
        if name in stack:
            raise AnsibleFilterError(
                f"Dependency cycle detected: {' -> '.join(stack + [name])}"
            )
        if name in visited:
            return
        if name not in tool_definitions:
            raise AnsibleFilterError(
                f"Unknown tool: '{name}'. Available: {sorted(tool_definitions.keys())}"
            )
        for dep in tool_definitions[name].get("deps", []):
            visit(dep, stack + [name])
        visited.add(name)
        result.append(name)

    for tool in required_tools:
        visit(tool, [])

    return result


def tools_with_key(resolved_tools: list, tool_definitions: dict, key: str) -> list:
    """
    Return the subset of resolved_tools whose tool_definitions entry contains key.
    Preserves order of resolved_tools.
    """
    return [t for t in resolved_tools if key in tool_definitions.get(t, {})]


class FilterModule:
    def filters(self) -> dict:
        return {
            "resolve_tool_deps": resolve_tool_deps,
            "tools_with_key": tools_with_key,
        }
```

- [ ] **Step 2: Run the tests**

```bash
cd ansible/filter_plugins
python -m pytest test_resolve_deps.py -v
```

Expected output: all tests pass. Example:
```
PASSED test_resolve_deps.py::TestResolveToolDeps::test_single_tool_no_deps
PASSED test_resolve_deps.py::TestResolveToolDeps::test_full_example_from_spec
...
13 passed in 0.XX s
```

- [ ] **Step 3: Commit**

```bash
git add ansible/filter_plugins/resolve_deps.py ansible/filter_plugins/test_resolve_deps.py
git commit -m "feat: add resolve_tool_deps and tools_with_key filter plugins"
```

---

## Task 3: Tools Role Skeleton

**Files:**
- Create: `ansible/roles/tools/defaults/main.yml`
- Create: `ansible/roles/tools/meta/main.yml`
- Create: `ansible/roles/tools/vars/main.yml`

- [ ] **Step 1: Create defaults/main.yml**

```yaml
---
# ansible/roles/tools/defaults/main.yml
required_tools: []
```

- [ ] **Step 2: Create meta/main.yml**

```yaml
---
# ansible/roles/tools/meta/main.yml
galaxy_info:
  role_name: tools
  author: homelab
  description: Install system tools with dependency resolution
```

- [ ] **Step 3: Create vars/main.yml**

```yaml
---
# ansible/roles/tools/vars/main.yml
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

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/tools/defaults/main.yml \
        ansible/roles/tools/meta/main.yml \
        ansible/roles/tools/vars/main.yml
git commit -m "feat: add tools role skeleton (defaults, meta, vars)"
```

---

## Task 4: Tools Role Task Files

**Files:**
- Create: `ansible/roles/tools/tasks/main.yml`
- Create: `ansible/roles/tools/tasks/install-gh.yml`
- Create: `ansible/roles/tools/tasks/install-cloudflared.yml`

- [ ] **Step 1: Create tasks/main.yml**

```yaml
---
# ansible/roles/tools/tasks/main.yml

- name: Resolve tool dependencies
  set_fact:
    resolved_tools: "{{ required_tools | resolve_tool_deps(tool_definitions) }}"

- name: Collect apt tools
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

- [ ] **Step 2: Create tasks/install-gh.yml**

```yaml
---
# ansible/roles/tools/tasks/install-gh.yml

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

- [ ] **Step 3: Create tasks/install-cloudflared.yml (placeholder)**

```yaml
---
# ansible/roles/tools/tasks/install-cloudflared.yml
# Placeholder — content to be added when cloudflared is needed
- name: cloudflared install not yet implemented
  fail:
    msg: "install-cloudflared.yml is a placeholder. Implement before using cloudflared in required_tools."
```

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/tools/tasks/
git commit -m "feat: add tools role task files"
```

---

## Task 5: Replace install-basic-tools.yml

**Files:**
- Create: `ansible/install-default-tools.yml`
- Delete: `ansible/install-basic-tools.yml`

- [ ] **Step 1: Create install-default-tools.yml**

```yaml
---
# ansible/install-default-tools.yml
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

- [ ] **Step 2: Delete install-basic-tools.yml**

```bash
git rm ansible/install-basic-tools.yml
```

- [ ] **Step 3: Commit**

```bash
git add ansible/install-default-tools.yml
git commit -m "feat: add install-default-tools.yml, remove install-basic-tools.yml"
```

---

## Task 6: Update deploy-claude-runner.yml

**Files:**
- Modify: `ansible/deploy-claude-runner.yml`

Current content of `ansible/deploy-claude-runner.yml`:
```yaml
---
- name: Deploy claude-runner service manager
  hosts: claude
  become: true
  roles:
    - claude-runner
```

- [ ] **Step 1: Rewrite deploy-claude-runner.yml**

Replace the entire file with:

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

- [ ] **Step 2: Commit**

```bash
git add ansible/deploy-claude-runner.yml
git commit -m "feat: use tools role for gh CLI in deploy-claude-runner"
```

---

## Task 7: Clean Up claude-runner Role

**Files:**
- Modify: `ansible/roles/claude-runner/tasks/main.yml`

Remove lines 27-48 (the gnupg install task and all three GitHub CLI tasks). Keep all other tasks intact.

Current lines 27-48 to remove:
```yaml
- name: Install gnupg (required by apt_repository module)
  apt:
    name: gnupg
    state: present

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

The resulting file after removal should have the "Install system dependencies" task (nodejs, npm, expect) immediately followed by the "Install Claude Code globally" npm task.

- [ ] **Step 1: Remove the four tasks from claude-runner/tasks/main.yml**

Edit `ansible/roles/claude-runner/tasks/main.yml` to remove lines 27-48. The result should be:

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

- name: Install system dependencies
  apt:
    name:
      - nodejs
      - npm
      - expect
    state: present
    update_cache: yes

- name: Install Claude Code globally
  npm:
    name: "@anthropic-ai/claude-code"
    global: yes

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

- name: Create logs directory
  file:
    path: "{{ claude_runner_base_dir }}/logs"
    state: directory
    owner: "{{ claude_user }}"
    group: "{{ claude_user }}"
    mode: '0755'

- name: Create tasks directory
  file:
    path: "{{ claude_runner_base_dir }}/tasks"
    state: directory
    owner: root
    group: root
    mode: '0755'

- name: Create accounts.conf (skip if already populated)
  copy:
    content: ""
    dest: "{{ claude_runner_base_dir }}/accounts.conf"
    owner: root
    group: root
    mode: '0644'
    force: no

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
git commit -m "refactor: remove gnupg and gh CLI tasks from claude-runner role (now via tools role)"
```

---

## Verification

After all tasks are complete, verify the filter plugin still passes its tests:

```bash
cd ansible/filter_plugins && python -m pytest test_resolve_deps.py -v
```

Ensure the `community.general` collection is installed on the Ansible controller (required for the `community.general.pipx` module):

```bash
ansible-galaxy collection install community.general
```

To verify the playbooks are syntactically valid (requires Ansible installed):

```bash
ansible-playbook --syntax-check ansible/deploy-claude-runner.yml
ansible-playbook --syntax-check ansible/install-default-tools.yml
```

Expected: `playbook: ansible/deploy-claude-runner.yml` with no errors.

**Note:** A live deployment test (`ansible-playbook ansible/deploy-claude-runner.yml`) is required to fully validate the tools role. This should be run against the `claude` host in inventory after all tasks are committed.
