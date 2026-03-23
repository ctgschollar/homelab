#!/usr/bin/env python3
"""
Homelab Agent — main entrypoint.

Usage:
  python cli.py                            # interactive REPL + monitor
  python cli.py "why is sonarr down?"     # single question, exit when done
  python cli.py --daemon                  # monitor + agent, no stdin
  python cli.py --check                   # list service status and exit
  python cli.py --config path/to/cfg.yaml
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sys

import yaml
from rich.console import Console

from agent.agent import ActionLogger, HomelabAgent
from agent.log_viewer import browse_log
from agent.monitor import MonitorDaemon

console = Console()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        raw = f.read()

    def _sub(m: re.Match) -> str:
        var = m.group(1)
        return os.environ.get(var, m.group(0))

    raw = re.sub(r"\$\{([^}]+)\}", _sub, raw)
    return yaml.safe_load(raw)


# ---------------------------------------------------------------------------
# Service status check (--check)
# ---------------------------------------------------------------------------

async def run_check(config: dict) -> None:
    import docker

    socket = config.get("docker", {}).get("socket", "unix:///var/run/docker.sock")
    loop = asyncio.get_event_loop()

    def _list() -> list[dict]:
        client = docker.DockerClient(base_url=socket)
        services = client.services.list()
        results = []
        for svc in services:
            spec = svc.attrs.get("Spec", {})
            mode = spec.get("Mode", {})
            replicated = mode.get("Replicated")
            if replicated is None:
                continue
            desired = replicated.get("Replicas", 0)
            tasks = svc.tasks()
            running = sum(
                1 for t in tasks
                if t.get("Status", {}).get("State") == "running"
                and t.get("DesiredState") == "running"
            )
            results.append({"name": svc.name, "running": running, "desired": desired})
        return results

    services = await loop.run_in_executor(None, _list)
    if not services:
        console.print("[dim]No services found.[/dim]")
        return

    for svc in sorted(services, key=lambda s: s["name"]):
        r, d = svc["running"], svc["desired"]
        status = "[green]OK[/green]" if r == d else f"[red]DEGRADED {r}/{d}[/red]"
        console.print(f"  {svc['name']:<45} {status}")


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

REPL_HELP = """\
Built-in commands:
  /quit          — exit
  /status        — show current service health
  /plans         — show plans awaiting approval
  /history       — show conversation turn count
  /safemode      — show current safe mode state (use config_cli.py to change)
  /log           — show all log entries
  /log 1h        — entries from the last hour
  /log 30m       — entries from the last 30 minutes
  /log today     — entries since midnight
  /log 2026-03-23           — entries for a specific date
  /log 2026-03-23 2026-03-24 — entries between two dates

Approvals (when a plan is waiting):
  y / yes                   — approve the pending plan
  n / no                    — deny the pending plan
  APPROVE <id> / STOP <id>  — approve/deny by ID (for Slack or multiple plans)
  any other text            — cancel pending plan(s) and send as agent context
