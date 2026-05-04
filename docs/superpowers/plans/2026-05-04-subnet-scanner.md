# Subnet Scanner & Inventory Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python script that scans a LAN subnet, interactively assigns hostnames and groups to new hosts, and writes a fresh Ansible inventory + CoreDNS zone file.

**Architecture:** Five focused modules (`scanner`, `inventory`, `zonefile`, `corefile`) plus a thin CLI entry point (`scan_subnet.py`). Each module is independently testable. The CLI orchestrates them: scan → diff against known IPs → interactive prompts → confirm → atomic writes.

**Tech Stack:** Python 3.12, `python-nmap` (nmap wrapper), `questionary` (interactive prompts), `rich` (output), `pyyaml` (inventory YAML), `hatch` (env management), `pytest` (tests).

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `ansible/scripts/scanner.py` | nmap scan → `list[Host]` |
| Create | `ansible/scripts/inventory.py` | Load/write `inventory.yml`, known-IP set |
| Create | `ansible/scripts/zonefile.py` | Generate/append DNS zone file |
| Create | `ansible/scripts/corefile.py` | Remove stanza from CoreDNS Corefile |
| Create | `ansible/scripts/scan_subnet.py` | CLI entry point, interactive loop |
| Modify | `ansible/pyproject.toml` | Add script deps + default hatch env |
| Modify | `ansible/tests/conftest.py` | Add `scripts/` to sys.path |
| Create | `ansible/tests/test_scanner.py` | Unit tests for scanner |
| Create | `ansible/tests/test_inventory.py` | Unit tests for inventory |
| Create | `ansible/tests/test_zonefile.py` | Unit tests for zonefile |
| Create | `ansible/tests/test_corefile.py` | Unit tests for corefile |

---

## Task 1: Update dependencies and project structure

**Files:**
- Modify: `ansible/pyproject.toml`
- Modify: `ansible/tests/conftest.py`

- [ ] **Step 1: Update pyproject.toml**

Replace the contents of `ansible/pyproject.toml` with:

```toml
[project]
name = "homelab-ansible"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "questionary",
    "rich",
    "python-nmap",
    "pyyaml",
]

[tool.hatch.envs.default]
type = "virtual"
python = "3.12"
path = ".venv"

[tool.hatch.envs.test]
type = "virtual"
python = "3.12"
path = ".venv-test"
dependencies = [
    "pytest>=8.0",
    "ansible-core>=2.15",
    "questionary",
    "rich",
    "python-nmap",
    "pyyaml",
]

[tool.hatch.envs.test.scripts]
run = "pytest {args}"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Update conftest.py to expose scripts/ to tests**

Replace `ansible/tests/conftest.py` with:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "filter_plugins"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
```

- [ ] **Step 3: Create the scripts directory**

```bash
mkdir -p ansible/scripts
```

- [ ] **Step 4: Install deps**

```bash
cd ansible && hatch env create test
```

Expected: resolves and installs questionary, rich, python-nmap, pyyaml, pytest, ansible-core into `.venv-test`.

- [ ] **Step 5: Commit**

```bash
git add ansible/pyproject.toml ansible/tests/conftest.py
git commit -m "chore: add subnet scanner dependencies and scripts dir"
```

---

## Task 2: Scanner module

**Files:**
- Create: `ansible/scripts/scanner.py`
- Create: `ansible/tests/test_scanner.py`

- [ ] **Step 1: Write the failing tests**

Create `ansible/tests/test_scanner.py`:

