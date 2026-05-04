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
