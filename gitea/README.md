# Gitea

Self-hosted Git service at `gitea.schollar.dev`, backed by the shared Postgres instance.

## Prerequisites

- Postgres database and user created:
  ```sql
  CREATE USER gitea WITH PASSWORD '<password>';
  CREATE DATABASE gitea OWNER gitea;
  ```
- Secrets populated in `/mnt/cephfs-configs/gitea/.env` (see below).

---

## Two-step deploy

The act-runner requires a registration token that only exists after Gitea is
initialised. Deploy in two passes:

### Step 1 — Deploy Gitea only

1. **Fill in the `.env` secrets** at `/mnt/cephfs-configs/gitea/.env`:

   | Key | How to generate |
   |-----|----------------|
   | `GITEA__database__PASSWD` | Password you set for the `gitea` Postgres user |
   | `GITEA__security__SECRET_KEY` | `openssl rand -hex 32` |
   | `GITEA__security__INTERNAL_TOKEN` | `docker run --rm gitea/gitea:latest gitea generate secret INTERNAL_TOKEN` |

   Leave `ACT_RUNNER_TOKEN` blank for now.

2. **Deploy the stack** (act-runner is commented out):
   ```sh
   docker stack deploy -c docker-compose.yaml gitea --with-registry-auth
   ```

3. **Complete the setup wizard** at `https://gitea.schollar.dev`.
   The database details are pre-configured via environment variables; you only
   need to set the admin account.

### Step 2 — Enable the act-runner

1. In Gitea: **Site Administration → Runners → Create new runner** — copy the
   registration token.

2. Add it to `/mnt/cephfs-configs/gitea/.env`:
   ```
   ACT_RUNNER_TOKEN=<paste token here>
   ```

3. Uncomment the `act-runner` service in `docker-compose.yaml` (remove the `#`
   from every line of the `act-runner` block).

4. Redeploy:
   ```sh
   docker stack deploy -c docker-compose.yaml gitea --with-registry-auth
   ```

---

## Stack layout

| Service | Image | Port |
|---------|-------|------|
| `gitea` | `gitea/gitea:latest` | 3000 (HTTP, internal) |
| `act-runner` | `gitea/act_runner:latest` | — (no exposed port) |

**Volume:** `gitea-data` → `/data` — LINSTOR `pool_ssd`, 20 GiB, 2 replicas.

**Network:** `traefik-net` (external overlay). Postgres is reachable at
`pg.schollar.dev:5432` on the same network.

**TLS:** Cloudflare cert resolver (`cf`), entrypoint `websecure`.