```python
from unittest.mock import MagicMock, patch
import pytest
from scanner import scan_subnet, Host


def make_nm_mock(hosts_data):
    """Build a mock PortScanner instance from a dict keyed by IP."""
    nm = MagicMock()
    nm.all_hosts.return_value = list(hosts_data.keys())
    nm.__getitem__ = MagicMock(side_effect=lambda ip: hosts_data[ip])
    return nm


class TestScanSubnet:
    def test_returns_hosts_sorted_by_ip(self):
        mock_data = {
            "192.168.3.10": {"addresses": {"ipv4": "192.168.3.10"}, "hostnames": []},
            "192.168.3.2": {"addresses": {"ipv4": "192.168.3.2"}, "hostnames": []},
        }
        with patch("scanner.nmap.PortScanner") as cls:
            cls.return_value = make_nm_mock(mock_data)
            result = scan_subnet("192.168.3.0/24")
        assert result[0].ip == "192.168.3.2"
        assert result[1].ip == "192.168.3.10"

    def test_extracts_mac_address(self):
        mock_data = {
            "192.168.3.1": {
                "addresses": {"ipv4": "192.168.3.1", "mac": "AA:BB:CC:DD:EE:FF"},
                "hostnames": [],
            }
        }
        with patch("scanner.nmap.PortScanner") as cls:
            cls.return_value = make_nm_mock(mock_data)
            result = scan_subnet("192.168.3.0/24")
        assert result[0].mac == "AA:BB:CC:DD:EE:FF"

    def test_mac_none_when_not_present(self):
        mock_data = {
            "192.168.3.1": {"addresses": {"ipv4": "192.168.3.1"}, "hostnames": []}
        }
        with patch("scanner.nmap.PortScanner") as cls:
            cls.return_value = make_nm_mock(mock_data)
            result = scan_subnet("192.168.3.0/24")
        assert result[0].mac is None

    def test_extracts_nmap_hostname(self):
        mock_data = {
            "192.168.3.1": {
                "addresses": {"ipv4": "192.168.3.1"},
                "hostnames": [{"name": "router.local", "type": "PTR"}],
            }
        }
        with patch("scanner.nmap.PortScanner") as cls:
            cls.return_value = make_nm_mock(mock_data)
            result = scan_subnet("192.168.3.0/24")
        assert result[0].nmap_hostname == "router.local"

    def test_nmap_hostname_none_when_empty(self):
        mock_data = {
            "192.168.3.1": {
                "addresses": {"ipv4": "192.168.3.1"},
                "hostnames": [{"name": "", "type": "PTR"}],
            }
        }
        with patch("scanner.nmap.PortScanner") as cls:
            cls.return_value = make_nm_mock(mock_data)
            result = scan_subnet("192.168.3.0/24")
        assert result[0].nmap_hostname is None

    def test_empty_subnet_returns_empty_list(self):
        with patch("scanner.nmap.PortScanner") as cls:
            nm = MagicMock()
            nm.all_hosts.return_value = []
            cls.return_value = nm
            result = scan_subnet("192.168.3.0/24")
        assert result == []
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd ansible && hatch run test:pytest tests/test_scanner.py -v
```

Expected: `ImportError` — `scanner` module not found.

- [ ] **Step 3: Implement scanner.py**

Create `ansible/scripts/scanner.py`:

```python
from dataclasses import dataclass
import nmap


@dataclass
class Host:
    ip: str
    mac: str | None
    nmap_hostname: str | None


def scan_subnet(subnet: str) -> list[Host]:
    nm = nmap.PortScanner()
    nm.scan(hosts=subnet, arguments="-sn")
    hosts = []
    for ip in nm.all_hosts():
        addresses = nm[ip].get("addresses", {})
        mac = addresses.get("mac")
        hostnames = nm[ip].get("hostnames", [])
        nmap_hostname = next(
            (h["name"] for h in hostnames if h.get("name")), None
        )
        hosts.append(Host(ip=ip, mac=mac, nmap_hostname=nmap_hostname))
    return sorted(hosts, key=lambda h: tuple(int(x) for x in h.ip.split(".")))
```

- [ ] **Step 4: Run tests and confirm they pass**

```bash
cd ansible && hatch run test:pytest tests/test_scanner.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add ansible/scripts/scanner.py ansible/tests/test_scanner.py
git commit -m "feat: add scanner module with nmap host discovery"
```

---

## Task 3: Inventory module

**Files:**
- Create: `ansible/scripts/inventory.py`
- Create: `ansible/tests/test_inventory.py`

- [ ] **Step 1: Write the failing tests**

Create `ansible/tests/test_inventory.py`:

