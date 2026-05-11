# controller.py
from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from rich.console import Console

from agent_base import AgentBase

if TYPE_CHECKING:
    from agent.config_schema import AgentConfig, ModelEntry
    from agent.rag import IncidentRAG
    from agent.slack import SlackClient

console = Console()

_COMMANDS = frozenset(["stop", "start", "queue", "mode monitor", "mode act", "model", "help", "context", "history"])


@dataclass
class DeferredAlert:
    alert_id: str
    event: dict
    services: list[str]
    timer_task: asyncio.Task
    slack_message_ref: tuple[str, str] | None
    deferred_at: datetime


class AgentController:
    def __init__(
        self,
        config: AgentConfig,
        agents: dict[str, AgentBase],
        slack: SlackClient,
        config_path: str = "config.yaml",
        rag: IncidentRAG | None = None,
    ) -> None:
        self._config = config
        self.agents = agents
        self._slack = slack
        self._config_path = config_path
        self._rag = rag
        self._whitelist_path = Path(config.controller.whitelist_path)
        self._grace_period = config.monitor.grace_period_seconds

        self.mode: Literal["monitor", "act"] = config.controller.mode
        self.whitelist: set[str] = self._load_whitelist()
        self.stopped: bool = False
        self.deferred: dict[str, DeferredAlert] = {}
        self._active_agent_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Whitelist persistence
    # ------------------------------------------------------------------

    def _load_whitelist(self) -> set[str]:
        if self._whitelist_path.exists():
            try:
                return set(json.loads(self._whitelist_path.read_text()))
            except Exception:
                return set()
        return set()

    def _save_whitelist(self) -> None:
        self._whitelist_path.write_text(json.dumps(sorted(self.whitelist), indent=2))

    # ------------------------------------------------------------------
    # Mode persistence
    # ------------------------------------------------------------------

    def _persist_mode(self, mode: str) -> None:
        try:
            with open(self._config_path) as f:
                data = yaml.safe_load(f) or {}
            data.setdefault("controller", {})["mode"] = mode
            with open(self._config_path, "w") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        except Exception as exc:
            console.print(f"[yellow]Warning: could not persist mode to config: {exc}[/yellow]")

    # ------------------------------------------------------------------
    # Command detection
    # ------------------------------------------------------------------

    def is_command(self, text: str) -> bool:
        lower = text.lower().strip()
        return lower in _COMMANDS or lower.startswith("model ") or lower.startswith("context ") or lower.startswith("history ")

    # ------------------------------------------------------------------
    # Event routing
    # ------------------------------------------------------------------

    async def handle_event(self, event: dict) -> None:
        etype = event.get("type", "")
        source = event.get("source", "")

        if self.stopped and etype != "user_message":
            return

        if etype == "user_message":
            await self._run_agent_chat(event)
            return

        if self.mode == "monitor":
            await self._notify_only(event)
            return

        if source == "monitor":
            if etype == "services_down":
                await self._defer(event)
                return
            if etype == "service_recovered":
                await self._handle_recovery(event)
                return

        await self._run_agent(event)

    async def _notify_only(self, event: dict) -> None:
        etype = event.get("type", "")
        data = event.get("data", {})
        if etype == "services_down":
            services = [s["service"] for s in data.get("services", [])]
            await self._slack.notify(
                f"👁 Monitor alert: {', '.join(f'`{s}`' for s in services)} degraded. "
                f"(monitor-only mode — not acting)"
            )
        elif etype == "service_recovered":
            svc = data.get("service", "unknown")
            await self._slack.notify(f"✅ `{svc}` recovered.")

    async def _run_agent(self, event: dict) -> None:
        agent = self.agents["default"]
        task = asyncio.create_task(agent.handle_event(event))
        agent._active_task = task  # type: ignore[attr-defined]
        self._active_agent_task = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._active_agent_task = None
            agent._active_task = None  # type: ignore[attr-defined]

    async def _run_agent_chat(self, event: dict) -> None:
        source = event.get("source", "cli")
        message = event["data"]["message"]
        agent = self.agents["default"]
        task = asyncio.create_task(
            agent.chat(message, trigger=f"{source}:user_message")
        )
        agent._active_task = task  # type: ignore[attr-defined]
        self._active_agent_task = task
        try:
            response, _ = await task
            if source != "cli" and response:
                await self._slack.notify(response)
        except asyncio.CancelledError:
            pass
        finally:
            self._active_agent_task = None
            agent._active_task = None  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Control commands
    # ------------------------------------------------------------------

    async def handle_command(self, text: str) -> str:
        lower = text.lower().strip()
        if lower == "help":
            return self._cmd_help()
        if lower == "stop":
            return await self._cmd_stop()
        if lower == "start":
            return await self._cmd_start()
        if lower == "queue":
            return self._cmd_queue()
        if lower == "mode monitor":
            return await self._cmd_mode("monitor")
        if lower == "mode act":
            return await self._cmd_mode("act")
        if lower == "model" or lower.startswith("model "):
            return await self._cmd_model(text.strip())
        if lower == "context" or lower.startswith("context "):
            return await self._cmd_context(text.strip())
        if lower == "history" or lower.startswith("history "):
            return await self._cmd_history(text.strip())
        return f"Unknown command: {text!r}"

    async def _cmd_stop(self) -> str:
        self.stopped = True
        for alert in list(self.deferred.values()):
            alert.timer_task.cancel()
        self.deferred.clear()
        if self._active_agent_task:
            self._active_agent_task.cancel()
        await self.agents["default"].cancel_all()
        return "🛑 Stopped. All pending work cancelled. Type `start` to resume."

    async def _cmd_start(self) -> str:
        self.stopped = False
        return "✅ Resumed."

    def _cmd_queue(self) -> str:
        if not self.deferred:
            return "No pending investigations in queue."
        now = datetime.now(timezone.utc)
        lines = [f"*{len(self.deferred)} pending investigation(s):*"]
        for alert in self.deferred.values():
            age = int((now - alert.deferred_at).total_seconds())
            remaining = max(0, self._grace_period - age)
            lines.append(
                f"• `{alert.alert_id}` — {', '.join(alert.services)} "
                f"— waiting {age}s, {remaining}s remaining"
            )
        return "\n".join(lines)

    async def _cmd_mode(self, mode: Literal["monitor", "act"]) -> str:
        self.mode = mode
        self._persist_mode(mode)
        if mode == "monitor":
            return "👁 Monitor-only mode. I'll notify but not act."
        return "⚡ Act mode. I'll investigate and propose actions."

    async def _cmd_model(self, text: str) -> str:
        parts = text.split(None, 2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if not sub:
            return f"Current model: `{self._config.llm.model}` ({self._config.llm.provider})"
        if sub == "list":
            return self._cmd_model_list()
        if sub == "use":
            return await self._cmd_model_use(arg)
        if sub == "add":
            return await self._cmd_model_add(arg)
        if sub == "remove":
            return await self._cmd_model_remove(arg)
        return (
            f"Unknown model subcommand: `{sub}`. "
            "Try: `model`, `model list`, `model use <name>`, `model add <name>`, `model remove <name>`"
        )

    def _cmd_model_list(self) -> str:
        available = self._config.llm.available_models
        if not available:
            return "No models configured. Use `model add <name>` to add one."
        current = self._config.llm.model
        lines = ["*Available models:*"]
        for m in available:
            marker = " ← active" if m.name == current else ""
            lines.append(f"• `{m.name}` ({m.provider}){marker}")
        return "\n".join(lines)

    async def _cmd_model_use(self, name: str) -> str:
        if not name:
            return "Usage: `model use <name>`"
        entry = next((m for m in self._config.llm.available_models if m.name == name), None)
        if entry is None:
            return f"`{name}` is not in available models. Use `model add {name}` first."
        self._config.llm.model = entry.name
        self._config.llm.provider = entry.provider
        self._config.llm.base_url = entry.base_url
        self._config.llm.api_key = entry.api_key
        self._config.llm.input_cost_per_mtok = entry.input_cost_per_mtok
        self._config.llm.output_cost_per_mtok = entry.output_cost_per_mtok
        self._config.llm.num_ctx = entry.num_ctx
        self._persist_active_model(entry)
        agent = self.agents.get("default")
        if agent is not None and hasattr(agent, "switch_backend"):
            agent.switch_backend(entry)  # type: ignore[attr-defined]
        return f"✅ Switched to `{name}` ({entry.provider})"

    async def _cmd_model_add(self, arg: str) -> str:
        parts = arg.split(None, 1)
        name = parts[0] if parts else ""
        provider = parts[1].strip() if len(parts) > 1 else "anthropic"
        if not name:
            return "Usage: `model add <name> [provider]`"
        if provider not in ("anthropic", "ollama"):
            return f"Unknown provider `{provider}`. Use `anthropic` or `ollama`."
        if any(m.name == name for m in self._config.llm.available_models):
            return f"✅ `{name}` already in available models."
        from agent.config_schema import ModelEntry
        base_url = ""
        if provider == "ollama":
            existing = next((m for m in self._config.llm.available_models if m.provider == "ollama"), None)
            base_url = existing.base_url if existing else self._config.llm.base_url
        self._config.llm.available_models.append(ModelEntry(name=name, provider=provider, base_url=base_url))
        self._persist_available_models(self._config.llm.available_models)
        return f"✅ Added `{name}` ({provider}) to available models."

    async def _cmd_model_remove(self, name: str) -> str:
        if not name:
            return "Usage: `model remove <name>`"
        entry = next((m for m in self._config.llm.available_models if m.name == name), None)
        if entry is None:
            return f"`{name}` is not in available models."
        if name == self._config.llm.model:
            return f"Cannot remove `{name}` — it is the active model. Switch first with `model use <other>`."
        self._config.llm.available_models.remove(entry)
        self._persist_available_models(self._config.llm.available_models)
        return f"✅ Removed `{name}` from available models."

    _VALID_CTX_K = frozenset([1, 4, 8, 16, 32, 64, 128])

    async def _cmd_context(self, text: str) -> str:
        parts = text.split(None, 2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if not sub:
            k = self._config.llm.num_ctx // 1024
            return f"Current context: `{self._config.llm.num_ctx}` tokens ({k}K)"
        if sub == "set":
            if not arg:
                return f"Usage: `context set <k>` — valid values: {sorted(self._VALID_CTX_K)}"
            try:
                k = int(arg.strip())
            except ValueError:
                return f"Invalid value `{arg}` — must be an integer."
            if k not in self._VALID_CTX_K:
                return f"Invalid value `{k}` — valid values: {sorted(self._VALID_CTX_K)}"
            num_ctx = k * 1024
            self._config.llm.num_ctx = num_ctx
            self._persist_num_ctx(num_ctx)
            agent = self.agents.get("default")
            if agent is not None and hasattr(agent, "update_num_ctx"):
                agent.update_num_ctx(num_ctx)  # type: ignore[attr-defined]
            return f"✅ Context set to `{num_ctx}` tokens ({k}K)"
        return f"Unknown context subcommand: `{sub}`. Try: `context`, `context set <k>`"

    async def _cmd_history(self, text: str) -> str:
        parts = text.split(None, 1)
        sub = parts[1].lower().strip() if len(parts) > 1 else ""
        agent = self.agents.get("default")
        if sub == "clear":
            if agent is not None and hasattr(agent, "clear_history"):
                agent.clear_history()  # type: ignore[attr-defined]
            return "✅ History cleared."
        if sub == "summary":
            if agent is None or not hasattr(agent, "get_summary"):
                return "Agent does not support history summary."
            summary = await agent.get_summary()  # type: ignore[attr-defined]
            if not summary:
                return "No history to summarize."
            await self._slack.notify(f"📋 *History summary:*\n{summary}")
            return summary
        return "Usage: `history clear` | `history summary`"

    def _cmd_help(self) -> str:
        return (
            "*Homelab Agent — Slack commands:*\n"
            "• `help` — show this message\n"
            "• `stop` — stop the agent and cancel all pending work\n"
            "• `start` — resume the agent\n"
            "• `queue` — show pending investigations\n"
            "• `mode monitor` — notify only, don't act\n"
            "• `mode act` — investigate and propose actions\n"
            "• `model` — show active model\n"
            "• `model list` — show all available models\n"
            "• `model use <name>` — switch active model\n"
            "• `model add <name> [provider]` — add model (provider: anthropic or ollama, default anthropic)\n"
            "• `model remove <name>` — remove model from available list\n"
            "• `context` — show active context size\n"
            "• `context set <k>` — set context window (k in thousands: 1, 4, 8, 16, 32, 64, 128)\n"
            "• `history clear` — clear conversation history\n"
            "• `history summary` — summarize and post current history\n"
            "\nAnything else is sent to the agent as a question."
        )

    def _persist_active_model(self, entry: "ModelEntry") -> None:
        try:
            with open(self._config_path) as f:
                data = yaml.safe_load(f) or {}
            data.setdefault("llm", {}).update({
                "model": entry.name,
                "provider": entry.provider,
                "base_url": entry.base_url,
                "api_key": entry.api_key,
                "input_cost_per_mtok": entry.input_cost_per_mtok,
                "output_cost_per_mtok": entry.output_cost_per_mtok,
                "num_ctx": entry.num_ctx,
            })
            with open(self._config_path, "w") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        except Exception as exc:
            console.print(f"[yellow]Warning: could not persist active model to config: {exc}[/yellow]")

    def _persist_num_ctx(self, num_ctx: int) -> None:
        try:
            with open(self._config_path) as f:
                data = yaml.safe_load(f) or {}
            data.setdefault("llm", {})["num_ctx"] = num_ctx
            with open(self._config_path, "w") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        except Exception as exc:
            console.print(f"[yellow]Warning: could not persist num_ctx to config: {exc}[/yellow]")

    def _persist_available_models(self, models: list["ModelEntry"]) -> None:
        try:
            with open(self._config_path) as f:
                data = yaml.safe_load(f) or {}
            data.setdefault("llm", {})["available_models"] = [m.model_dump() for m in models]
            with open(self._config_path, "w") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        except Exception as exc:
            console.print(f"[yellow]Warning: could not persist available models to config: {exc}[/yellow]")

    # ------------------------------------------------------------------
    # Grace period
    # ------------------------------------------------------------------

    async def _defer(self, event: dict) -> None:
        alert_id = f"alert-{secrets.token_hex(4)}"
        services = [s["service"] for s in event["data"].get("services", [])]
        message_ref = await self._slack.notify_deferred_alert(
            alert_id, services, self._grace_period
        )
        timer = asyncio.create_task(self._grace_period_timer(alert_id))
        self.deferred[alert_id] = DeferredAlert(
            alert_id=alert_id,
            event=event,
            services=services,
            timer_task=timer,
            slack_message_ref=message_ref,
            deferred_at=datetime.now(timezone.utc),
        )

    async def _grace_period_timer(self, alert_id: str) -> None:
        await asyncio.sleep(self._grace_period)
        alert = self.deferred.pop(alert_id, None)
        if alert is not None:
            await self._run_agent(alert.event)

    async def _handle_recovery(self, event: dict) -> None:
        service = event["data"]["service"]
        duration = event["data"].get("down_duration_seconds", 0)
        for alert_id, alert in list(self.deferred.items()):
            if service in alert.services:
                alert.timer_task.cancel()
                del self.deferred[alert_id]
                await self._slack.notify(
                    f"✅ `{service}` recovered on its own after {duration}s — no investigation needed."
                )
                if self._rag is not None:
                    await self._write_self_healed_incident(service, duration)
                return
        # No deferred alert — agent was already working on it
        await self._run_agent(event)

    async def _write_self_healed_incident(self, service: str, duration: int) -> None:
        count = await self._rag.count_incidents()
        inc_id = f"INC-{count + 1:04d}"
        now = datetime.now(timezone.utc)
        await self._rag.store_incident({
            "id": inc_id,
            "title": f"{service}-self-healed",
            "date": now,
            "tags": ["recovery", "self-healed"],
            "inciting_incident": (
                f"`{service}` degraded and recovered without agent intervention after {duration}s."
            ),
            "resolution": "Cluster software self-healed the service. No agent action taken.",
            "tools_used": [],
        })

    async def start_alert(self, alert_id: str) -> bool:
        alert = self.deferred.pop(alert_id, None)
        if alert is None:
            return False
        alert.timer_task.cancel()
        asyncio.create_task(self._run_agent(alert.event))
        return True

    async def ignore_alert(self, alert_id: str) -> bool:
        alert = self.deferred.pop(alert_id, None)
        if alert is None:
            return False
        alert.timer_task.cancel()
        return True

    # ------------------------------------------------------------------
    # Whitelist management
    # ------------------------------------------------------------------

    async def add_to_whitelist(self, command: str) -> None:
        self.whitelist.add(command)
        self._save_whitelist()
        agent = self.agents["default"]
        if hasattr(agent, "_safety"):
            agent._safety.update_whitelist(self.whitelist)  # type: ignore[attr-defined]
        await self._slack.notify(f"✅ Added to whitelist: `{command}`")
