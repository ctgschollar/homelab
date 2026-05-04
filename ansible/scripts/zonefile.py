from datetime import date
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