```python
import pytest
from inventory import (
    load_inventory,
    known_ips,
    add_host,
    write_inventory,
    Inventory,
    Group,
    HostEntry,
    DEFAULT_VARS,
)

SAMPLE_INVENTORY = """\
all:
  children:
    proxmox_vms:
      hosts:
        dks01:
          ansible_host: 192.168.3.70
          ansible_ip: 192.168.3.70
          mac_address: aa:bb:cc:dd:ee:ff
        claude:
          ansible_host: 192.168.3.79
          ansible_ip: 192.168.3.79
      vars:
        ansible_user: root
        ansible_python_interpreter: /usr/bin/python3
        ansible_ssh_private_key_file: "{{ inventory_dir }}/ansible_ssh_key"
"""


class TestLoadInventory:
    def test_returns_empty_when_file_missing(self, tmp_path):
        inv = load_inventory(tmp_path / "nonexistent.yml")
        assert inv.groups == {}

    def test_loads_groups(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert "proxmox_vms" in inv.groups

    def test_loads_hosts(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert "dks01" in inv.groups["proxmox_vms"].hosts
        assert inv.groups["proxmox_vms"].hosts["dks01"].ansible_host == "192.168.3.70"

    def test_loads_ansible_ip(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert inv.groups["proxmox_vms"].hosts["dks01"].ansible_ip == "192.168.3.70"

    def test_loads_mac_address(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert inv.groups["proxmox_vms"].hosts["dks01"].mac_address == "aa:bb:cc:dd:ee:ff"

    def test_loads_group_vars(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert inv.groups["proxmox_vms"].vars["ansible_user"] == "root"

    def test_host_without_mac_loads_none(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert inv.groups["proxmox_vms"].hosts["claude"].mac_address is None


class TestKnownIps:
    def test_returns_ansible_ip_values(self):
        inv = Inventory(
            groups={
                "g": Group(
                    hosts={
                        "h1": HostEntry(ansible_host="h1.example.com", ansible_ip="192.168.3.1"),
                        "h2": HostEntry(ansible_host="h2.example.com", ansible_ip="192.168.3.2"),
                    }
                )
            }
        )
        ips = known_ips(inv)
        assert "192.168.3.1" in ips
        assert "192.168.3.2" in ips

    def test_returns_raw_ip_ansible_host(self):
        inv = Inventory(
            groups={
                "g": Group(
                    hosts={"h": HostEntry(ansible_host="192.168.3.5")}
                )
            }
        )
        ips = known_ips(inv)
        assert "192.168.3.5" in ips

    def test_empty_inventory_returns_empty_set(self):
        assert known_ips(Inventory()) == set()


class TestAddHost:
    def test_adds_to_existing_group(self):
        inv = Inventory(groups={"mygroup": Group(hosts={}, vars={})})
        add_host(inv, "myhost", "192.168.3.5", "AA:BB:CC:DD:EE:FF", "mygroup", "example.com")
        assert "myhost" in inv.groups["mygroup"].hosts
        h = inv.groups["mygroup"].hosts["myhost"]
        assert h.ansible_host == "myhost.example.com"
        assert h.ansible_ip == "192.168.3.5"
        assert h.mac_address == "AA:BB:CC:DD:EE:FF"

    def test_creates_new_group_with_default_vars(self):
        inv = Inventory()
        add_host(inv, "myhost", "192.168.3.5", None, "newgroup", "example.com")
        assert "newgroup" in inv.groups
        assert inv.groups["newgroup"].vars == DEFAULT_VARS

    def test_mac_none_stored_as_none(self):
        inv = Inventory(groups={"g": Group(hosts={}, vars={})})
        add_host(inv, "h", "192.168.3.5", None, "g", "x.com")
        assert inv.groups["g"].hosts["h"].mac_address is None


class TestWriteInventory:
    def test_roundtrip_preserves_hosts(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        write_inventory(inv, f)
        inv2 = load_inventory(f)
        assert "dks01" in inv2.groups["proxmox_vms"].hosts
        assert inv2.groups["proxmox_vms"].hosts["dks01"].ansible_host == "192.168.3.70"
        assert inv2.groups["proxmox_vms"].hosts["dks01"].mac_address == "aa:bb:cc:dd:ee:ff"

    def test_roundtrip_preserves_group_vars(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        write_inventory(inv, f)
        inv2 = load_inventory(f)
        assert inv2.groups["proxmox_vms"].vars["ansible_user"] == "root"

    def test_no_tmp_file_left_behind(self, tmp_path):
        f = tmp_path / "inventory.yml"
        inv = Inventory(
            groups={"g": Group(hosts={"h": HostEntry(ansible_host="1.2.3.4", ansible_ip="1.2.3.4")}, vars={})}
        )
        write_inventory(inv, f)
        assert f.exists()
        assert not (tmp_path / "inventory.yml.tmp").exists()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd ansible && hatch run test:pytest tests/test_inventory.py -v
```

Expected: `ImportError` — `inventory` module not found.

- [ ] **Step 3: Implement inventory.py**

Create `ansible/scripts/inventory.py`:

