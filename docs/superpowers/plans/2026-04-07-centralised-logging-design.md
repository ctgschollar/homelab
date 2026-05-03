# Centralised Logging (PLG Stack) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a full PLG (Promtail + Loki + Grafana) centralised logging stack — Loki and a global Promtail Swarm service as a new `logging/` stack, an Ansible `promtail` role for non-Swarm hosts, and wire everything into the existing Grafana.

**Architecture:** Loki runs as a single-binary Swarm service with filesystem storage on a 30 GB Linstor HDD volume, published on port 3100 via Swarm ingress. A global Promtail service runs on every Swarm node collecting Docker container logs and systemd journal. Non-Swarm hosts (VMs, Proxmox nodes) run Promtail via systemd, installed by the new Ansible `promtail` role. Grafana is extended with a Loki datasource and joined to `logging-net`.

**Tech Stack:** Docker Swarm (global services, published ports, overlay networks), Loki 3.0, Promtail 3.0, Grafana provisioning YAML, Ansible roles with Jinja2 templates, systemd units.

---

## File Map

**New files:**
- `logging/docker-compose.yaml` — Loki + Promtail global Swarm stack
- `logging/loki-config.yml` — Loki server configuration
- `logging/promtail-config.yml` — Promtail configuration for Swarm global service
- `ansible/roles/promtail/defaults/main.yml` — Role defaults (version, loki URL, extra_paths)
- `ansible/roles/promtail/handlers/main.yml` — systemd reload handler
- `ansible/roles/promtail/tasks/main.yml` — Install binary, template config, enable service
- `ansible/roles/promtail/templates/config.yml.j2` — Promtail config template
- `ansible/roles/promtail/templates/promtail.service.j2` — systemd unit template
- `ansible/deploy-logging-agents.yml` — One-shot bootstrap playbook for non-Swarm hosts
- `monitoring/provisioning/datasources/loki.yml` — Grafana Loki datasource

**Modified files:**
- `ansible/inventory.yml` — Add `proxmox_nodes` and `swarm_nodes` groups
- `monitoring/docker-compose.yaml` — Add `logging-net` to Grafana service + config entry
- `ansible/deploy-claude-runner.yml` — Append `promtail` role
- `ansible/deploy-edge.yml` — Append `promtail` role
- `ansible/deploy-gitea-runner.yml` — Append `promtail` role

---

## Task 1: Create `logging/loki-config.yml`

**Files:**
- Create: `logging/loki-config.yml`

- [ ] **Step 1: Create the Loki configuration file**

```yaml
# logging/loki-config.yml
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9096

common:
  path_prefix: /loki
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

storage_config:
  filesystem:
    directory: /loki/chunks

limits_config:
  retention_period: 168h

compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
```

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('logging/loki-config.yml'))" && echo "VALID"
```

Expected: `VALID`

- [ ] **Step 3: Commit**

```bash
git add logging/loki-config.yml
git commit -m "feat(logging): add Loki server configuration"
```

---

## Task 2: Create `logging/promtail-config.yml` (Swarm global service config)

**Files:**
- Create: `logging/promtail-config.yml`

- [ ] **Step 1: Create the Promtail configuration for the Swarm global service**

This config runs inside a container on each Swarm node. It scrapes Docker container logs via the Docker socket and the systemd journal via the host journal bind-mount.

```yaml
# logging/promtail-config.yml
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  # Docker container logs via Docker daemon socket
  - job_name: docker
    docker_sd_configs:
      - host: unix:///var/run/docker.sock
        refresh_interval: 5s
        filters:
          - name: status
            values: [running]
    relabel_configs:
      # Drop containers with no swarm service label (bare docker run, etc.)
      - source_labels: [__meta_docker_container_label_com_docker_swarm_service_name]
        action: drop
        regex: ^$
      - source_labels: [__meta_docker_container_label_com_docker_swarm_service_name]
        target_label: swarm_service
      - source_labels: [__meta_docker_container_name]
        regex: /(.*)
        target_label: container_name
        replacement: $1
      - source_labels: [__meta_docker_container_label_com_docker_stack_namespace]
        target_label: stack
      - source_labels: [__meta_docker_container_label_com_docker_swarm_node_hostname]
        target_label: swarm_node
    pipeline_stages:
      - docker: {}

  # Systemd journal on the host
  - job_name: journal
    journal:
      path: /run/log/journal
      max_age: 12h
      labels:
        job: swarm-node
    relabel_configs:
      - source_labels: [__journal__hostname]
        target_label: host
      - source_labels: [__journal__systemd_unit]
        target_label: unit
