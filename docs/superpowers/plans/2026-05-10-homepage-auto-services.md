# Homepage Auto-Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python generator script that reads Traefik labels from every `docker-compose.yaml` and writes `homepage/services.yaml` automatically.

**Architecture:** A standalone Python script under `scripts/` reads all `*/docker-compose.yaml` files in the repo root, extracts HTTP-Traefik-enabled services and their hostnames, and writes a complete `homepage/services.yaml` with a hardcoded HomeLab/Proxmox section plus a generated Services section. Re-run the script whenever a service is added or removed, then commit.

**Tech Stack:** Python 3.12, PyYAML, pytest, hatch

> **Note:** The spec names the script `generate-homepage-services.py` but this plan uses `generate_homepage_services.py` (underscores) so it can be imported by pytest without a workaround.

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Create | `scripts/pyproject.toml` | hatch project — pytest + pyyaml deps |
| Create | `scripts/tests/__init__.py` | marks tests as a package |
| Create | `scripts/tests/test_generate.py` | all unit tests |
| Create | `scripts/generate_homepage_services.py` | generator script |
| Modify | `homepage/services.yaml` | replaced by script output |
| Modify | `homepage/settings.yaml` | update layout groups |

---

## Task 1: Bootstrap the scripts directory

**Files:**
- Create: `scripts/pyproject.toml`
- Create: `scripts/tests/__init__.py`
- Create: `scripts/generate_homepage_services.py` (empty skeleton)
- Create: `scripts/tests/test_generate.py` (empty)

- [ ] **Step 1: Create `scripts/pyproject.toml`**

```toml
[project]
name = "homelab-scripts"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["pyyaml"]

[tool.hatch.envs.default]
type = "virtual"
python = "3.12"
path = ".venv"
skip-install = true
dependencies = ["pyyaml"]

[tool.hatch.envs.test]
type = "virtual"
python = "3.12"
path = ".venv-test"
skip-install = true
dependencies = ["pytest>=8.0", "pyyaml"]

[tool.hatch.envs.test.scripts]
run = "pytest {args}"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 2: Create empty placeholder files**

```bash
touch scripts/tests/__init__.py
touch scripts/tests/test_generate.py
```

Create `scripts/generate_homepage_services.py` with just the imports:

```python
from __future__ import annotations
import re
from pathlib import Path
import yaml
```

- [ ] **Step 3: Verify hatch picks up the project**

```bash
cd scripts && hatch run test:run
```

Expected: `no tests ran` with exit 0.

- [ ] **Step 4: Commit**

```bash
git add scripts/
git commit -m "chore: scaffold scripts directory with hatch + pytest"
```

---

## Task 2: Implement `get_labels()`

Normalises Docker Compose label blocks — services use either a YAML dict or a list of `key=value` / `key: value` strings. All labels in this repo live under `deploy.labels`.

**Files:**
- Modify: `scripts/tests/test_generate.py`
- Modify: `scripts/generate_homepage_services.py`

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_generate.py`:

```python
from generate_homepage_services import get_labels


def test_get_labels_dict_format():
    svc = {
        "deploy": {
            "labels": {
                "traefik.enable": "true",
                "traefik.http.routers.foo.rule": "Host(`foo.example.com`)",
            }
        }
    }
    result = get_labels(svc)
    assert result["traefik.enable"] == "true"
    assert result["traefik.http.routers.foo.rule"] == "Host(`foo.example.com`)"


def test_get_labels_list_equals_format():
    svc = {
        "deploy": {
            "labels": [
                "traefik.enable=true",
                "traefik.http.routers.foo.rule=Host(`foo.example.com`)",
            ]
        }
    }
    result = get_labels(svc)
    assert result["traefik.enable"] == "true"
    assert result["traefik.http.routers.foo.rule"] == "Host(`foo.example.com`)"


def test_get_labels_list_quoted_value():
    # portainer uses: - "traefik.http.routers.portainer.rule=Host(`portainer.schollar.dev`)"
    svc = {
        "deploy": {
            "labels": ['"traefik.enable=true"']
        }
    }
    result = get_labels(svc)
    assert result["traefik.enable"] == "true"


def test_get_labels_no_deploy():
    assert get_labels({}) == {}
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd scripts && hatch run test:run tests/test_generate.py -v
```