```python
from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import socket
import yaml


DEFAULT_VARS = {
    "ansible_user": "root",
    "ansible_python_interpreter": "/usr/bin/python3",
    "ansible_ssh_private_key_file": "{{ inventory_dir }}/ansible_ssh_key",
}


@dataclass
class HostEntry:
    ansible_host: str
    ansible_ip: str | None = None
    mac_address: str | None = None


@dataclass
class Group:
    hosts: dict[str, HostEntry] = field(default_factory=dict)
    vars: dict = field(default_factory=dict)


@dataclass
class Inventory:
    groups: dict[str, Group] = field(default_factory=dict)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def load_inventory(path: Path) -> Inventory:
    if not path.exists():
        return Inventory()
    raw = yaml.safe_load(path.read_text())
    inventory = Inventory()
    children = (raw or {}).get("all", {}).get("children", {}) or {}
    for group_name, group_data in children.items():
        group_data = group_data or {}
        hosts = {}
        for hostname, host_data in (group_data.get("hosts") or {}).items():
            host_data = host_data or {}
            hosts[hostname] = HostEntry(
                ansible_host=host_data.get("ansible_host", hostname),
                ansible_ip=host_data.get("ansible_ip"),
                mac_address=host_data.get("mac_address"),
            )
        inventory.groups[group_name] = Group(
            hosts=hosts,
            vars=group_data.get("vars") or {},
        )
    return inventory


def known_ips(inventory: Inventory) -> set[str]:
    ips: set[str] = set()
    for group in inventory.groups.values():
        for host in group.hosts.values():
            if host.ansible_ip:
                ips.add(host.ansible_ip)
            elif _is_ip(host.ansible_host):
                ips.add(host.ansible_host)
            else:
                try:
                    ips.add(socket.gethostbyname(host.ansible_host))
                except socket.gaierror:
                    pass
    return ips


def group_names(inventory: Inventory) -> list[str]:
    return list(inventory.groups.keys())


def add_host(
    inventory: Inventory,
    hostname: str,
    ip: str,
    mac: str | None,
    group: str,
    domain: str,
) -> None:
    if group not in inventory.groups:
        inventory.groups[group] = Group(vars=dict(DEFAULT_VARS))
    inventory.groups[group].hosts[hostname] = HostEntry(
        ansible_host=f"{hostname}.{domain}",
        ansible_ip=ip,
        mac_address=mac,
    )


def write_inventory(inventory: Inventory, path: Path) -> None:
    children: dict = {}
    for group_name, group in inventory.groups.items():
        hosts_dict: dict = {}
        for hostname, entry in group.hosts.items():
            host_data: dict = {"ansible_host": entry.ansible_host}
            if entry.ansible_ip:
                host_data["ansible_ip"] = entry.ansible_ip
            if entry.mac_address:
                host_data["mac_address"] = entry.mac_address
            hosts_dict[hostname] = host_data
        group_dict: dict = {}
        if hosts_dict:
            group_dict["hosts"] = hosts_dict
        if group.vars:
            group_dict["vars"] = group.vars
        children[group_name] = group_dict
    data = {"all": {"children": children}}
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True))
    tmp.replace(path)
```

- [ ] **Step 4: Run tests and confirm they pass**

```bash
cd ansible && hatch run test:pytest tests/test_inventory.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add ansible/scripts/inventory.py ansible/tests/test_inventory.py
git commit -m "feat: add inventory module with load/write/known-ips"
```

---

## Task 4: Zone file module

**Files:**
- Create: `ansible/scripts/zonefile.py`
- Create: `ansible/tests/test_zonefile.py`

- [ ] **Step 1: Write the failing tests**

Create `ansible/tests/test_zonefile.py`:

