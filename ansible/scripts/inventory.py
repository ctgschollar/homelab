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
class Inventory:
    hosts: dict[str, HostEntry] = field(default_factory=dict)
    vars: dict = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)


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
    all_section = (raw or {}).get("all", {}) or {}

    inv = Inventory()
    inv.vars = all_section.get("vars") or {}

    for hostname, host_data in (all_section.get("hosts") or {}).items():
        host_data = host_data or {}
        inv.hosts[hostname] = HostEntry(
            ansible_host=host_data.get("ansible_host", hostname),
            ansible_ip=host_data.get("ansible_ip"),
            mac_address=host_data.get("mac_address"),
        )

    for group_name, group_data in (all_section.get("children") or {}).items():
        group_data = group_data or {}
        inv.groups[group_name] = list((group_data.get("hosts") or {}).keys())

    return inv


def known_ips(inventory: Inventory) -> set[str]:
    ips: set[str] = set()
    for host in inventory.hosts.values():
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
    groups: list[str],
    domain: str,
) -> None:
    if not inventory.vars:
        inventory.vars = dict(DEFAULT_VARS)
    inventory.hosts[hostname] = HostEntry(
        ansible_host=f"{hostname}.{domain}",
        ansible_ip=ip,
        mac_address=mac,
    )
    for group in groups:
        if group not in inventory.groups:
            inventory.groups[group] = []
        if hostname not in inventory.groups[group]:
            inventory.groups[group].append(hostname)


def write_inventory(inventory: Inventory, path: Path) -> None:
    hosts_dict: dict = {}
    for hostname, entry in inventory.hosts.items():
        host_data: dict = {"ansible_host": entry.ansible_host}
        if entry.ansible_ip:
            host_data["ansible_ip"] = entry.ansible_ip
        if entry.mac_address:
            host_data["mac_address"] = entry.mac_address
        hosts_dict[hostname] = host_data

    children: dict = {}
    for group_name, hostnames in inventory.groups.items():
        children[group_name] = {"hosts": {h: None for h in hostnames}} if hostnames else {}

    all_section: dict = {}
    if inventory.vars:
        all_section["vars"] = inventory.vars
    if hosts_dict:
        all_section["hosts"] = hosts_dict
    if children:
        all_section["children"] = children

    data = {"all": all_section}
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True))
    tmp.replace(path)