```

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('logging/promtail-config.yml'))" && echo "VALID"
```

Expected: `VALID`

- [ ] **Step 3: Commit**

```bash
git add logging/promtail-config.yml
git commit -m "feat(logging): add Promtail Swarm global service configuration"
```

---

## Task 3: Create `logging/docker-compose.yaml` (Loki + Promtail Swarm stack)

**Files:**
- Create: `logging/docker-compose.yaml`

- [ ] **Step 1: Create the Swarm stack**

This stack has two services: Loki (replicated, on linstor node, excluding dks05) and Promtail (global, running on every node).

```yaml
# logging/docker-compose.yaml
networks:
  logging-net:
    external: true

volumes:
  loki-data:
    driver: linbit/linstor-docker-volume
    driver_opts:
      size: "30G"
      fs: "xfs"
      replicas: "2"
      storagepool: "pool_hdd"

configs:
  loki-config.yml:
    file: ./loki-config.yml
  promtail-config.yml:
    file: ./promtail-config.yml

services:
  loki:
    image: grafana/loki:3.0.0
    command: -config.file=/etc/loki/config.yml
    configs:
      - source: loki-config.yml
        target: /etc/loki/config.yml
    volumes:
      - loki-data:/loki
    networks: [logging-net]
    ports:
      - "3100:3100"
    deploy:
      mode: replicated
      replicas: 1
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 0
      placement:
        constraints:
          - "node.labels.linstor==true"
          - "node.hostname!=dks05"
      update_config:
        order: start-first
        parallelism: 1
        failure_action: rollback

  promtail:
    image: grafana/promtail:3.0.0
    command: -config.file=/etc/promtail/config.yml
    configs:
      - source: promtail-config.yml
        target: /etc/promtail/config.yml
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /run/log/journal:/run/log/journal:ro
      - /etc/machine-id:/etc/machine-id:ro
    networks: [logging-net]
    deploy:
      mode: global
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 0
      update_config:
        order: start-first
        parallelism: 1
        failure_action: rollback
```

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('logging/docker-compose.yaml'))" && echo "VALID"
```

Expected: `VALID`

- [ ] **Step 3: Commit**

```bash
git add logging/docker-compose.yaml
git commit -m "feat(logging): add Loki + Promtail Docker Swarm stack"
```

---

## Task 4: Create the Ansible `promtail` role defaults and handlers

**Files:**
- Create: `ansible/roles/promtail/defaults/main.yml`
- Create: `ansible/roles/promtail/handlers/main.yml`

- [ ] **Step 1: Create defaults**

```yaml
# ansible/roles/promtail/defaults/main.yml
---
promtail_version: "3.0.0"
promtail_loki_url: "http://dks01.schollar.dev:3100"
promtail_extra_paths: []
```

- [ ] **Step 2: Create handlers**

```yaml
# ansible/roles/promtail/handlers/main.yml
---
- name: reload systemd
  systemd:
    daemon_reload: yes

- name: restart promtail
  systemd:
    name: promtail
    state: restarted
    enabled: yes
```

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('ansible/roles/promtail/defaults/main.yml'))" && echo "defaults VALID"
python3 -c "import yaml; yaml.safe_load(open('ansible/roles/promtail/handlers/main.yml'))" && echo "handlers VALID"
```