```python
import pytest
from zonefile import (
    today_serial,
    next_serial,
    parse_serial,
    create_zone,
    append_to_zone,
)

SAMPLE_ZONE = """\
$ORIGIN example.com.
$TTL 300
@   IN SOA ns.example.com. hostmaster.example.com. (
        2026050401   ; serial
        3600         ; refresh
        600          ; retry
        604800       ; expire
        300 )        ; minimum

    IN NS  ns.example.com.
ns  IN A   192.168.3.70

; --- Hosts
host1        IN A  192.168.3.1  ; aa:bb:cc:dd:ee:ff

; --- Wildcard -> Traefik
*   IN A  192.168.3.70
    IN A  192.168.3.71
@   IN A  192.168.3.70
"""


class TestSerial:
    def test_today_serial_is_10_digits(self):
        s = today_serial()
        assert 2026010101 <= s <= 2099123199

    def test_next_serial_increments_by_one(self):
        assert next_serial(2026050401) == 2026050402

    def test_parse_serial_from_soa(self):
        assert parse_serial(SAMPLE_ZONE) == 2026050401

    def test_parse_serial_raises_on_missing(self):
        with pytest.raises(ValueError, match="serial"):
            parse_serial("$ORIGIN example.com.\n")


class TestCreateZone:
    def test_contains_origin(self):
        z = create_zone("example.com", 2026050401, "192.168.3.70", [], ["192.168.3.70"])
        assert "$ORIGIN example.com." in z

    def test_contains_serial(self):
        z = create_zone("example.com", 2026050401, "192.168.3.70", [], ["192.168.3.70"])
        assert "2026050401" in z

    def test_contains_host_a_record(self):
        z = create_zone(
            "example.com",
            2026050401,
            "192.168.3.70",
            [("myhost", "192.168.3.5", "aa:bb:cc:dd:ee:ff")],
            ["192.168.3.70"],
        )
        assert "myhost" in z
        assert "192.168.3.5" in z
        assert "aa:bb:cc:dd:ee:ff" in z

    def test_host_without_mac_has_no_comment(self):
        z = create_zone(
            "example.com", 2026050401, "192.168.3.70",
            [("bare", "192.168.3.9", None)],
            ["192.168.3.70"],
        )
        assert "bare" in z
        assert "bare" in z and ";" not in z.split("bare")[1].split("\n")[0]

    def test_wildcard_block_present(self):
        z = create_zone(
            "example.com", 2026050401, "192.168.3.70", [],
            ["192.168.3.70", "192.168.3.71"],
        )
        assert "*   IN A  192.168.3.70" in z
        assert "    IN A  192.168.3.71" in z

    def test_at_record_points_to_first_wildcard_ip(self):
        z = create_zone(
            "example.com", 2026050401, "192.168.3.70", [], ["192.168.3.70"]
        )
        assert "@   IN A  192.168.3.70" in z


class TestAppendToZone:
    def test_increments_serial(self):
        new_text, old, new = append_to_zone(SAMPLE_ZONE, [])
        assert old == 2026050401
        assert new == 2026050402
        assert "2026050402" in new_text

    def test_new_host_inserted_before_wildcard(self):
        new_text, _, _ = append_to_zone(
            SAMPLE_ZONE, [("newhost", "192.168.3.99", None)]
        )
        wildcard_pos = new_text.index("; --- Wildcard")
        host_pos = new_text.index("newhost")
        assert host_pos < wildcard_pos

    def test_existing_hosts_preserved(self):
        new_text, _, _ = append_to_zone(
            SAMPLE_ZONE, [("newhost", "192.168.3.99", None)]
        )
        assert "host1" in new_text

    def test_mac_written_as_comment(self):
        new_text, _, _ = append_to_zone(
            SAMPLE_ZONE, [("h2", "192.168.3.5", "BB:CC:DD:EE:FF:00")]
        )
        assert "; BB:CC:DD:EE:FF:00" in new_text

    def test_multiple_new_hosts(self):
        new_text, _, _ = append_to_zone(
            SAMPLE_ZONE,
            [("alpha", "192.168.3.10", None), ("beta", "192.168.3.11", None)],
        )
        assert "alpha" in new_text
        assert "beta" in new_text
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd ansible && hatch run test:pytest tests/test_zonefile.py -v
```

Expected: `ImportError` — `zonefile` module not found.

- [ ] **Step 3: Implement zonefile.py**

Create `ansible/scripts/zonefile.py`:

```python
from datetime import date
from pathlib import Path
import re


def today_serial() -> int:
    return int(date.today().strftime("%Y%m%d")) * 100 + 1


def next_serial(current: int) -> int:
    return current + 1


def parse_serial(zone_text: str) -> int:
    m = re.search(r"(\d{8,10})\s*;\s*serial", zone_text)
    if not m:
        raise ValueError("Could not find serial in zone file")
    return int(m.group(1))


def _host_record(hostname: str, ip: str, mac: str | None) -> str:
    line = f"{hostname:<12} IN A  {ip}"
    if mac:
        line += f"  ; {mac}"
    return line


def create_zone(
    domain: str,
    serial: int,
    ns_ip: str,
    hosts: list[tuple[str, str, str | None]],
    wildcard_ips: list[str],
) -> str:
    host_lines = "\n".join(_host_record(h, ip, mac) for h, ip, mac in hosts)
    wildcard_lines = "\n".join(
        f"*   IN A  {ip}" if i == 0 else f"    IN A  {ip}"
        for i, ip in enumerate(wildcard_ips)
    )
    return (
        f"$ORIGIN {domain}.\n"
        f"$TTL 300\n"
        f"@   IN SOA ns.{domain}. hostmaster.{domain}. (\n"
        f"        {serial}   ; serial\n"
        f"        3600       ; refresh\n"
        f"        600        ; retry\n"
        f"        604800     ; expire\n"
        f"        300 )      ; minimum\n"
        f"\n"
        f"    IN NS  ns.{domain}.\n"
        f"ns  IN A   {ns_ip}\n"
        f"\n"
        f"; --- Hosts\n"
        f"{host_lines}\n"
        f"\n"
        f"; --- Wildcard -> Traefik\n"
        f"{wildcard_lines}\n"
        f"@   IN A  {wildcard_ips[0]}\n"
    )


def append_to_zone(
    zone_text: str,
    new_hosts: list[tuple[str, str, str | None]],
) -> tuple[str, int, int]:
    old_serial = parse_serial(zone_text)
    new_serial = next_serial(old_serial)

    zone_text = re.sub(
        r"(\d{8,10})(\s*;\s*serial)",
        lambda m: f"{new_serial}{m.group(2)}",
        zone_text,
    )

    new_lines = "\n".join(_host_record(h, ip, mac) for h, ip, mac in new_hosts)
    marker = "; --- Wildcard"
    if new_lines:
        if marker in zone_text:
            zone_text = zone_text.replace(marker, new_lines + "\n\n" + marker, 1)
        else:
            zone_text = zone_text.rstrip("\n") + "\n" + new_lines + "\n"

    return zone_text, old_serial, new_serial
```

