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

## Privilege Escalation

All managed hosts connect as `root` directly (`ansible_user: root`) and do **not** have `sudo` installed. Any task that needs `become_user` must explicitly set `become_method: su`. Never rely on the default `sudo` become method.

## Roles

### `runner`

Deploys the autonomous Claude Code runner system to the `claude` host (`192.168.3.79`).

**Deploy:** `ansible-playbook deploy-runner.yml -i inventory.yml --extra-vars "gitea_pypi_token=<token>"`

**Required tools:** `tea`, `jq` (declared in `deploy-runner.yml`)

**What it does:**
1. Creates the `claude` system user (`/home/claude`)
2. Installs system deps: `python3`, `python3-pip`, `pipx`, `nodejs`, `npm`, `git`
3. Installs Claude Code globally via npm (`@anthropic-ai/claude-code`)
4. Installs `claude-runner` from Gitea PyPI **as the `claude` user** via `pipx install --force`, so the binary lands at `/home/claude/.local/bin/`
5. Creates base directories under `CLAUDE_RUNNER_BASE_DIR` (`/opt/claude-runner`)
6. Installs and starts the `claude-runner-api` systemd service

**Auth:** No `ANTHROPIC_API_KEY` needed. Claude Code credentials are stored in `/home/claude/.claude/` after the first interactive login (which happens during `claude-runner new`).

**Binary path:** `/home/claude/.local/bin/claude-runner-api` (installed by pipx as the `claude` user)

**Key vars** (in `roles/runner/defaults/main.yml`):

| Var | Default |
|-----|---------|
| `runner_base_dir` | `/opt/claude-runner` |
| `runner_port` | `8080` |
| `claude_user` | `claude` |
| `claude_user_home` | `/home/claude` |
| `gitea_pypi_token` | `""` — must be passed via `--extra-vars` |

See `runner/CLAUDE.md` in the repo root for the runner's own development docs.

### `tools`

Installs system tools with dependency resolution. Used as a pre-task by other playbooks.

### `traefik-edge`

Deploys Traefik reverse proxy to edge nodes.

### `cloudflared`

Deploys Cloudflare tunnel daemon.
