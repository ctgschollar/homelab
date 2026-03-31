# Ansible Directory

## Tools Role

The `roles/tools` role provides a dependency-aware tool installer. Roles and playbooks declare which tools they need; the role handles installation order and deduplication.

### How it works

**`roles/tools/vars/main.yml`** — the registry of all known tools. Each entry has:

```yaml
tool_name:
  deps: [other_tool, ...]   # tools that must be installed first
  apt: [pkg1, pkg2]         # install via apt (mutually exclusive with tasks/pipx)
  pipx: package-name        # install via pipx (mutually exclusive with apt/tasks)
  tasks: install-foo.yml    # custom task file in roles/tools/tasks/ (for binaries)
```

A tool can use `apt`, `pipx`, or `tasks` — not multiple. `deps` is always optional.

**`filter_plugins/resolve_deps.py`** — two Jinja2 filters:
- `resolve_tool_deps(required_tools, tool_definitions)` — depth-first topological sort; returns deduplicated ordered list with deps before dependents
- `tools_with_key(resolved_tools, tool_definitions, key)` — filters the resolved list to tools that have a given install key (`apt`, `pipx`, or `tasks`)

### Adding a new tool

1. **Simple apt package** — add to `vars/main.yml`:
   ```yaml
   mytool:
     apt: [mytool-package]
     deps: []
   ```

2. **Binary from a release URL** — add to `vars/main.yml` and create a task file:
   ```yaml
   mytool:
     deps: [curl]
     tasks: install-mytool.yml
   ```
   Then create `roles/tools/tasks/install-mytool.yml`. Follow the pattern in `install-tea.yml` or `install-act.yml`:
   - Fetch latest release from the API
   - Download the binary (or tarball) with `get_url`
   - For tarballs, use `unarchive` with `remote_src: yes` and `include: [binary-name]`
   - Place the binary at `/usr/local/bin/<name>` with mode `0755`

### Using tools from a playbook

Pass `required_tools` as a var and import the role:

```yaml
vars:
  required_tools: [tea, jq, act]

pre_tasks:
  - name: Install required tools
    import_role:
      name: tools
```

The role resolves deps automatically — you only need to list the tools you directly need, not their transitive dependencies.

### Current tools

| Name | Install method | Notes |
|------|---------------|-------|
| `jq` | apt | JSON processor |
| `git` | apt | |
| `curl` | apt | |
| `wget` | apt | |
| `vim` | apt | |
| `gnupg` | apt | Required by gh, cloudflared |
| `python3-pip` | apt | Required by pipx |
| `pipx` | apt | Required by hatch |
| `hatch` | pipx | Python project manager |
| `tea` | binary (gitea.com) | Gitea CLI |
| `gh` | binary (GitHub) | GitHub CLI |
| `cloudflared` | binary (Cloudflare) | Cloudflare tunnel |
| `act` | binary (github.com/nektos/act) | Local Gitea/GitHub Actions runner |

## Roles

### `claude-runner`

Deploys the autonomous Claude Code runner system to the `claude` host (`192.168.3.79`).

**Deploy:** `ansible-playbook deploy-claude-runner.yml -i inventory.yml`

**Required tools:** `tea`, `jq`, `act` (declared in `deploy-claude-runner.yml`)

See `roles/claude-runner/README.md` for full documentation of the runner system.

### `tools`

Installs system tools with dependency resolution. Used as a pre-task by other playbooks.

### `traefik-edge`

Deploys Traefik reverse proxy to edge nodes.

### `cloudflared`

Deploys Cloudflare tunnel daemon.