- [ ] **Step 4: Run tests and confirm they pass**

```bash
cd ansible && hatch run test:pytest tests/test_zonefile.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add ansible/scripts/zonefile.py ansible/tests/test_zonefile.py
git commit -m "feat: add zonefile module with create and append operations"
```

---

## Task 5: Corefile module

**Files:**
- Create: `ansible/scripts/corefile.py`
- Create: `ansible/tests/test_corefile.py`

- [ ] **Step 1: Write the failing tests**

Create `ansible/tests/test_corefile.py`:

```python
import pytest
from corefile import remove_stanza, update_corefile

SAMPLE_COREFILE = """\
home.:53 {
  file /etc/coredns/zones/home.db
  log
  errors
}

schollar.dev:53 {
  file /etc/coredns/zones/schollar.dev.db
  log
  errors
}

.:53 {
  cache 30
  forward . 1.1.1.1 9.9.9.9
  log
  errors
}
"""


class TestRemoveStanza:
    def test_removes_target_stanza(self):
        new_text, found = remove_stanza(SAMPLE_COREFILE, "home.")
        assert found is True
        assert "home.:53" not in new_text

    def test_preserves_other_stanzas(self):
        new_text, _ = remove_stanza(SAMPLE_COREFILE, "home.")
        assert "schollar.dev:53" in new_text
        assert ".:53" in new_text

    def test_returns_false_when_not_found(self):
        _, found = remove_stanza(SAMPLE_COREFILE, "nonexistent.")
        assert found is False

    def test_idempotent(self):
        text, _ = remove_stanza(SAMPLE_COREFILE, "home.")
        text2, found = remove_stanza(text, "home.")
        assert found is False
        assert "schollar.dev:53" in text2

    def test_result_has_no_double_blank_lines(self):
        new_text, _ = remove_stanza(SAMPLE_COREFILE, "home.")
        assert "\n\n\n" not in new_text


class TestUpdateCorefile:
    def test_writes_updated_file(self, tmp_path):
        f = tmp_path / "Corefile"
        f.write_text(SAMPLE_COREFILE)
        result = update_corefile(f, "home.")
        assert result is True
        assert "home.:53" not in f.read_text()

    def test_returns_false_when_stanza_absent(self, tmp_path):
        f = tmp_path / "Corefile"
        f.write_text(SAMPLE_COREFILE)
        result = update_corefile(f, "nonexistent.")
        assert result is False

    def test_no_tmp_file_left_behind(self, tmp_path):
        f = tmp_path / "Corefile"
        f.write_text(SAMPLE_COREFILE)
        update_corefile(f, "home.")
        assert not (tmp_path / "Corefile.tmp").exists()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd ansible && hatch run test:pytest tests/test_corefile.py -v
```

Expected: `ImportError` — `corefile` module not found.

- [ ] **Step 3: Implement corefile.py**

Create `ansible/scripts/corefile.py`:

```python
from pathlib import Path
import re


def remove_stanza(text: str, zone_name: str) -> tuple[str, bool]:
    pattern = re.compile(
        r"\n?" + re.escape(zone_name) + r":\d+\s*\{[^}]*\}\n?",
        re.DOTALL,
    )
    new_text, count = pattern.subn("", text)
    # Collapse any resulting triple newlines to double
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    return new_text, count > 0


def update_corefile(path: Path, zone_name: str) -> bool:
    text = path.read_text()
    new_text, found = remove_stanza(text, zone_name)
    if found:
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(new_text)
        tmp.replace(path)
    return found
```

- [ ] **Step 4: Run tests and confirm they pass**

```bash
cd ansible && hatch run test:pytest tests/test_corefile.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
cd ansible && hatch run test:pytest -v
```

Expected: all existing tests plus the 4 new test files pass.

- [ ] **Step 6: Commit**

```bash
git add ansible/scripts/corefile.py ansible/tests/test_corefile.py
git commit -m "feat: add corefile module for removing zone stanzas"
```

---

## Task 6: Main CLI — scan_subnet.py

**Files:**
- Create: `ansible/scripts/scan_subnet.py`

No unit tests for the interactive CLI. Manual smoke test instructions are in Task 7.

- [ ] **Step 1: Implement scan_subnet.py**

Create `ansible/scripts/scan_subnet.py`:

