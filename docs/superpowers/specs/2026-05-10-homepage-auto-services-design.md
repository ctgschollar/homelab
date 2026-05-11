# Homepage Auto-Services Design

**Date:** 2026-05-10
**Status:** Approved

## Overview

Replace the manually-maintained `homepage/services.yaml` with a generator script that reads Traefik labels from every `docker-compose.yaml` in the repo and produces the services list automatically. Proxmox nodes (not Docker services) remain as a manually-maintained `HomeLab` section.

---

## Generator Script

**File:** `scripts/generate-homepage-services.py`

**Inputs:** All `*/docker-compose.yaml` files under the repo root.

**Logic:**
1. For each compose file, iterate over services.
2. Skip services without `traefik.enable: "true"` in their deploy labels.
3. Extract the hostname from the first `traefik.http.routers.*.rule` label matching `Host(...)`.
4. Derive a display name from the Docker service name: strip everything up to and including the first `_` (the Swarm stack prefix), then title-case the remainder (e.g. `jellyfin_jellyfin` → `Jellyfin`).
5. Look up the service name in a hardcoded icon map; omit the icon key if not found.
6. Sort entries alphabetically by name.
7. Write `homepage/services.yaml` with two top-level sections:
   - `HomeLab` — Proxmox entries, hardcoded in the script (not auto-generated).
   - `Services` — the generated list.

**Usage:** `python scripts/generate-homepage-services.py` from repo root. Re-run after adding or removing a Swarm service, then commit the updated `services.yaml`.

**Output format per entry:**
```yaml
- Services:
    - Jellyfin:
        href: https://jellyfin.schollar.dev
        icon: si-jellyfin
    - Gitea:
        href: https://gitea.schollar.dev
        icon: si-gitea
    # ...
```

Entries without a known icon omit the `icon` key entirely.

---

## Icon Map

A hardcoded dict in the script mapping lowercase service names to simple-icons slugs. Initial entries cover the known services. Services not in the map appear without an icon.

---

## `homepage/services.yaml`

After generation this file contains only the `HomeLab` section (Proxmox) and the generated `Services` section. The existing `Media` section is removed — Jellyfin, Sonarr, Radarr, and QBittorrent appear in `Services` instead.

---

## `homepage/settings.yaml`

Layout updated to two groups:

```yaml
layout:
  HomeLab:
    style: row
    columns: 3
  Services:
    style: row
    columns: 4
```

The `Media` and `Docker Services` layout entries are removed.

---

## What Does Not Change

- `homepage/docker.yaml` — unchanged (Docker socket / swarmModeEndpoint config stays for potential future use).
- All `docker-compose.yaml` files — no new labels added.
- Proxmox entries — remain hardcoded in the script and in `services.yaml`.

---

## Running the Generator

```bash
cd /home/claude/src/homelab
python scripts/generate-homepage-services.py
git add homepage/services.yaml
git commit -m "chore: regenerate homepage services"
```

No runtime dependency — homepage reads the static file, the script is only needed when the service list changes.