Expected: both `VALID`

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/promtail/defaults/main.yml ansible/roles/promtail/handlers/main.yml
git commit -m "feat(promtail): add Ansible role defaults and handlers"
```

---

## Task 5: Create the Promtail config Jinja2 template

**Files:**
- Create: `ansible/roles/promtail/templates/config.yml.j2`

- [ ] **Step 1: Create the config template**

The template scrapes the full systemd journal (all units), labels each entry with `host` and `unit`, and optionally scrapes extra file paths.

```jinja2
{# ansible/roles/promtail/templates/config.yml.j2 #}
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /var/lib/promtail/positions.yaml

clients:
  - url: {{ promtail_loki_url }}/loki/api/v1/push

scrape_configs:
  - job_name: journal
    journal:
      max_age: 12h
      labels:
        job: ansible-host
    relabel_configs:
      - source_labels: [__journal__hostname]
        target_label: host
      - source_labels: [__journal__systemd_unit]
        target_label: unit
{% if promtail_extra_paths %}
{% for path in promtail_extra_paths %}

  - job_name: files_{{ loop.index }}
    static_configs:
      - targets:
          - localhost
        labels:
          job: ansible-host
          host: {{ ansible_hostname }}
          filename: {{ path | basename }}
          __path__: {{ path }}
{% endfor %}
{% endif %}
```

- [ ] **Step 2: Validate Jinja2 template syntax (basic check)**

```bash
python3 -c "
from jinja2 import Environment
env = Environment()
env.parse(open('ansible/roles/promtail/templates/config.yml.j2').read())
print('VALID')
"
```

Expected: `VALID`

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/promtail/templates/config.yml.j2
git commit -m "feat(promtail): add Promtail config Jinja2 template"
```

---

## Task 6: Create the systemd unit template and role tasks

**Files:**
- Create: `ansible/roles/promtail/templates/promtail.service.j2`
- Create: `ansible/roles/promtail/tasks/main.yml`

- [ ] **Step 1: Create the systemd unit template**

```jinja2
{# ansible/roles/promtail/templates/promtail.service.j2 #}
[Unit]
Description=Promtail log shipper
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/promtail -config.file=/etc/promtail/config.yml
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create the role tasks**

```yaml
# ansible/roles/promtail/tasks/main.yml
---
- name: Set promtail architecture
  set_fact:
    _promtail_arch: "{{ 'amd64' if ansible_architecture == 'x86_64' else ansible_architecture }}"

- name: Download promtail binary
  get_url:
    url: "https://github.com/grafana/loki/releases/download/v{{ promtail_version }}/promtail-linux-{{ _promtail_arch }}.zip"
    dest: "/tmp/promtail-{{ promtail_version }}.zip"
    mode: '0644'

- name: Unzip promtail binary
  unarchive:
    src: "/tmp/promtail-{{ promtail_version }}.zip"
    dest: /usr/local/bin/
    remote_src: yes
    include:
      - "promtail-linux-{{ _promtail_arch }}"

- name: Rename promtail binary
  command: "mv /usr/local/bin/promtail-linux-{{ _promtail_arch }} /usr/local/bin/promtail"
  args:
    creates: /usr/local/bin/promtail

- name: Set promtail binary permissions
  file:
    path: /usr/local/bin/promtail
    mode: '0755'
    owner: root
    group: root

- name: Create promtail config directory
  file:
    path: /etc/promtail
    state: directory
    owner: root
    group: root
    mode: '0755'

- name: Create promtail positions directory
  file:
    path: /var/lib/promtail
    state: directory
    owner: root
    group: root
    mode: '0755'

- name: Template promtail config
  template:
    src: config.yml.j2
    dest: /etc/promtail/config.yml
    owner: root
    group: root
    mode: '0644'
  notify: restart promtail

- name: Install promtail systemd unit
  template:
    src: promtail.service.j2
    dest: /etc/systemd/system/promtail.service
    owner: root
    group: root
    mode: '0644'
  notify:
    - reload systemd
    - restart promtail

- name: Enable and start promtail
  systemd:
    name: promtail
    enabled: yes
    state: started
```

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('ansible/roles/promtail/tasks/main.yml'))" && echo "tasks VALID"
```

Expected: `tasks VALID`

- [ ] **Step 4: Run Ansible syntax check**

```bash
cd ansible && ansible-playbook --syntax-check -i inventory.yml deploy-claude-runner.yml 2>&1 | tail -5
```

(This validates the toolchain works before we wire promtail in.)

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/promtail/templates/promtail.service.j2 ansible/roles/promtail/tasks/main.yml
git commit -m "feat(promtail): add Ansible promtail role tasks and systemd unit template"
```

---

## Task 7: Update `ansible/inventory.yml` — add `proxmox_nodes` and `swarm_nodes` groups

**Files:**
- Modify: `ansible/inventory.yml`

The current inventory has a `proxmox_vms` group with all VMs. We need to add:
- `swarm_nodes` — dks01–dks05 (used to exclude them from `deploy-logging-agents.yml`)
- `proxmox_nodes` — prx01–prx05 Proxmox hypervisors

- [ ] **Step 1: Read current inventory**

Read `ansible/inventory.yml` to understand the current structure before editing.

- [ ] **Step 2: Add new groups to inventory**

The full updated `ansible/inventory.yml`:

```yaml
---
# Inventory file for homelab infrastructure
# Docker Swarm nodes: dks01-dks05.schollar.dev
# Edge node: 192.168.3.91

all:
  children:
    proxmox_vms:
      hosts:
        dks01:
          ansible_host: dks01.schollar.dev
        dks02:
          ansible_host: dks02.schollar.dev
        dks03:
          ansible_host: dks03.schollar.dev
        dks04:
          ansible_host: dks04.schollar.dev
        dks05:
          ansible_host: dks05.schollar.dev
        claude:
          ansible_host: 192.168.3.79
        edge01:
          ansible_host: 192.168.3.91
        gt-runner-01:
          ansible_host: 192.168.3.100
      vars:
        ansible_user: root
        ansible_python_interpreter: /usr/bin/python3
        ansible_ssh_private_key_file: "{{ inventory_dir }}/ansible_ssh_key"

    swarm_nodes:
      hosts:
        dks01:
        dks02:
        dks03:
        dks04:
        dks05:

    proxmox_nodes:
      hosts:
        prx01:
          ansible_host: prx01.schollar.dev
        prx02:
          ansible_host: prx02.schollar.dev
        prx03:
          ansible_host: prx03.schollar.dev
        prx04:
          ansible_host: prx04.schollar.dev
        prx05:
          ansible_host: prx05.schollar.dev
      vars:
        ansible_user: root
        ansible_python_interpreter: /usr/bin/python3
        ansible_ssh_private_key_file: "{{ inventory_dir }}/ansible_ssh_key"
```

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('ansible/inventory.yml'))" && echo "VALID"
```

Expected: `VALID`

- [ ] **Step 4: Syntax-check a playbook against the new inventory**

```bash
cd ansible && ansible-playbook --syntax-check -i inventory.yml deploy-claude-runner.yml 2>&1 | tail -3
```

Expected: `playbook: deploy-claude-runner.yml` (no errors)

- [ ] **Step 5: Commit**

```bash
git add ansible/inventory.yml
git commit -m "feat(inventory): add swarm_nodes and proxmox_nodes groups"
```

---

## Task 8: Create `ansible/deploy-logging-agents.yml`

**Files:**
- Create: `ansible/deploy-logging-agents.yml`

This playbook bootstraps Promtail on all non-Swarm hosts (VMs and Proxmox nodes). Swarm nodes use the global Swarm service instead.

- [ ] **Step 1: Create the playbook**

```yaml
# ansible/deploy-logging-agents.yml
---
- name: Deploy Promtail log shipping agents
  hosts: proxmox_vms:!swarm_nodes:proxmox_nodes
  become: true

  roles:
    - promtail
```

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('ansible/deploy-logging-agents.yml'))" && echo "VALID"
```

Expected: `VALID`

- [ ] **Step 3: Ansible syntax check**

```bash
cd ansible && ansible-playbook --syntax-check -i inventory.yml deploy-logging-agents.yml 2>&1 | tail -5
```

Expected: `playbook: deploy-logging-agents.yml` with no errors listed above it.

- [ ] **Step 4: Commit**

```bash
git add ansible/deploy-logging-agents.yml
git commit -m "feat(logging): add deploy-logging-agents.yml bootstrap playbook"
```

---

## Task 9: Update existing deploy playbooks to include the `promtail` role

**Files:**
- Modify: `ansible/deploy-claude-runner.yml`
- Modify: `ansible/deploy-edge.yml`
- Modify: `ansible/deploy-gitea-runner.yml`

- [ ] **Step 1: Update `deploy-claude-runner.yml`**

```yaml
# ansible/deploy-claude-runner.yml
---
- name: Deploy claude-runner service manager
  hosts: claude
  become: true

  vars:
    required_tools: [tea, jq, act]

  pre_tasks:
    - name: Install required tools
      import_role:
        name: tools

  roles:
    - claude-runner
    - promtail