```python
#!/usr/bin/env python3
import argparse
import shutil
import sys
from pathlib import Path

import questionary
from rich.console import Console

from corefile import update_corefile
from inventory import add_host, group_names, known_ips, load_inventory, write_inventory
from scanner import scan_subnet
from zonefile import (
    append_to_zone,
    create_zone,
    next_serial,
    parse_serial,
    today_serial,
)

console = Console()

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent


def _default_paths(domain: str) -> dict[str, Path]:
    return {
        "inventory": REPO_ROOT / "ansible" / "inventory.yml",
        "zone_file": REPO_ROOT / "coredns" / "zones" / f"{domain}.db",
        "corefile": REPO_ROOT / "coredns" / "Corefile",
    }


def _check_nmap() -> None:
    if not shutil.which("nmap"):
        console.print("[red]Error: nmap not found.[/red]")
        console.print("Install with:  sudo apt install nmap")
        sys.exit(1)


def _prompt_wildcard_ips(domain: str) -> list[str]:
    console.print(f"\n[bold]Wildcard *.{domain} should point to which IPs? (your Traefik nodes)[/bold]")
    console.print("Enter IPs one per line, blank to finish:")
    ips = []
    while True:
        val = questionary.text("> ").ask()
        if val is None:
            raise KeyboardInterrupt
        val = val.strip()
        if not val:
            break
        ips.append(val)
    return ips


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan subnet and update Ansible inventory + CoreDNS zone file"
    )
    parser.add_argument("--subnet", default="192.168.3.0/24")
    parser.add_argument("--domain", default="schollar.dev")
    parser.add_argument("--inventory")
    parser.add_argument("--zone-file")
    parser.add_argument("--corefile")
    args = parser.parse_args()

    defaults = _default_paths(args.domain)
    inventory_path = Path(args.inventory) if args.inventory else defaults["inventory"]
    zone_path = Path(args.zone_file) if args.zone_file else defaults["zone_file"]
    corefile_path = Path(args.corefile) if args.corefile else defaults["corefile"]

    _check_nmap()

    inventory = load_inventory(inventory_path)
    existing_ips = known_ips(inventory)

    console.print(f"\n[bold]Scanning {args.subnet}...[/bold]")
    try:
        all_hosts = scan_subnet(args.subnet)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(0)

    if not all_hosts:
        console.print(f"[yellow]No hosts found on {args.subnet}[/yellow]")
        sys.exit(0)

    new_hosts = [h for h in all_hosts if h.ip not in existing_ips]
    console.print(
        f"Scan complete: {len(all_hosts)} hosts found, "
        f"{len(all_hosts) - len(new_hosts)} already in inventory, "
        f"{len(new_hosts)} new"
    )

    if not new_hosts:
        console.print("[green]No new hosts found.[/green]")
        sys.exit(0)

    is_fresh_zone = not zone_path.exists()
    wildcard_ips: list[str] = []

    try:
        if is_fresh_zone:
            wildcard_ips = _prompt_wildcard_ips(args.domain)
            if not wildcard_ips:
                console.print("[red]No wildcard IPs provided. Aborting.[/red]")
                sys.exit(1)

        pending: list[dict] = []
        current_groups = group_names(inventory)
        dynamic_groups: list[str] = []

        for host in new_hosts:
            console.rule()
            console.print(f"[bold]Found:[/bold] {host.ip}")
            if host.mac:
                console.print(f"  MAC: {host.mac}")
            if host.nmap_hostname:
                console.print(f"  nmap name: {host.nmap_hostname}")
            console.print()

            hostname = questionary.text(
                "Hostname (leave blank to skip):",
                default=host.nmap_hostname or "",
            ).ask()
            if hostname is None:
                raise KeyboardInterrupt
            hostname = hostname.strip()
            if not hostname:
                console.print("[dim]Skipped.[/dim]")
                continue

            console.print(f"  → Will register as: [green]{hostname}.{args.domain}[/green]\n")

            choices = current_groups + dynamic_groups + ["+ Create new group"]
            group = questionary.select("Group:", choices=choices).ask()
            if group is None:
                raise KeyboardInterrupt

            if group == "+ Create new group":
                group = questionary.text("New group name:").ask()
                if group is None:
                    raise KeyboardInterrupt
                group = group.strip()
                if group and group not in current_groups and group not in dynamic_groups:
                    dynamic_groups.append(group)

            if group:
                pending.append(
                    {"hostname": hostname, "ip": host.ip, "mac": host.mac, "group": group}
                )

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled. No files written.[/yellow]")
        sys.exit(0)

    if not pending:
        console.print("[yellow]No hosts configured. Nothing to write.[/yellow]")
        sys.exit(0)

    # Confirm
    console.rule()
    if is_fresh_zone:
        serial_info = f"serial: {today_serial()} (new file)"
    else:
        old_s = parse_serial(zone_path.read_text())
        serial_info = f"serial: {old_s} → {next_serial(old_s)}"

    corefile_note = (
        corefile_path.exists()
        and "home." in corefile_path.read_text()
    )

    console.print("[bold]Ready to write:[/bold]")
    console.print(f"  {inventory_path.name:<20} +{len(pending)} hosts")
    console.print(f"  {zone_path.name:<20} +{len(pending)} A records  ({serial_info})")
    if corefile_note:
        console.print(f"  {corefile_path.name:<20} remove home. stanza")
    console.print()

    if not questionary.confirm("Proceed?", default=False).ask():
        console.print("[yellow]Aborted. No files written.[/yellow]")
        sys.exit(0)

    # Write inventory
    for p in pending:
        add_host(inventory, p["hostname"], p["ip"], p["mac"], p["group"], args.domain)
    write_inventory(inventory, inventory_path)

    # Write zone file
    host_tuples = [(p["hostname"], p["ip"], p["mac"]) for p in pending]
    if is_fresh_zone:
        zone_path.parent.mkdir(parents=True, exist_ok=True)
        zone_text = create_zone(
            domain=args.domain,
            serial=today_serial(),
            ns_ip=wildcard_ips[0],
            hosts=host_tuples,
            wildcard_ips=wildcard_ips,
        )
    else:
        existing_zone = zone_path.read_text()
        zone_text, _, _ = append_to_zone(existing_zone, host_tuples)

    tmp = Path(str(zone_path) + ".tmp")
    tmp.write_text(zone_text)
    tmp.replace(zone_path)

    # Update Corefile
    if corefile_path.exists():
        update_corefile(corefile_path, "home.")

    console.print("\n[green]Done![/green]")
    console.print(
        "[dim]Redeploy the coredns stack via Portainer to apply DNS changes.[/dim]"
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run full test suite to confirm nothing broken**

```bash
cd ansible && hatch run test:pytest -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add ansible/scripts/scan_subnet.py
git commit -m "feat: add scan_subnet CLI entry point"
```

---

## Task 7: Manual smoke test

This task cannot be automated — it requires a live network and an interactive terminal.

- [ ] **Step 1: Install nmap on the host if not present**

```bash
which nmap || sudo apt install nmap
```

- [ ] **Step 2: Back up existing files**

```bash
cp ansible/inventory.yml ansible/inventory.yml.bak
cp coredns/zones/schollar.dev.db coredns/zones/schollar.dev.db.bak
cp coredns/Corefile coredns/Corefile.bak
```

- [ ] **Step 3: Remove existing inventory and zone file to test fresh-generation path**

```bash
rm ansible/inventory.yml coredns/zones/schollar.dev.db
```

- [ ] **Step 4: Run the script**

```bash
cd ansible && hatch run python scripts/scan_subnet.py
```

Expected flow:
1. Prompts for wildcard IPs (enter `192.168.3.70` through `192.168.3.73`, then blank)
2. For each new host: shows IP/MAC/nmap name, prompts for hostname and group
3. Shows confirm summary with host count and serial
4. After confirming: writes `inventory.yml`, `schollar.dev.db`, updates `Corefile`

- [ ] **Step 5: Verify outputs**

```bash
# Inventory has new hosts
cat ansible/inventory.yml

