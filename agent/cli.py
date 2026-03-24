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
import sys

import httpx
from pydantic import ValidationError
from rich.console import Console

from agent.agent import ActionLogger, HomelabAgent
from agent.config_schema import AgentConfig, load_agent_config
from agent.log_viewer import browse_log
from agent.monitor import MonitorDaemon

console = Console()


async def _fetch_zar_rate() -> float | None:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("https://api.frankfurter.app/latest?from=USD&to=ZAR")
            return resp.json()["rates"]["ZAR"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> AgentConfig:
    try:
        return load_agent_config(path)
    except ValidationError as e:
        for err in e.errors():
            loc = " → ".join(str(x) for x in err["loc"])
            console.print(f"[bold red]CONFIG ERROR:[/bold red] {loc}: {err['msg']}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Service status check (--check)
# ---------------------------------------------------------------------------

async def run_check(config: AgentConfig) -> None:
    import docker

    socket = config.docker.socket
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
  /cost day|month|year      — show API spend for the period

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


def compute_cost_summary(log_path: str, start: datetime, end: datetime | None = None) -> dict:
    """Read the action log and sum api_cost entries within the time window."""
    total_usd = 0.0
    total_input = 0
    total_output = 0
    calls = 0
    try:
        with open(log_path) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("event") != "api_cost":
                    continue
                ts_str = entry.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < start:
                    continue
                if end and ts >= end:
                    continue
                total_usd += entry.get("cost_usd", 0.0)
                total_input += entry.get("input_tokens", 0)
                total_output += entry.get("output_tokens", 0)
                calls += 1
    except FileNotFoundError:
        pass
    return {"cost_usd": total_usd, "input_tokens": total_input, "output_tokens": total_output, "calls": calls}


def format_cost_report(label: str, period: str, summary: dict, zar_rate: float | None = None) -> str:
    usd = summary["cost_usd"]
    lines = [f"*{label} API spend ({period})*"]
    cost_str = f"${usd:.4f}"
    if zar_rate is not None:
        cost_str += f" / R{usd * zar_rate:.2f}"
    lines.append(f"Cost: {cost_str}")
    lines.append(f"Turns: {summary['calls']}  ({summary['input_tokens']:,}↑ {summary['output_tokens']:,}↓ tokens)")
    return "\n".join(lines)


async def show_cost(log_path: str, args: list[str]) -> None:
    if not args or args[0] not in ("day", "month", "year"):
        console.print("  Usage: /cost day|month|year")
        return
    now = datetime.now(timezone.utc)
    period = args[0]
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label, period_str = "Daily", now.strftime("%Y-%m-%d")
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label, period_str = "Monthly", now.strftime("%B %Y")
    else:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        label, period_str = "Yearly", str(now.year)
    summary = compute_cost_summary(log_path, start)
    zar_rate = await _fetch_zar_rate()
    report = format_cost_report(label, period_str, summary, zar_rate)
    console.print(f"\n  [bold]{label} spend ({period_str}):[/bold]")
    console.print(f"  ${summary['cost_usd']:.4f}", end="")
    if zar_rate:
        console.print(f" / R{summary['cost_usd'] * zar_rate:.2f}", end="")
    console.print(f"  ({summary['calls']} turns, {summary['input_tokens']:,}↑ {summary['output_tokens']:,}↓)")


async def cost_reporter(agent: HomelabAgent, log_path: str) -> None:
    """Background task: post cost summaries to Slack at midnight each day/month/year."""
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_midnight - now).total_seconds())

        zar_rate = await _fetch_zar_rate()

        # Daily — always
        yesterday = next_midnight - timedelta(days=1)
        summary = compute_cost_summary(log_path, yesterday, next_midnight)
        await agent._slack.notify(format_cost_report("Daily", yesterday.strftime("%Y-%m-%d"), summary, zar_rate))

        # Monthly — on the 1st
        if next_midnight.day == 1:
            month_start = (next_midnight - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            summary = compute_cost_summary(log_path, month_start, next_midnight)
            period_str = month_start.strftime("%B %Y")
            await agent._slack.notify(format_cost_report("Monthly", period_str, summary, zar_rate))

        # Yearly — on Jan 1
        if next_midnight.month == 1 and next_midnight.day == 1:
            year_start = next_midnight.replace(year=next_midnight.year - 1)
            summary = compute_cost_summary(log_path, year_start, next_midnight)
            await agent._slack.notify(format_cost_report("Yearly", str(year_start.year), summary, zar_rate))


async def run_repl(agent: HomelabAgent, config: AgentConfig, event_queue: asyncio.Queue, log_path: str) -> None:
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
        elif upper.startswith("/COST"):
            await show_cost(log_path, line.split()[1:])
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

async def _post_cost(agent: HomelabAgent, cost_usd: float) -> None:
    await agent._slack.notify(f"_{agent._last_cost_breakdown}_")


async def event_consumer(agent: HomelabAgent, event_queue: asyncio.Queue) -> None:
    while True:
        event = await event_queue.get()
        try:
            if event["type"] == "user_message":
                source = event.get("source", "cli")
                response, cost_usd = await agent.chat(event["data"]["message"], trigger=f"{source}:user_message")
                if source != "cli":
                    if response:
                        await agent._slack.notify(response)
                    await _post_cost(agent, cost_usd)
            else:
                _, cost_usd = await agent.handle_event(event)
                await _post_cost(agent, cost_usd)
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

    log_path = config.action_log.path
    action_logger = ActionLogger(log_path)

    monitor = MonitorDaemon(config, event_queue, action_logger)

    listener_host = config.approval_listener.host
    listener_port = config.approval_listener.port

    # Slack test mode
    if args.test_slack:
        if not agent._slack.configured:
            console.print("[bold red]Slack is not configured — set SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET.[/bold red]")
            return
        listener_task, listener_server = await agent.start_approval_listener(listener_host, listener_port)
        console.print(f"[dim]Approval listener up on {listener_host}:{listener_port}[/dim]")
        plan_id = "test-plan"
        tool_input = {"command": "cat /etc/debian_version", "agent_proposed_tier": 1, "agent_reasoning": "read-only"}
        plan_text = "*Tool:* `run_shell`\n*Inputs:*\n  command: cat /etc/debian_version"
        fut = agent._pending.register(plan_id, "run_shell", plan_text, tier=1)
        message_ref = await agent._slack.notify_plan(plan_id, plan_text, veto_seconds=None)
        console.print(f"  Test plan [bold yellow]{plan_id}[/bold yellow] posted to Slack — waiting for Approve/Deny…")
        approved, reason = await fut
        if approved:
            console.print(f"  [bold green]Approved![/bold green] reason: {reason or '(none)'}")
            result = await agent._tools.execute("run_shell", tool_input)
            console.print(f"  Result: {result}")
            if message_ref:
                await agent._slack.update_plan_result(*message_ref, plan_id, plan_text, result)
        else:
            console.print(f"  [bold red]Denied.[/bold red] reason: {reason or '(none)'}")
        listener_server.should_exit = True
        await listener_task
        await agent.aclose()
        return

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

    if args.daemon:
        listener_task, listener_server = await agent.start_approval_listener(listener_host, listener_port, event_queue)
        reporter_task = asyncio.create_task(cost_reporter(agent, log_path))
        console.print("[dim]Running in daemon mode. Ctrl+C to stop.[/dim]")
        try:
            await asyncio.gather(monitor_task, consumer_task, listener_task, reporter_task)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            monitor_task.cancel()
            consumer_task.cancel()
            reporter_task.cancel()
            listener_server.should_exit = True
            await asyncio.gather(monitor_task, consumer_task, listener_task, reporter_task, return_exceptions=True)
    else:
        # Interactive REPL — no listener, approvals via CLI only
        try:
            await run_repl(agent, config, event_queue, log_path)
        except KeyboardInterrupt:
            pass
        finally:
            monitor_task.cancel()
            consumer_task.cancel()
            await asyncio.gather(monitor_task, consumer_task, return_exceptions=True)

    await agent.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Homelab sysadmin agent")
    parser.add_argument("message", nargs="?", help="Single question/command (non-interactive mode)")
    parser.add_argument("--daemon", action="store_true", help="Run headlessly as a monitor daemon")
    parser.add_argument("--check", action="store_true", help="Print service status and exit")
    parser.add_argument("--test-slack", action="store_true", help="Post a test plan to Slack and wait for approval")
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
