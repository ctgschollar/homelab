import pytest
from inventory import (
    load_inventory,
    known_ips,
    add_host,
    write_inventory,
    Inventory,
    HostEntry,
    DEFAULT_VARS,
)

SAMPLE_INVENTORY = """\
all:
  vars:
    ansible_user: root
    ansible_python_interpreter: /usr/bin/python3
    ansible_ssh_private_key_file: "{{ inventory_dir }}/ansible_ssh_key"
  hosts:
    dks01:
      ansible_host: 192.168.3.70
      ansible_ip: 192.168.3.70
      mac_address: aa:bb:cc:dd:ee:ff
    claude:
      ansible_host: 192.168.3.79
      ansible_ip: 192.168.3.79
  children:
    proxmox_vms:
      hosts:
        dks01:
        claude:
"""


class TestLoadInventory:
    def test_returns_empty_when_file_missing(self, tmp_path):
        inv = load_inventory(tmp_path / "nonexistent.yml")
        assert inv.hosts == {}
        assert inv.groups == {}

    def test_loads_hosts(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert "dks01" in inv.hosts
        assert inv.hosts["dks01"].ansible_host == "192.168.3.70"

    def test_loads_ansible_ip(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert inv.hosts["dks01"].ansible_ip == "192.168.3.70"

    def test_loads_mac_address(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert inv.hosts["dks01"].mac_address == "aa:bb:cc:dd:ee:ff"

    def test_loads_vars(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert inv.vars["ansible_user"] == "root"

    def test_loads_group_membership(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert "proxmox_vms" in inv.groups
        assert "dks01" in inv.groups["proxmox_vms"]

    def test_host_without_mac_loads_none(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        assert inv.hosts["claude"].mac_address is None


class TestKnownIps:
    def test_returns_ansible_ip_values(self):
        inv = Inventory(
            hosts={
                "h1": HostEntry(ansible_host="h1.example.com", ansible_ip="192.168.3.1"),
                "h2": HostEntry(ansible_host="h2.example.com", ansible_ip="192.168.3.2"),
            }
        )
        ips = known_ips(inv)
        assert "192.168.3.1" in ips
        assert "192.168.3.2" in ips

    def test_returns_raw_ip_ansible_host(self):
        inv = Inventory(hosts={"h": HostEntry(ansible_host="192.168.3.5")})
        ips = known_ips(inv)
        assert "192.168.3.5" in ips

    def test_empty_inventory_returns_empty_set(self):
        assert known_ips(Inventory()) == set()


class TestAddHost:
    def test_adds_to_hosts(self):
        inv = Inventory()
        add_host(inv, "myhost", "192.168.3.5", "AA:BB:CC:DD:EE:FF", ["mygroup"], "example.com")
        assert "myhost" in inv.hosts
        h = inv.hosts["myhost"]
        assert h.ansible_host == "myhost.example.com"
        assert h.ansible_ip == "192.168.3.5"
        assert h.mac_address == "AA:BB:CC:DD:EE:FF"

    def test_sets_default_vars_on_empty_inventory(self):
        inv = Inventory()
        add_host(inv, "myhost", "192.168.3.5", None, ["newgroup"], "example.com")
        assert inv.vars == DEFAULT_VARS

    def test_does_not_overwrite_existing_vars(self):
        inv = Inventory(vars={"ansible_user": "custom"})
        add_host(inv, "myhost", "192.168.3.5", None, ["g"], "example.com")
        assert inv.vars["ansible_user"] == "custom"

    def test_registers_group_membership(self):
        inv = Inventory()
        add_host(inv, "myhost", "192.168.3.5", None, ["mygroup"], "example.com")
        assert "mygroup" in inv.groups
        assert "myhost" in inv.groups["mygroup"]

    def test_registers_multiple_group_memberships(self):
        inv = Inventory()
        add_host(inv, "myhost", "192.168.3.5", None, ["g1", "g2"], "example.com")
        assert "myhost" in inv.groups["g1"]
        assert "myhost" in inv.groups["g2"]

    def test_host_defined_once_with_multiple_groups(self):
        inv = Inventory()
        add_host(inv, "myhost", "192.168.3.5", None, ["g1", "g2"], "example.com")
        assert list(inv.hosts.keys()).count("myhost") == 1

    def test_mac_none_stored_as_none(self):
        inv = Inventory()
        add_host(inv, "h", "192.168.3.5", None, ["g"], "x.com")
        assert inv.hosts["h"].mac_address is None


class TestWriteInventory:
    def test_roundtrip_preserves_hosts(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        write_inventory(inv, f)
        inv2 = load_inventory(f)
        assert "dks01" in inv2.hosts
        assert inv2.hosts["dks01"].ansible_host == "192.168.3.70"
        assert inv2.hosts["dks01"].mac_address == "aa:bb:cc:dd:ee:ff"

    def test_roundtrip_preserves_vars(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        write_inventory(inv, f)
        inv2 = load_inventory(f)
        assert inv2.vars["ansible_user"] == "root"

    def test_roundtrip_preserves_group_membership(self, tmp_path):
        f = tmp_path / "inventory.yml"
        f.write_text(SAMPLE_INVENTORY)
        inv = load_inventory(f)
        write_inventory(inv, f)
        inv2 = load_inventory(f)
        assert "dks01" in inv2.groups["proxmox_vms"]

    def test_host_not_duplicated_in_output(self, tmp_path):
        f = tmp_path / "inventory.yml"
        inv = Inventory()
        add_host(inv, "myhost", "192.168.3.5", None, ["g1", "g2"], "example.com")
        write_inventory(inv, f)
        text = f.read_text()
        assert text.count("myhost") == 4  # key + ansible_host value + once per group

    def test_no_tmp_file_left_behind(self, tmp_path):
        f = tmp_path / "inventory.yml"
        inv = Inventory(hosts={"h": HostEntry(ansible_host="1.2.3.4", ansible_ip="1.2.3.4")})
        write_inventory(inv, f)
        assert f.exists()
        assert not (tmp_path / "inventory.yml.tmp").exists()
