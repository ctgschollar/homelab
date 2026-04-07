# Centralised Logging — Design Spec

**Date:** 2026-04-07
**Stack:** PLG (Promtail + Loki + Grafana)

---

## Overview

Centralised log collection and querying for all homelab nodes and services. Logs are shipped from every Docker Swarm node (via a global Promtail service) and every Ansible-managed host (via a systemd Promtail service) into a single Loki instance, queryable via the existing Grafana deployment.

---

## Non-Negotiables

- Per-service and per-host automatic log labelling (no manual config per service)
- String/ID query capability (LogQL substring and regex support)
- 7-day hot retention minimum
- S3-compatible archival tier (future work — architecture must support it)
- Central store: ≤ 4GB RAM
- Per-node collectors: < 200MB RAM each

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                Docker Swarm Cluster                  │
│  dks01  dks02  dks03  dks04  dks05                  │
│  ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐                │
│  │ PT │ │ PT │ │ PT │ │ PT │ │ PT │  ← global svc   │
│  └──┬─┘ └──┬─┘ └──┬─┘ └──┬─┘ └──┬─┘                │
└─────┼───────┼───────┼───────┼───────┼───────────────┘
      └───────┴───────┴───────┴───────┘
                        │ push (logging-net overlay)
                 ┌──────▼──────┐
                 │    Loki     │  logging/ stack
                 └──────┬──────┘  port 3100 published
                        │ datasource (logging-net overlay)
                 ┌──────▼──────┐
                 │   Grafana   │  monitoring/ stack (existing)
                 └─────────────┘

Ansible-managed hosts (systemd promtail)
┌────────────────────────────────────────────────────────────┐
│  VMs:      claude  edge01  gt-runner-01                    │
│  Proxmox:  prx01  prx02  prx03  prx04  prx05              │
│                                                            │
│  Each runs promtail as a systemd service:                  │
│    - full systemd journal (all units, labelled by unit)    │
│    - configurable extra file paths per role                │
└────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. `logging/` Docker Swarm Stack

New stack at `logging/docker-compose.yaml` containing two services: Loki and Promtail (global).

#### Loki

- **Mode:** single binary / filesystem storage
- **Retention:** 7 days (`limits_config.retention_period: 168h`)
- **Storage:** Linstor HDD volume, 30GB, `pool_hdd`, 2 replicas
- **Network:** `logging-net` (new external overlay network); port 3100 published via Swarm ingress for Ansible-managed hosts
- **No Traefik exposure** — internal only
- **Placement:** `node.labels.linstor==true`, exclude dks05
- **S3 migration path:** Loki's filesystem mode config can be swapped to `s3` storage backend by updating `storage_config` and `schema_config` — no schema changes required if planned from day one

#### Promtail (Swarm global service)

- **Mode:** `global` — one instance per Swarm node automatically
- **Host mounts:**
  - `/var/run/docker.sock` — Docker service discovery
  - `/var/lib/docker/containers` — container log files (read-only)
  - `/run/log/journal` — systemd journal (read-only)
  - `/etc/machine-id` — journal host identification (read-only)
- **Labels extracted from Docker:**
  - `swarm_service` — Swarm service name (e.g. `monitoring_grafana`)
  - `container_name`
  - `swarm_node` — node hostname
  - `stack` — Swarm stack name (compose project)
- **Labels extracted from journal:**
  - `host` — node hostname
  - `unit` — `_SYSTEMD_UNIT` value
- **Static label:** `job: swarm-node`

### 2. Ansible `promtail` Role

New role at `ansible/roles/promtail/`.

**Responsibilities:**
- Download and install the Promtail binary (version-pinned via `promtail_version`)
- Template `/etc/promtail/config.yml`
- Install and enable `promtail.service` systemd unit
- Open no firewall ports (outbound push only)

**Defaults (`defaults/main.yml`):**
```yaml
promtail_version: "3.0.0"
promtail_loki_url: "http://dks01.schollar.dev:3100"  # any Swarm node works — Swarm ingress routes to Loki
promtail_extra_paths: []   # list of file glob strings
```

`promtail_loki_url` can be overridden in `group_vars/all.yml` if a dedicated VIP or DNS alias is preferred. Any Swarm node hostname resolves correctly because Swarm ingress routing forwards port 3100 to the Loki container regardless of which node receives the connection.

**Config template behaviour:**
- Scrapes full systemd journal — no unit filter, all services collected automatically
- `_SYSTEMD_UNIT` promoted to `unit` label
- `_HOSTNAME` promoted to `host` label
- Each entry in `promtail_extra_paths` added as a file scrape target with `filename` label derived from path
- Static label `job: ansible-host`