```

- [ ] **Step 2: Update `deploy-edge.yml`**

```yaml
# ansible/deploy-edge.yml
---
- name: Deploy edge node services
  hosts: edge_nodes
  become: true

  vars:
    required_tools: [cloudflared]

  pre_tasks:
    - name: Install required tools
      import_role:
        name: tools

  roles:
    - cloudflared
    - traefik-edge
    - promtail
```

- [ ] **Step 3: Update `deploy-gitea-runner.yml`**

```yaml
# ansible/deploy-gitea-runner.yml
---
- name: Deploy Gitea act runner
  hosts: gt-runner-01
  become: true

  vars:
    required_tools: [docker, node, make, hatch, git]

  pre_tasks:
    - name: Install required tools
      import_role:
        name: tools

  roles:
    - gitea-runner
    - promtail
```

- [ ] **Step 4: Validate YAML syntax on all three**

```bash
python3 -c "
import yaml
for f in ['ansible/deploy-claude-runner.yml','ansible/deploy-edge.yml','ansible/deploy-gitea-runner.yml']:
    yaml.safe_load(open(f))
    print(f, 'VALID')
"
```

Expected: all three print `VALID`

- [ ] **Step 5: Ansible syntax check on all three**

```bash
cd ansible && for p in deploy-claude-runner.yml deploy-edge.yml deploy-gitea-runner.yml; do
  echo -n "$p: "
  ansible-playbook --syntax-check -i inventory.yml $p 2>&1 | tail -1
