---
tags: [recovery, docker, storage]
incident: INC-0015
date: 2026-03-24T22:40:27.991532+00:00
---

# INC-0015: gitea-service-drbd-mount-failures

**Inciting Incident**
Monitor alert reported gitea_gitea service as degraded with 0/1 replicas due to DRBD volume mount failures on dks03 node. Alert indicated "Satellite 'dks03' does not support the following layers: [DRBD]" error when attempting to mount the gitea-data LINSTOR volume.

**Resolution**
Investigation revealed the service was actually healthy and running normally on dks01. The alert was related to failed deployment attempts on dks03 during initial service startup. Docker Swarm's scheduler had automatically moved the service to dks01 which has proper DRBD/LINSTOR support, and the service was functioning correctly. This is a known infrastructure limitation where dks03 lacks DRBD configuration for replicated LINSTOR volumes.

**Tools Used**
`docker_service_inspect`, `run_shell`, `read_logs`, `slack_notify`

---
## Action Log

```json
[]
```