Expected: `ImportError: cannot import name 'get_labels'`

- [ ] **Step 3: Implement `get_labels()`**

Add to `scripts/generate_homepage_services.py`:

```python
def get_labels(service_def: dict) -> dict:
    """Normalise deploy.labels from dict or list format to a flat str→str dict."""
    raw = service_def.get("deploy", {}).get("labels", {})
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items()}
    result = {}
    for item in raw:
        item = str(item).strip().strip('"')
        if "=" in item:
            k, _, v = item.partition("=")
        else:
            k, _, v = item.partition(": ")
        result[k.strip()] = v.strip().strip('"')
    return result
```

- [ ] **Step 4: Run to confirm passing**

```bash
cd scripts && hatch run test:run tests/test_generate.py -v
```

Expected: 4 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_homepage_services.py scripts/tests/test_generate.py
git commit -m "feat: implement get_labels() with dict and list format support"
```

---

## Task 3: Implement `parse_host_rule()`

Extracts the hostname from a Traefik `Host(...)` rule. Rules come in three forms: bare backticks, double-quoted, and list-format with `=`. Env-var hostnames (`${...}`) and TCP `HostSNI` rules must be skipped.

**Files:**
- Modify: `scripts/tests/test_generate.py`
- Modify: `scripts/generate_homepage_services.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_generate.py`:

```python
from generate_homepage_services import parse_host_rule


def test_parse_host_rule_bare_backticks():
    assert parse_host_rule("Host(`foo.example.com`)") == "foo.example.com"


def test_parse_host_rule_double_quoted_value():
    # value stored as: "Host(`gitea.schollar.dev`)"
    assert parse_host_rule('"Host(`gitea.schollar.dev`)"') == "gitea.schollar.dev"


def test_parse_host_rule_env_var_returns_none():
    assert parse_host_rule("Host(`${DOMAIN?Variable not set}`)") is None


def test_parse_host_rule_tcp_hostsni_returns_none():
    assert parse_host_rule("HostSNI(`*`)") is None


def test_parse_host_rule_no_host_returns_none():
    assert parse_host_rule("PathPrefix(`/api`)") is None
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd scripts && hatch run test:run tests/test_generate.py::test_parse_host_rule_bare_backticks -v
```

Expected: `ImportError: cannot import name 'parse_host_rule'`

- [ ] **Step 3: Implement `parse_host_rule()`**

Add to `scripts/generate_homepage_services.py`:

```python
def parse_host_rule(rule: str) -> str | None:
    """Extract hostname from a Traefik Host() rule. Returns None for env vars or no match."""
    m = re.search(r"Host\(`([^`]+)`\)", rule)
    if not m:
        return None
    hostname = m.group(1)
    return None if "$" in hostname else hostname
```

- [ ] **Step 4: Run to confirm passing**

```bash
cd scripts && hatch run test:run tests/test_generate.py -v
```

Expected: all 9 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_homepage_services.py scripts/tests/test_generate.py
git commit -m "feat: implement parse_host_rule()"
```

---

## Task 4: Implement `make_display_name()`, `get_icon()`, and `ICON_MAP`

Display names come from the subdomain of the hostname. The icon map covers every known subdomain in the repo.

**Files:**
- Modify: `scripts/tests/test_generate.py`
- Modify: `scripts/generate_homepage_services.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_generate.py`:

```python
from generate_homepage_services import make_display_name, get_icon


def test_make_display_name_simple():
    assert make_display_name("jellyfin.schollar.dev") == "Jellyfin"


def test_make_display_name_hyphenated():
    assert make_display_name("excalidraw-room.schollar.dev") == "Excalidraw Room"


def test_make_display_name_multipart_tld():
    assert make_display_name("qbittorrent.schollar.dev") == "Qbittorrent"


def test_get_icon_known_service():
    assert get_icon("jellyfin.schollar.dev") == "si-jellyfin"


def test_get_icon_unknown_service():
    assert get_icon("unknown.schollar.dev") is None


def test_get_icon_case_insensitive():
    assert get_icon("Jellyfin.schollar.dev") == "si-jellyfin"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd scripts && hatch run test:run tests/test_generate.py::test_make_display_name_simple -v
```

