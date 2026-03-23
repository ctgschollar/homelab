---
tags: [recovery, docker, monitoring]
incident: INC-0008
date: 2026-03-23T22:52:06.251397+00:00
---

# INC-0008: dks04-vm-restart-monitoring-recovery

**Inciting Incident**
Monitor alert indicated that monitoring_grafana service was down for 300s. Investigation revealed that Docker Swarm node dks04 had status "down" with "heartbeat failure", preventing critical monitoring services (Grafana, Prometheus, Alertmanager) from running as they have placement constraints requiring node.labels.metrics == true.

**Resolution**
Located dks04 VM (VMID 112) on Proxmox host prx05.schollar.dev and found it was stopped. Started the VM using `qm start 112`. The VM successfully rejoined the Docker Swarm cluster, and all monitoring services automatically rescheduled and recovered. Grafana recovery was confirmed by subsequent monitor alert showing service restored after 300s downtime.

**Tools Used**
`run_shell`, `docker_service_inspect`, `docker_service_list`, `slack_notify`

---
## Action Log

```json
[]
```