# Zone file has A records and wildcard block
cat coredns/zones/schollar.dev.db

# Corefile no longer has home. stanza
grep -c 'home\.' coredns/Corefile   # should print 0
```

- [ ] **Step 6: Restore backups if anything looks wrong**

```bash
cp ansible/inventory.yml.bak ansible/inventory.yml
cp coredns/zones/schollar.dev.db.bak coredns/zones/schollar.dev.db
cp coredns/Corefile.bak coredns/Corefile
```

- [ ] **Step 7: Commit the generated files if everything looks good**

```bash
git add ansible/inventory.yml coredns/zones/schollar.dev.db coredns/Corefile
git commit -m "feat: regenerate inventory and zone file from subnet scan"
```

---

## Self-Review Notes

- **Spec coverage**: All goals covered — scan, diff, interactive hostname+group, MAC stored, fresh vs append zone, Corefile `home.` removal, configurable domain/subnet/paths, atomic writes, Ctrl+C handling.
- **ansible_ip field**: Added beyond spec to make skip-known-hosts reliable without requiring DNS to be live. This is necessary for subsequent runs to work correctly.
- **nmap root caveat**: MAC addresses require nmap to run as root (ARP scan). If not root, `mac` will be `None` — handled gracefully throughout (no crash, no MAC stored).
- **Type consistency**: `Host.mac` → `HostEntry.mac_address` → `host_tuples[2]` — all `str | None`, consistent across all tasks.