Expected: `ImportError: cannot import name 'make_display_name'`

- [ ] **Step 3: Implement `ICON_MAP`, `make_display_name()`, and `get_icon()`**

Add to `scripts/generate_homepage_services.py` (before any function definitions):

```python
ICON_MAP: dict[str, str | None] = {
    "alerts": None,
    "audiobooks": "si-audiobookshelf",
    "books": "si-calibreweb",
    "chat": None,
    "clip": None,
    "excalidraw": "si-excalidraw",
    "excalidraw-room": None,
    "excalidraw-storage": None,
    "flaresolverr": None,
    "gitea": "si-gitea",
    "grafana": "si-grafana",
    "hdoc": "si-hedgedoc",
    "homepage": "si-homepage",
    "immich": "si-immich",
    "jellyfin": "si-jellyfin",
    "jellyseerr": "si-jellyseerr",
    "joplin": "si-joplin",
    "lazylibarian": None,
    "litellm": None,
    "obsidian": "si-obsidian",
    "portainer": "si-portainer",
    "prometheus": "si-prometheus",
    "prowlarr": "si-prowlarr",
    "qbittorrent": "si-qbittorrent",
    "radarr": "si-radarr",
    "registry": "si-docker",
    "sonarr": "si-sonarr",
    "traefik": "si-traefikproxy",
}
```

Then add the functions:

```python
def make_display_name(hostname: str) -> str:
    """'excalidraw-room.schollar.dev' -> 'Excalidraw Room'"""
    subdomain = hostname.split(".")[0]
    return subdomain.replace("-", " ").title()


def get_icon(hostname: str) -> str | None:
    subdomain = hostname.split(".")[0].lower()
    return ICON_MAP.get(subdomain)
```

- [ ] **Step 4: Run to confirm passing**

```bash
cd scripts && hatch run test:run tests/test_generate.py -v
```

Expected: all 15 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_homepage_services.py scripts/tests/test_generate.py
git commit -m "feat: add ICON_MAP, make_display_name(), get_icon()"
```

---

## Task 5: Implement `get_traefik_services()`

Reads a single compose file and returns a list of `{hostname, display_name, icon}` dicts for every HTTP-Traefik-enabled service. Services with TCP routes, disabled Traefik, or env-var hostnames are skipped.

**Files:**
- Modify: `scripts/tests/test_generate.py`
- Modify: `scripts/generate_homepage_services.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_generate.py`:

```python
import textwrap
from generate_homepage_services import get_traefik_services

DICT_LABELS_COMPOSE = textwrap.dedent("""\
    services:
      jellyfin:
        image: jellyfin/jellyfin:latest
        deploy:
          labels:
            traefik.enable: "true"
            traefik.http.routers.jellyfin.rule: Host(`jellyfin.example.com`)
""")

LIST_LABELS_COMPOSE = textwrap.dedent("""\
    services:
      clipcascade:
        image: clipcascade:latest
        deploy:
          labels:
            - traefik.enable=true
            - traefik.http.routers.clip.rule=Host(`clip.example.com`)
""")

DISABLED_COMPOSE = textwrap.dedent("""\
    services:
      metrics:
        image: metrics:latest
        deploy:
          labels:
            traefik.enable: "false"
            traefik.http.routers.metrics.rule: Host(`metrics.example.com`)
""")

ENV_VAR_COMPOSE = textwrap.dedent("""\
    services:
      red-ui:
        image: red-ui:latest
        deploy:
          labels:
            - traefik.enable=true
            - traefik.http.routers.red-ui.rule=Host(`${DOMAIN?Variable not set}`)
""")

TCP_COMPOSE = textwrap.dedent("""\
    services:
      postgres:
        image: postgres:latest
        deploy:
          labels:
            traefik.enable: "true"
            traefik.tcp.routers.postgres.rule: HostSNI(`*`)
""")

MULTI_SERVICE_COMPOSE = textwrap.dedent("""\
    services:
      prometheus:
        image: prom/prometheus:latest
        deploy:
          labels:
            traefik.enable: "true"
            traefik.http.routers.prometheus.rule: Host(`prometheus.example.com`)
      grafana:
        image: grafana/grafana:latest
        deploy:
          labels:
            traefik.enable: "true"
            traefik.http.routers.grafana.rule: Host(`grafana.example.com`)
""")