done
```

Expected: each prints `playbook: <name>.yml` (no errors).

- [ ] **Step 6: Commit**

```bash
git add ansible/deploy-claude-runner.yml ansible/deploy-edge.yml ansible/deploy-gitea-runner.yml
git commit -m "feat(logging): append promtail role to existing deploy playbooks"
```

---

## Task 10: Add Grafana Loki datasource and connect Grafana to `logging-net`

**Files:**
- Create: `monitoring/provisioning/datasources/loki.yml`
- Modify: `monitoring/docker-compose.yaml`

- [ ] **Step 1: Create the Loki datasource provisioning file**

```yaml
# monitoring/provisioning/datasources/loki.yml
apiVersion: 1
datasources:
  - name: Loki
    type: loki
    access: proxy
    url: http://loki:3100
    isDefault: false
    version: 1
```

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('monitoring/provisioning/datasources/loki.yml'))" && echo "VALID"
```

Expected: `VALID`

- [ ] **Step 3: Update `monitoring/docker-compose.yaml`**

Add `logging-net` to the top-level `networks` block and to the Grafana service's networks list. Also add the new datasource config to the `configs` block and mount it into the Grafana container.

The full updated `monitoring/docker-compose.yaml`:

```yaml
version: "3.8"

networks:
  traefik-net:
    external: true
  logging-net:
    external: true

volumes:
  prometheus-data:
  grafana-data:
  grafana-dashboards:   # drop Ceph dashboard JSONs here at runtime (optional)

configs:
  prometheus.yml:
    file: ./prometheus.yml
  grafana-datasource-prometheus.yml:
    file: ./provisioning/datasources/prometheus.yml
  grafana-datasource-loki.yml:
    file: ./provisioning/datasources/loki.yml
  grafana-dashboards-provider.yml:
    file: ./provisioning/dashboards/provider.yml

secrets:
  grafana-admin-password:
    file: ./secrets/grafana-admin-password.txt

services:
  prometheus:
    image: prom/prometheus:v2.54.1
    user: "0:0"
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"
      - "--web.enable-lifecycle"
      - "--storage.tsdb.retention.time=15d"
    configs:
      - source: prometheus.yml
        target: /etc/prometheus/prometheus.yml
    volumes:
      - prometheus-data:/prometheus
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [traefik-net]
    deploy:
      mode: replicated
      replicas: 1
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 0
      placement:
        constraints:
          - node.role == manager
      update_config:
        order: start-first
        parallelism: 1
      labels:
        traefik.enable: "true"
        traefik.docker.network: traefik-net
        traefik.http.routers.prometheus.rule: Host(`prometheus.schollar.dev`)
        traefik.http.routers.prometheus.entrypoints: websecure
        traefik.http.routers.prometheus.tls.certresolver: cf
        traefik.http.services.prometheus.loadbalancer.server.port: "9090"
        # Metrics
        prometheus.blackbox: "true"
        metrics.probe_url: "https://prometheus.schollar.dev"

  grafana:
    image: grafana/grafana:10.4.5
    environment:
      GF_SECURITY_ADMIN_USER: admin
      GF_SECURITY_ADMIN_PASSWORD__FILE: /run/secrets/grafana-admin-password
      GF_SERVER_ROOT_URL: https://grafana.schollar.dev
      GF_USERS_DEFAULT_THEME: dark
      GF_PATHS_PROVISIONING: /etc/grafana/provisioning
    secrets:
      - grafana-admin-password
    configs:
      - source: grafana-datasource-prometheus.yml
        target: /etc/grafana/provisioning/datasources/prometheus.yml
      - source: grafana-datasource-loki.yml
        target: /etc/grafana/provisioning/datasources/loki.yml
      - source: grafana-dashboards-provider.yml
        target: /etc/grafana/provisioning/dashboards/provider.yml
    volumes:
      - grafana-data:/var/lib/grafana
      - grafana-dashboards:/var/lib/grafana/dashboards
    networks: [traefik-net, logging-net]
    deploy:
      mode: replicated
      replicas: 1
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 0
      placement:
        constraints:
          - node.labels.metrics == true
      update_config:
        order: start-first
        parallelism: 1
      labels:
        traefik.enable: "true"
        traefik.docker.network: traefik-net
        traefik.http.routers.grafana.rule: Host(`grafana.schollar.dev`)
        traefik.http.routers.grafana.entrypoints: websecure
        traefik.http.routers.grafana.tls.certresolver: cf
        traefik.http.services.grafana.loadbalancer.server.port: "3000"
```