**Integration convention:** Any playbook in this repo that deploys a systemd service includes the promtail role at the end:
```yaml
- import_role:
    name: promtail
  vars:
    promtail_extra_paths:
      - "/opt/my-service/logs/*.log"   # only if writing outside journal
```
The role is idempotent — safe to run repeatedly.

### 3. New Playbook: `deploy-logging-agents.yml`

Targets all Ansible-managed hosts that are not Swarm nodes (which use the global Swarm service instead):

```yaml
hosts: proxmox_vms:!swarm_nodes, proxmox_nodes
roles:
  - tools
  - promtail
```

This is the one-shot playbook to bootstrap all non-Swarm hosts. After initial deployment, logging is maintained by including promtail in each service's own deploy playbook.

### 4. Updated Playbooks

The following existing playbooks get the promtail role appended:
- `deploy-claude-runner.yml`
- `deploy-edge.yml`
- `deploy-gitea-runner.yml`

The claude-runner role produces multiple template unit instances (`claude-runner@<queue>.service`, `claude-watcher@<queue>.service`). Because Promtail collects the full journal with unit labels, all queue instances appear automatically in Loki with `unit=~"claude-runner@.*"` — no extra config needed.

### 5. Inventory Changes

Add a `proxmox_nodes` group to `ansible/inventory.yml` for prx01–prx05. Add a `swarm_nodes` group for dks01–dks05 (used to exclude them from `deploy-logging-agents.yml`).

```yaml
proxmox_nodes:
  hosts:
    prx01:
      ansible_host: prx01.schollar.dev   # replace with actual IPs/hostnames
    prx02:
      ansible_host: prx02.schollar.dev
    prx03:
      ansible_host: prx03.schollar.dev
    prx04:
      ansible_host: prx04.schollar.dev
    prx05:
      ansible_host: prx05.schollar.dev

swarm_nodes:
  hosts:
    dks01:
    dks02:
    dks03:
    dks04:
    dks05:
```

### 6. Grafana Datasource Provisioning

Add `monitoring/provisioning/datasources/loki.yml`:
```yaml
datasources:
  - name: Loki
    type: loki
    url: http://loki:3100
    isDefault: false
    version: 1
```

Grafana service in `monitoring/docker-compose.yaml` gets `logging-net` added to its `networks` list so it can reach Loki via the overlay.

The `logging-net` overlay network must be created externally (same pattern as `traefik-net`):
```bash
docker network create --driver overlay --attachable logging-net
```

---

## Data Flow

```
Container logs  ──► Docker socket / container log files
                         │
                    Promtail (Swarm global)
                         │  labels: swarm_service, container_name, swarm_node, stack
                         ▼
Systemd journal ──► Promtail (Swarm global)
                         │  labels: host, unit
                         ▼
                      Loki :3100  (logging-net)
                         │
                      Grafana (logging-net + traefik-net)
                         │
                      User query via https://grafana.schollar.dev


VM/Proxmox journal ──► Promtail (systemd)
                             │  labels: host, unit, job=ansible-host
                             │  extra file paths (optional per role)
                             ▼
                          Loki :3100  (published port, Swarm ingress)
```

---

## Storage Sizing

| Component | Backend | Size | Pool |
|-----------|---------|------|------|
| Loki data | Linstor volume | 30GB | pool_hdd |

30GB is a conservative estimate for 7 days across ~10 hosts + all Swarm services. Loki's chunk compression typically achieves 5–10x compression on plain text logs.

---

## Retention & Future Archival

**Hot retention:** 7 days, enforced by Loki's compactor. Configured via `limits_config.retention_period: 168h` and `compactor.retention_enabled: true`.

**Archival (future):** When a MinIO or Ceph RADOS Gateway S3 endpoint is available:
1. Update Loki `storage_config` to use `s3` backend instead of `filesystem`
2. Migrate existing chunks (Loki provides tooling for this)
3. Set tiered retention: 7 days in S3 hot tier, archive older chunks to cold storage

No schema changes are required — Loki's TSDB index schema is backend-agnostic.

---

## Querying

All logs queryable via Grafana's Explore view using LogQL.

Example queries:
```logql
# All logs for a specific Swarm service
{swarm_service="monitoring_grafana"}

# All claude-runner queue instances
{job="ansible-host", unit=~"claude-runner@.*"}

# Search for a specific ID across all logs
{host="claude"} |= "some-request-id"

# Container logs from the logging stack on a specific node
{swarm_service=~"logging_.*", swarm_node="dks01"}

# All journal logs from edge node
{job="ansible-host", host="edge01"}
```

---

## Out of Scope

- S3/MinIO deployment (future archival work)
- Log-based alerting via Loki ruler (future)
- Log parsing / structured field extraction beyond labels (future)
- Authentication on the Loki push endpoint (internal network only — acceptable for homelab)