"""


# ---------------------------------------------------------------------------
# Action log viewer
# ---------------------------------------------------------------------------


def _parse_log_range(args: list[str]) -> tuple[datetime | None, datetime | None]:
    """Parse /log arguments into (start, end) datetimes (UTC). Both may be None."""
    now = datetime.now(timezone.utc)

    if not args:
        return None, None

    # Relative: 30m, 2h, 1d
    m = re.fullmatch(r"(\d+)(m|h|d)", args[0].lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)
        return now - delta, None

    if args[0].lower() == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, None

    # Absolute date(s): YYYY-MM-DD [YYYY-MM-DD]
    def _parse_date(s: str) -> datetime:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    try:
        start = _parse_date(args[0])
        end = _parse_date(args[1]) + timedelta(days=1) if len(args) > 1 else start + timedelta(days=1)
        return start, end
    except ValueError:
        pass

    return None, None


async def show_log(log_path: str, args: list[str]) -> None:
    start, end = _parse_log_range(args)

    if args and start is None and end is None:
        console.print(f"  [red]Unrecognised range: {' '.join(args)}[/red]")
        console.print("  Try: /log 1h  |  /log today  |  /log 2026-03-23")
        return

    try:
        with open(log_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        console.print("  [dim]No action log found yet.[/dim]")
        return

    entries: list[tuple[datetime | None, dict]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts_str = entry.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            ts = None

        if ts and start and ts < start:
            continue
        if ts and end and ts >= end:
            continue
        entries.append((ts, entry))

    if not entries:
        console.print("  [dim]No log entries for that range.[/dim]")
        return

    await browse_log(entries)


async def run_repl(agent: HomelabAgent, config: dict, event_queue: asyncio.Queue, log_path: str) -> None:
    loop = asyncio.get_event_loop()
    console.print("[bold cyan]Homelab Agent[/bold cyan] — type /quit to exit, /help for commands.")

    while True:
        try:
            line: str = await loop.run_in_executor(None, lambda: input("\n> "))
        except (EOFError, KeyboardInterrupt):
            break

        line = line.strip()
        if not line:
            continue

        upper = line.upper()
        if line == "/quit":
            break
        elif line in ("/help", "/?"):
            console.print(REPL_HELP)
        elif line == "/status":
            await run_check(config)
        elif line == "/plans":
            active = agent._active_execution
            plans = agent._pending.all_plans()
            if not active and not plans:
                console.print("  [dim]No active or pending plans.[/dim]")
            else:
                if active:
                    age = int((datetime.now(timezone.utc) - active["started_at"]).total_seconds())
                    console.print(f"  [bold green]Executing:[/bold green] {active['plan_id']}  tool={active['tool']}  running={age}s")
                    for k, v in active["input"].items():
                        console.print(f"    [dim]{k}: {v}[/dim]")
                if plans:
                    console.print(f"\n  [bold]{len(plans)} plan(s) awaiting approval:[/bold]")
                    for p in plans:
                        age = int((datetime.now(timezone.utc) - p["proposed_at"]).total_seconds())
                        console.print(f"\n  [bold yellow]{p['plan_id']}[/bold yellow]  tier={p['tier']}  tool={p['tool']}  waiting={age}s")
                        for ln in p["plan_text"].splitlines():
                            console.print(f"  [dim]{ln}[/dim]")
        elif line == "/history":
            n = len(agent._history)
            console.print(f"History: {n} messages ({n // 2} turn-pairs)")
        elif line == "/safemode":
            state = "ON" if agent._safety.global_safe_mode else "OFF"
            console.print(f"Global safe mode: [bold]{state}[/bold]")
            console.print("Use [cyan]python config_cli.py safemode on|off[/cyan] to change.")
        elif upper.startswith("/LOG"):
            log_args = line.split()[1:]
            await show_log(log_path, log_args)
        elif upper in ("Y", "YES", "N", "NO"):
            # Shorthand: approve or deny the single pending plan (CLI-only convenience)
            pending_ids = agent._pending.known_ids()
            if not pending_ids:
                console.print("  [dim]No pending plan to approve.[/dim]")
            elif len(pending_ids) > 1:
                console.print(f"  [dim]Multiple plans pending — use APPROVE/STOP <id>: {', '.join(pending_ids)}[/dim]")
            else:
                plan_id = pending_ids[0]
                approved = upper in ("Y", "YES")
                agent._pending.resolve(plan_id, approved)
                if approved:
                    await asyncio.sleep(0)
                    console.print(f"  [dim]Executing {plan_id}… (type /plans to check progress)[/dim]")
        elif upper.startswith("APPROVE ") or upper.startswith("STOP "):
            command, _, plan_id = line.partition(" ")
            plan_id = plan_id.strip().lower()
            approved = command.upper() == "APPROVE"
            found = agent._pending.resolve(plan_id, approved)
            if not found:
                console.print(f"  [dim]Unknown plan ID: {plan_id}[/dim]")
            elif approved:
                await asyncio.sleep(0)
                console.print(f"  [dim]Executing {plan_id}… (type /plans to check progress)[/dim]")
        else:
            # Free-form message: if plans are pending, cancel them so the agent
            # can re-plan with the user's new context.
            pending_ids = agent._pending.known_ids()
            if pending_ids:
                cancelled = agent._pending.cancel_all(reason=line)
                console.print(
                    f"  [dim]Cancelled {len(cancelled)} pending plan(s). "
                    f"Sending your message to the agent...[/dim]"
                )
            await event_queue.put({
                "source": "cli",
                "type": "user_message",
                "data": {"message": line},
                "timestamp": datetime.now(timezone.utc),
            })


# ---------------------------------------------------------------------------
# Event consumer task
# ---------------------------------------------------------------------------

async def event_consumer(agent: HomelabAgent, event_queue: asyncio.Queue) -> None:
    while True:
        event = await event_queue.get()
        try:
            if event["type"] == "user_message":
                await agent.chat(event["data"]["message"], trigger="cli:user_message")
            else:
                await agent.handle_event(event)
        except Exception as exc:
            console.print(f"\n[bold red]Event consumer error:[/bold red] {exc}")
        finally:
            event_queue.task_done()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    event_queue: asyncio.Queue = asyncio.Queue()

    agent = HomelabAgent(config)

    log_path = config.get("action_log", {}).get("path", "./action.log")
    action_logger = ActionLogger(log_path)

    monitor = MonitorDaemon(config, event_queue, action_logger)

    listener_cfg = config.get("approval_listener", {})
    listener_host = listener_cfg.get("host", "0.0.0.0")
    listener_port = int(listener_cfg.get("port", 8765))

    # Single message mode
    if args.message:
        consumer_task = asyncio.create_task(event_consumer(agent, event_queue))
        await agent.chat(args.message, trigger="cli:user_message")
        await event_queue.join()
        consumer_task.cancel()
        await agent.aclose()
        return

    # Start background tasks
    monitor_task = asyncio.create_task(monitor.run())
    consumer_task = asyncio.create_task(event_consumer(agent, event_queue))
    listener_task = await agent.start_approval_listener(listener_host, listener_port)

    all_tasks = [monitor_task, consumer_task, listener_task]

    if args.daemon:
        console.print("[dim]Running in daemon mode. Ctrl+C to stop.[/dim]")
        try:
            await asyncio.gather(*all_tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
    else:
        # Interactive REPL
        try:
            await run_repl(agent, config, event_queue, log_path)
        except KeyboardInterrupt:
            pass
        finally:
            for t in all_tasks:
                t.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)

    await agent.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Homelab sysadmin agent")
    parser.add_argument("message", nargs="?", help="Single question/command (non-interactive mode)")
    parser.add_argument("--daemon", action="store_true", help="Run headlessly as a monitor daemon")
    parser.add_argument("--check", action="store_true", help="Print service status and exit")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)

    try:
        if args.check:
            asyncio.run(run_check(config))
            return

        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