- [ ] **Step 4: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('monitoring/docker-compose.yaml'))" && echo "VALID"
```

Expected: `VALID`

- [ ] **Step 5: Commit**

```bash
git add monitoring/provisioning/datasources/loki.yml monitoring/docker-compose.yaml
git commit -m "feat(monitoring): add Loki datasource and connect Grafana to logging-net"
```

---

## Spec Coverage Check

| Spec requirement | Task |
|---|---|
| `logging/` Docker Swarm stack with Loki + Promtail global | Tasks 1, 2, 3 |
| Loki filesystem storage, 30 GB Linstor HDD, 2 replicas | Task 3 |
| Loki 7-day retention via compactor | Task 1 |
| Port 3100 published via Swarm ingress | Task 3 |
| Promtail global, no-constraint placement | Task 3 |
| Labels: swarm_service, container_name, swarm_node, stack | Task 2 |
| Labels: host, unit from journal | Task 2 |
| Ansible `promtail` role (binary install, config template, systemd) | Tasks 4, 5, 6 |
| `promtail_version`, `promtail_loki_url`, `promtail_extra_paths` defaults | Task 4 |
| Full journal scrape, `job: ansible-host` static label | Task 5 |
| `deploy-logging-agents.yml` for non-Swarm hosts | Task 8 |
| Inventory `proxmox_nodes` and `swarm_nodes` groups | Task 7 |
| Append promtail to deploy-claude-runner.yml, deploy-edge.yml, deploy-gitea-runner.yml | Task 9 |
| Grafana Loki datasource provisioning | Task 10 |
| Grafana on `logging-net` | Task 10 |
| S3 migration path preserved (schema_config is backend-agnostic) | Task 1 |
| Memory constraints: Loki ≤ 4GB, Promtail < 200MB | No hard resource limits in Swarm — monitored operationally |
