# Homelab — Claude Code Instructions

## Repo Purpose

This repo holds all deployable configuration for a homelab. Two concerns:

1. **Docker Swarm stacks** — one directory per service under the repo root (e.g. `gitea/`, `traefik/`), each containing a `docker-compose.yaml`
2. **Ansible roles/playbooks** — under `ansible/`, for anything that runs directly on a host (not in Swarm)

---

## Infrastructure

| Layer | Nodes | Notes |
|-------|-------|-------|
| Proxmox hypervisors | prx01–prx05 | Bare metal |
| Docker Swarm | dks01–dks05 | One VM per hypervisor |
| Edge node | 192.168.3.91 | Cloudflared tunnel + local Traefik |
| Claude runner VM | 192.168.3.79 (`claude`) | Runs the autonomous Claude Code service |

- Domain: `*.schollar.dev`
- Reverse proxy: Traefik with Let's Encrypt via Cloudflare DNS-01
- Git hosting: self-hosted Gitea at `gitea.schollar.dev` — **always use `tea` CLI, never `gh`**

---

## Node Labels (Docker Swarm placement)

| Label | Meaning | Which nodes |
|-------|---------|-------------|
| `node.labels.media == true` | Collocates torrent, Jellyfin, large media disk | dks05 only |
| `node.labels.traefik == true` | Traefik proxy instances | Any node except dks05 |
| `node.labels.linstor == true` | Node has a local Linstor disk | All nodes (Linstor volumes are accessible over network from any node; local colocation is future work) |

Default placement for new services: no constraint needed unless the service specifically requires media or must be off the media node.

---

## Storage

| Backend | Use case | Notes |
|---------|----------|-------|
| Linstor volumes | Persistent container data | Fast, HA, 2 replicas; use `pool_ssd` or `pool_hdd` |
| CephFS (`/mnt/cephfs-configs/`) | Setup config delivered at deploy time | **Never mount CephFS into a running container** — cluster is too slow; CephFS is for backups and pre-deploy config only |
| `/mnt/shared/` | Shared media | Large files accessed by media services |

---

## Secrets

Prefer **Docker Swarm secrets** (`docker secret create` + `secrets:` in compose). Fall back to a CephFS `.env` file (`env_file: /mnt/cephfs-configs/{service}/.env`) only when the container image does not support reading secrets from files.

---

## Deployment

- **Stacks**: deployed via Portainer's repo/stack mechanism, linked to this repo. To deploy a change, push to the repo and redeploy via Portainer.
- **Ansible playbooks**: deployed manually (`ansible-playbook -i inventory.yml <playbook>.yml`) — no automation.

---

## Docker Swarm Stack Conventions

Always use this structure for new stacks:

```yaml
networks:
  traefik-net:
    external: true

volumes:
  service_data:
    driver: linbit/linstor-docker-volume
    driver_opts:
      size: "10G"
      fs: "xfs"
      replicas: "2"
      storagepool: "pool_ssd"   # or pool_hdd

services:
  service-name:
    image: your/service:latest
    networks: [traefik-net]
    volumes:
      - service_data:/app/data
    deploy:
      mode: replicated
      replicas: 1
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 0
      placement:
        constraints:
          - "node.labels.linstor==true"   # adjust as needed
      update_config:
        order: start-first
        parallelism: 1
        failure_action: rollback
      labels:
        traefik.enable: "true"
        traefik.docker.network: traefik-net
        traefik.http.routers.service.rule: "Host(`service.schollar.dev`)"
        traefik.http.routers.service.entrypoints: websecure
        traefik.http.routers.service.tls.certresolver: cf
        traefik.http.services.service.loadbalancer.server.port: "8080"
        prometheus.blackbox: "true"
        metrics.probe_url: "https://service.schollar.dev"
```

Key rules:
- Always attach to `traefik-net` external network
- Use `condition: on-failure` restart policy (not `restart: always` — incompatible with Swarm)
- Linstor volumes for all persistent data
- Prometheus blackbox labels on all public-facing services

---

## Ansible

### Playbooks

| Playbook | Target | Purpose |
|----------|--------|---------|
| `deploy-claude-runner.yml` | `claude` host | Deploys the autonomous Claude Code service |
| `deploy-edge.yml` | `edge_nodes` | Deploys cloudflared + traefik-edge |
| `deploy-traefik-edge.yml` | `edge_nodes` | Traefik edge only |
| `install-default-tools.yml` | varies | Installs common tools |

### `tools` Role — Dependency Management

The `tools` role installs system tools with automatic dependency resolution. It uses two custom Ansible filter plugins (`ansible/filter_plugins/resolve_deps.py`):

- **`resolve_tool_deps`** — topological sort of `required_tools` against `tool_definitions`; detects cycles
- **`tools_with_key`** — filters resolved tools to those that have a specific install key (`apt`, `pipx`, `tasks`)

**To use in a playbook:**

```yaml
vars:
  required_tools: [tea, jq]   # just list what you need — deps resolved automatically

pre_tasks:
  - import_role:
      name: tools
```

**To add a new tool**, edit `ansible/roles/tools/vars/main.yml`:

```yaml
tool_definitions:
  mytool:
    apt: [mytool]       # apt package name(s)
    deps: [curl]        # other tools this depends on
    # OR:
    pipx: mytool        # install via pipx instead of apt
    # OR:
    tasks: install-mytool.yml  # run a custom task file from roles/tools/tasks/
```

Install methods are not mutually exclusive — a tool can have `apt` + `tasks`, etc. Dependencies are always installed first.

### Python in Ansible/Scripts

**Always use `hatch` environments — never the system Python.**

```bash
hatch run python script.py
hatch run pytest
```

The project uses `pyproject.toml` in `ansible/` for dependency management.

---

## Claude Runner (autonomous Claude Code service)

The `ansible/roles/claude-runner` role and `deploy-claude-runner.yml` playbook deploy an autonomous Claude Code service on the `claude` VM (192.168.3.79). This is a systemd-managed service. See `ansible/roles/claude-runner/` for implementation details.

Note: there is also a homelab monitoring agent (`agent/`) that can deploy stacks via `docker stack deploy`, but it is still in development and run manually — it is not yet deployed via Ansible.
