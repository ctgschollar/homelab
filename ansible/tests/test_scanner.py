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
