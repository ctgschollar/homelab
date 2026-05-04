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