def test_get_traefik_services_dict_labels(tmp_path):
    f = tmp_path / "docker-compose.yaml"
    f.write_text(DICT_LABELS_COMPOSE)
    result = get_traefik_services(f)
    assert len(result) == 1
    assert result[0]["hostname"] == "jellyfin.example.com"
    assert result[0]["display_name"] == "Jellyfin"
    assert result[0]["icon"] == "si-jellyfin"


def test_get_traefik_services_list_labels(tmp_path):
    f = tmp_path / "docker-compose.yaml"
    f.write_text(LIST_LABELS_COMPOSE)
    result = get_traefik_services(f)
    assert len(result) == 1
    assert result[0]["hostname"] == "clip.example.com"


def test_get_traefik_services_skips_disabled(tmp_path):
    f = tmp_path / "docker-compose.yaml"
    f.write_text(DISABLED_COMPOSE)
    assert get_traefik_services(f) == []


def test_get_traefik_services_skips_env_var_hostname(tmp_path):
    f = tmp_path / "docker-compose.yaml"
    f.write_text(ENV_VAR_COMPOSE)
    assert get_traefik_services(f) == []


def test_get_traefik_services_skips_tcp_only_routes(tmp_path):
    f = tmp_path / "docker-compose.yaml"
    f.write_text(TCP_COMPOSE)
    assert get_traefik_services(f) == []


def test_get_traefik_services_multi_service(tmp_path):
    f = tmp_path / "docker-compose.yaml"
    f.write_text(MULTI_SERVICE_COMPOSE)
    result = get_traefik_services(f)
    assert len(result) == 2
    hostnames = {r["hostname"] for r in result}
    assert hostnames == {"prometheus.example.com", "grafana.example.com"}
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd scripts && hatch run test:run tests/test_generate.py::test_get_traefik_services_dict_labels -v
```

Expected: `ImportError: cannot import name 'get_traefik_services'`

- [ ] **Step 3: Implement `get_traefik_services()`**

Add to `scripts/generate_homepage_services.py`:

```python
def get_traefik_services(compose_path: Path) -> list[dict]:
    """Return [{hostname, display_name, icon}] for each HTTP-Traefik-enabled service."""
    data = yaml.safe_load(compose_path.read_text())
    if not data or "services" not in data:
        return []

    results = []
    for _name, svc_def in data["services"].items():
        if not isinstance(svc_def, dict):
            continue
        labels = get_labels(svc_def)
        if labels.get("traefik.enable", "").lower() != "true":
            continue
        hostname = None
        for key, value in labels.items():
            if re.match(r"traefik\.http\.routers\.[^.]+\.rule$", key):
                hostname = parse_host_rule(value)
                if hostname:
                    break
        if not hostname:
            continue
        results.append({
            "hostname": hostname,
            "display_name": make_display_name(hostname),
            "icon": get_icon(hostname),
        })
    return results
```

- [ ] **Step 4: Run to confirm passing**

```bash
cd scripts && hatch run test:run tests/test_generate.py -v
```

Expected: all 21 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_homepage_services.py scripts/tests/test_generate.py
git commit -m "feat: implement get_traefik_services()"
```

---

## Task 6: Implement `generate_yaml()` and `main()`

`generate_yaml()` builds the complete `services.yaml` string. `main()` wires everything together: finds all compose files, deduplicates by hostname, sorts, and writes the output file.

**Files:**
- Modify: `scripts/tests/test_generate.py`
- Modify: `scripts/generate_homepage_services.py`

- [ ] **Step 1: Write the failing tests for `generate_yaml()`**

Append to `scripts/tests/test_generate.py`:

