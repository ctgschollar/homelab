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