```python
from generate_homepage_services import generate_yaml


def test_generate_yaml_includes_service_with_icon():
    services = [{"hostname": "jellyfin.schollar.dev", "display_name": "Jellyfin", "icon": "si-jellyfin"}]
    output = generate_yaml(services)
    assert "    - Jellyfin:" in output
    assert "        href: https://jellyfin.schollar.dev" in output
    assert "        icon: si-jellyfin" in output


def test_generate_yaml_omits_icon_when_none():
    services = [{"hostname": "lazylibarian.schollar.dev", "display_name": "Lazylibarian", "icon": None}]
    output = generate_yaml(services)
    assert "    - Lazylibarian:" in output
    assert "icon:" not in output


def test_generate_yaml_includes_proxmox():
    output = generate_yaml([])
    assert "- HomeLab:" in output
    assert "prx01:" in output
    assert "si-proxmox" in output


def test_generate_yaml_includes_services_section():
    output = generate_yaml([])
    assert "- Services:" in output
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd scripts && hatch run test:run tests/test_generate.py::test_generate_yaml_includes_service_with_icon -v
```

Expected: `ImportError: cannot import name 'generate_yaml'`

- [ ] **Step 3: Implement `PROXMOX_YAML`, `generate_yaml()`, `find_compose_files()`, and `main()`**

Add to `scripts/generate_homepage_services.py`:

```python
PROXMOX_YAML = """\
- HomeLab:
    - Proxmox:
        - prx01:
            href: https://prx01.schollar.dev:8006
            icon: si-proxmox
        - prx02:
            href: https://prx02.schollar.dev:8006
            icon: si-proxmox
        - prx03:
            href: https://prx03.schollar.dev:8006
            icon: si-proxmox
        - prx04:
            href: https://prx04.schollar.dev:8006
            icon: si-proxmox
        - prx05:
            href: https://prx05.schollar.dev:8006
            icon: si-proxmox
"""


def generate_yaml(services: list[dict]) -> str:
    lines: list[str] = [PROXMOX_YAML.rstrip(), "", "- Services:"]
    for svc in services:
        lines.append(f"    - {svc['display_name']}:")
        lines.append(f"        href: https://{svc['hostname']}")
        if svc["icon"]:
            lines.append(f"        icon: {svc['icon']}")
    lines.append("")
    return "\n".join(lines)


def find_compose_files(repo_root: Path) -> list[Path]:
    return sorted(repo_root.glob("*/docker-compose.yaml"))


def main() -> None:
    repo_root = Path(__file__).parent.parent
    seen: set[str] = set()
    all_services: list[dict] = []

    for path in find_compose_files(repo_root):
        for svc in get_traefik_services(path):
            if svc["hostname"] not in seen:
                seen.add(svc["hostname"])
                all_services.append(svc)

    all_services.sort(key=lambda s: s["display_name"].lower())

    out_path = repo_root / "homepage" / "services.yaml"
    out_path.write_text(generate_yaml(all_services))
    print(f"Written {len(all_services)} services to {out_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to confirm all tests pass**

```bash
cd scripts && hatch run test:run tests/test_generate.py -v
```

Expected: all 25 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_homepage_services.py scripts/tests/test_generate.py
git commit -m "feat: implement generate_yaml() and main()"
```

---

## Task 7: Run the generator and update `homepage/settings.yaml`

**Files:**
- Modify: `homepage/services.yaml` (generated)
- Modify: `homepage/settings.yaml`

- [ ] **Step 1: Run the generator from the repo root**

```bash
cd /home/claude/src/homelab
python scripts/generate_homepage_services.py
```

Expected output: `Written N services to .../homepage/services.yaml`

- [ ] **Step 2: Verify the output looks correct**

```bash
cat homepage/services.yaml
```

Expected: a `HomeLab` section with prx01–prx05, followed by a `Services` section with all Traefik-accessible services sorted alphabetically.

- [ ] **Step 3: Update `homepage/settings.yaml`**

Replace the full content of `homepage/settings.yaml` with:

```yaml
title: Services
theme: dark
layout:
  HomeLab:
    style: row
    columns: 3
  Services:
    style: row
    columns: 4

providers:
  docker:
    socket: /var/run/docker.sock
    # For Docker Swarm
    swarmModeEndpoint: true
```

- [ ] **Step 4: Commit everything**

```bash
git add homepage/services.yaml homepage/settings.yaml
git commit -m "feat: auto-generate homepage services from Traefik labels"
```

---

## Done

The generator is now in place. When a new Swarm service is added or removed:

```bash
python scripts/generate_homepage_services.py
git add homepage/services.yaml
git commit -m "chore: regenerate homepage services"
```
