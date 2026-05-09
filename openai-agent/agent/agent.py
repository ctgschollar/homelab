import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("homelab.agent")

import httpx
import ollama
import uvicorn
from fastapi import FastAPI, Request, Response
from rich.console import Console
from rich.text import Text

from .config_schema import AgentConfig
from .prompts import build_system_prompt
from .rag import IncidentRAG
from .safety import SafetyPolicy
from .slack import SlackClient
from .tools import TOOL_DEFINITIONS, ToolExecutor

MAX_ITERATIONS = 15
MAX_HISTORY_TURNS = 20

console = Console()


def _resolve_listener_host(host: str, signing_secret_configured: bool) -> str:
    if not signing_secret_configured and host == "0.0.0.0":
        console.print("[bold red]WARNING: Slack signing secret not configured — approval listener restricted to localhost[/bold red]")
        return "127.0.0.1"
    return host


# ---------------------------------------------------------------------------
# Simple dataclass to represent a pending tool call uniformly
# ---------------------------------------------------------------------------

@dataclass
class _ToolCall:
    id: str
    name: str
    input: dict


# ---------------------------------------------------------------------------
# Action Logger
# ---------------------------------------------------------------------------

class ActionLogger:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def log(self, record: dict) -> None:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        async with self._lock:
            with open(self._path, "a") as f:
                f.write(json.dumps(record) + "\n")

    async def log_action_taken(
        self,
        tool: str,
        tool_input: dict,
        outcome: str,
        tier: int,
        safe_mode_active: bool,
        trigger: str,
    ) -> None:
        await self.log({
            "event": "action_taken",
            "tool": tool,
            "input": tool_input,
            "outcome": outcome,
            "tier": tier,
            "safe_mode_active": safe_mode_active,
            "trigger": trigger,
        })

    async def log_plan_proposed(
        self,
        plan_id: str,
        tool: str,
        tool_input: dict,
        plan_text: str,
        tier: int,
        safe_mode_active: bool,
        trigger: str,
    ) -> None:
        await self.log({
            "event": "plan_proposed",
            "plan_id": plan_id,
            "tool": tool,
            "input": tool_input,
            "plan_text": plan_text,
            "tier": tier,
            "safe_mode_active": safe_mode_active,
            "trigger": trigger,
        })

    async def log_plan_approved(self, plan_id: str, tool: str) -> None:
        await self.log({
            "event": "plan_approved",
            "plan_id": plan_id,
            "tool": tool,
            "approved_by": "slack:APPROVE",
        })

    async def log_plan_cancelled(self, plan_id: str, tool: str, reason: str) -> None:
        await self.log({
            "event": "plan_cancelled",
            "plan_id": plan_id,
            "tool": tool,
            "reason": reason,
        })

    async def log_tier_reasoning(
        self,
        tool: str,
        agent_proposed_tier: int,
        reasoning: str,
        safe_mode_active: bool,
        effective_tier: int,
        override_reason: str | None = None,
        guard_matched_list: str | None = None,
        guard_matched_pattern: str | None = None,
    ) -> None:
        entry: dict = {
            "event": "tier_reasoning",
            "tool": tool,
            "agent_proposed_tier": agent_proposed_tier,
            "reasoning": reasoning,
            "safe_mode_active": safe_mode_active,
            "effective_tier": effective_tier,
        }
        if override_reason is not None:
            entry["override_reason"] = override_reason
            entry["guard_matched_list"] = guard_matched_list
            entry["guard_matched_pattern"] = guard_matched_pattern
        await self.log(entry)

    async def log_cost(
        self,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
        trigger: str,
    ) -> None:
        await self.log({
            "event": "api_cost",
            "cost_usd": cost_usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "trigger": trigger,
        })


# ---------------------------------------------------------------------------
# Pending Approvals
# ---------------------------------------------------------------------------

class PendingApprovals:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}
        self._meta: dict[str, dict] = {}

    def register(self, plan_id: str, tool: str, plan_text: str, tier: int) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._futures[plan_id] = fut
        self._meta[plan_id] = {
            "tool": tool,
            "plan_text": plan_text,
            "tier": tier,
            "proposed_at": datetime.now(timezone.utc),
        }
        return fut

    def resolve(self, plan_id: str, approved: bool, reason: str = "") -> bool:
        fut = self._futures.pop(plan_id, None)
        self._meta.pop(plan_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result((approved, reason))
        return True

    def cancel_all(self, reason: str) -> list[str]:
        ids = list(self._futures.keys())
        for plan_id in ids:
            self.resolve(plan_id, False, reason)
        return ids

    def known_ids(self) -> list[str]:
        return list(self._futures.keys())

    def all_plans(self) -> list[dict]:
        return [
            {"plan_id": pid, **meta}
            for pid, meta in sorted(self._meta.items(), key=lambda x: x[1]["proposed_at"])
        ]


# ---------------------------------------------------------------------------
# Slack approval listener (FastAPI)
# ---------------------------------------------------------------------------

def build_approval_app(
    pending: PendingApprovals,
    slack: "SlackClient",
    event_queue: asyncio.Queue | None = None,
    controller: Any = None,
) -> FastAPI:
    app = FastAPI()
    _message_cache: dict[str, tuple[str, str, str]] = {}

    @app.post("/slack/events")
    async def slack_events(request: Request) -> Response:
        raw_body = await request.body()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if slack.signature_verification_enabled and not slack.verify_signature(timestamp, raw_body, signature):
            return Response(content="Invalid signature", status_code=403)

        body = json.loads(raw_body)
        if body.get("type") == "url_verification":
            return Response(content=json.dumps({"challenge": body["challenge"]}), media_type="application/json")

        if body.get("type") == "event_callback":
            event = body.get("event", {})
            if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
                text = event.get("text", "").strip()
                if text and event_queue is not None:
                    if controller is not None and controller.is_command(text):
                        response = await controller.handle_command(text)
                        await slack.notify(response)
                    else:
                        await event_queue.put({
                            "source": "slack",
                            "type": "user_message",
                            "data": {"message": text},
                            "timestamp": datetime.now(timezone.utc),
                        })

        return Response(content="", status_code=200)

    @app.post("/slack/interactions")
    async def slack_interactions(request: Request) -> Response:
        raw_body = await request.body()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        console.print(f"  [dim]Slack interaction received — timestamp={timestamp!r} sig={signature[:20]!r}…[/dim]")

        if slack.signature_verification_enabled and not slack.verify_signature(timestamp, raw_body, signature):
            console.print("  [bold red]Slack signature verification failed[/bold red]")
            return Response(content="Invalid signature", status_code=403)

        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
        interaction_type = payload.get("type")
        console.print(f"  [dim]Slack interaction type: {interaction_type!r}[/dim]")

        if interaction_type == "block_actions":
            for action in payload.get("actions", []):
                action_id = action.get("action_id")
                value = action.get("value", "")

                if action_id == "alert_start":
                    if controller is not None:
                        await controller.start_alert(value)
                    return Response(content="", status_code=200)

                if action_id == "alert_ignore":
                    if controller is not None:
                        await controller.ignore_alert(value)
                    return Response(content="", status_code=200)

                if action_id not in ("plan_approve", "plan_deny", "plan_approve_whitelist"):
                    continue

                if action_id == "plan_approve_whitelist":
                    data = json.loads(value) if value else {}
                    plan_id = data.get("plan_id", "")
                    command = data.get("command", "")
                    if command and controller is not None:
                        await controller.add_to_whitelist(command)
                    channel = payload.get("channel", {}).get("id", "")
                    ts = payload.get("message", {}).get("ts", "")
                    user = payload.get("user", {}).get("name", "slack")
                    if channel and ts:
                        plan_text = ""
                        for block in payload.get("message", {}).get("blocks", []):
                            if block.get("type") == "section":
                                plan_text = block.get("text", {}).get("text", "")
                                break
                        await slack.resolve_plan_message(channel, ts, plan_id, plan_text, True, "", user)
                    pending.resolve(plan_id, True, reason="")
                    return Response(content="", status_code=200)

                plan_id = value
                approved = action_id == "plan_approve"
                plan_text = ""
                for block in payload.get("message", {}).get("blocks", []):
                    if block.get("type") == "section":
                        plan_text = block.get("text", {}).get("text", "")
                        break

                channel = payload.get("channel", {}).get("id", "")
                ts = payload.get("message", {}).get("ts", "")
                user = payload.get("user", {}).get("name", "slack")

                if approved:
                    if channel and ts:
                        await slack.resolve_plan_message(channel, ts, plan_id, plan_text, True, "", user)
                    pending.resolve(plan_id, True, reason="")
                else:
                    if channel and ts:
                        _message_cache[plan_id] = (channel, ts, plan_text)
                    trigger_id = payload.get("trigger_id", "")
                    modal = slack._approval_modal(plan_id, plan_text, approved)
                    await slack.open_modal(trigger_id, modal)

            return Response(content="", status_code=200)

        if interaction_type == "view_submission":
            metadata = json.loads(payload.get("view", {}).get("private_metadata", "{}"))
            plan_id = metadata.get("plan_id", "")
            approved = metadata.get("approved", False)
            context = (
                payload
                .get("view", {})
                .get("state", {})
                .get("values", {})
                .get("context_block", {})
                .get("context_input", {})
                .get("value") or ""
            )
            user = payload.get("user", {}).get("name", "slack")
            if approved:
                reason = ""
            elif context:
                reason = context
            else:
                reason = f"slack:denied by {user}"

            if plan_id in _message_cache:
                channel, ts, plan_text = _message_cache.pop(plan_id)
                await slack.resolve_plan_message(channel, ts, plan_id, plan_text, approved, context, user)

            pending.resolve(plan_id, approved, reason=reason)
            return Response(content="", status_code=200)

        return Response(content="", status_code=200)

    return app


# ---------------------------------------------------------------------------
# HomelabAgent (OpenAI SDK)
# ---------------------------------------------------------------------------

class HomelabAgent:
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._model: str = config.model.name
        self._host: str = config.model.base_url
        self._client = ollama.AsyncClient(host=config.model.base_url)
        self._input_cost_per_mtok: float = config.model.input_cost_per_mtok
        self._output_cost_per_mtok: float = config.model.output_cost_per_mtok

        self._slack = SlackClient(
            bot_token=config.slack.bot_token,
            signing_secret=config.slack.signing_secret,
            channel=config.slack.channel,
        )
        self._veto_window: int = config.slack.veto_window_seconds

        self._logger = ActionLogger(config.action_log.path)
        self._safety = SafetyPolicy(config)
        self._rag: IncidentRAG | None = (
            IncidentRAG(config.rag) if config.rag.dsn else None
        )
        self._tools = ToolExecutor(config, self._slack, rag=self._rag)
        self._pending = PendingApprovals()

        self._history_path = Path(config.history.path)
        self._history: list[dict] = self._load_history()
        self._last_cost_breakdown: str = ""
        self._zar_rate: float | None = None
        self._zar_rate_fetched_at: datetime | None = None
        self._system_prompt = build_system_prompt()
        self._active_execution: dict | None = None
        self._active_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(self, message: str, trigger: str = "cli:user_message") -> tuple[str, float]:
        self._history.append({"role": "user", "content": message})
        self._trim_history()
        return await self._run_loop(trigger)

    async def handle_event(self, event: dict) -> tuple[str, float]:
        source = event.get("source", "unknown")
        etype = event.get("type", "")
        data = event.get("data", {})

        if etype == "services_down":
            services = data.get("services", [])
            lines = []
            for s in services:
                lines.append(
                    f"  - {s['service']}: {s['running']}/{s['desired']} replicas"
                    + (f" (error: {s['last_error']})" if s.get("last_error") else "")
                )
            svc_list = "\n".join(lines)
            msg = (
                f"[MONITOR ALERT] {len(services)} service(s) are degraded:\n{svc_list}\n"
                "Investigate the common root cause and take appropriate action per your autonomy tier rules."
            )
            trigger = "monitor:services_down"
        elif etype == "service_down":
            svc = data["service"]
            running = data["running"]
            desired = data["desired"]
            err = data.get("last_error", "none")
            msg = (
                f"[MONITOR ALERT] Service {svc} is degraded: {running}/{desired} replicas running. "
                f"Last error: {err}. Investigate and take appropriate action per your autonomy tier rules."
            )
            trigger = "monitor:service_down"
        elif etype == "service_recovered":
            svc = data["service"]
            dur = data.get("down_duration_seconds", 0)
            msg = f"[MONITOR] Service {svc} has recovered after {dur}s. Notify Slack."
            trigger = "monitor:service_recovered"
        else:
            msg = str(data)
            trigger = f"{source}:{etype}"

        return await self.chat(msg, trigger=trigger)

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    async def _api_create(self) -> Any:
        """Call Ollama chat with exponential backoff on errors."""
        messages = [{"role": "system", "content": self._system_prompt}] + self._history

        logger.debug(
            "API REQUEST model=%s host=%s history_turns=%d\nMESSAGES:\n%s\nTOOLS:\n%s",
            self._model,
            self._host,
            len(self._history),
            json.dumps(messages, indent=2),
            json.dumps(TOOL_DEFINITIONS, indent=2),
        )

        delay = 5
        for attempt in range(5):
            try:
                response = await self._client.chat(
                    model=self._model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    think=False,
                    stream=False,
                )
                logger.debug(
                    "API RESPONSE done_reason=%s tokens=(%d in, %d out)\nMESSAGE:\n%s",
                    response.done_reason,
                    response.prompt_eval_count or 0,
                    response.eval_count or 0,
                    json.dumps(response.message.model_dump(), indent=2),
                )
                return response
            except ollama.ResponseError as exc:
                console.print(f"  [bold red]Ollama ResponseError: status={exc.status_code} error={exc.error!r}[/bold red]")
                if attempt < 4:
                    console.print(f"  [dim]Retrying in {delay}s… (attempt {attempt + 1}/5)[/dim]")
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    raise

    async def _run_loop(self, trigger: str) -> tuple[str, float]:
        final_text = ""
        total_input_tokens = 0
        total_output_tokens = 0
        live_to_slack = not trigger.startswith("cli:")

        for iteration in range(MAX_ITERATIONS):
            response = await self._api_create()
            message = response.message
            done_reason = response.done_reason

            total_input_tokens += response.prompt_eval_count or 0
            total_output_tokens += response.eval_count or 0

            # Build assistant history entry (Ollama native format)
            assistant_entry: dict = {"role": "assistant", "content": message.content or ""}
            if message.tool_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    }
                    for tc in message.tool_calls
                ]
            self._history.append(assistant_entry)
            self._trim_history()

            # Print text content if present
            if message.content:
                final_text = message.content
                label = Text("Agent: ", style="bold cyan")
                console.print(label, end="")
                console.print(message.content)
                if live_to_slack and message.content.strip():
                    if done_reason == "stop":
                        slack_text = f"✅ {message.content}"
                    elif iteration == 0:
                        slack_text = f"📋 {message.content}"
                    else:
                        slack_text = f"🔍 {message.content}"
                    console.print(f"  [dim cyan]→ Slack notify ({len(slack_text)} chars)[/dim cyan]")
                    try:
                        await self._slack.notify(slack_text)
                    except Exception as exc:
                        console.print(f"  [yellow]Slack notify failed: {exc}[/yellow]")

            if done_reason == "stop":
                break

            if done_reason == "tool_calls" and message.tool_calls:
                tool_calls = [
                    _ToolCall(
                        id=str(i),
                        name=tc.function.name,
                        input=tc.function.arguments,  # Already a dict in native API
                    )
                    for i, tc in enumerate(message.tool_calls)
                ]
                try:
                    tool_results = await self._handle_tool_calls(tool_calls, trigger)
                except Exception as exc:
                    tool_results = [
                        {"role": "tool", "content": f"ERROR: {exc}"}
                        for _ in tool_calls
                    ]
                for result_msg in tool_results:
                    self._history.append(result_msg)
                self._trim_history()

        input_cost = total_input_tokens / 1_000_000 * self._input_cost_per_mtok
        output_cost = total_output_tokens / 1_000_000 * self._output_cost_per_mtok
        cost_usd = input_cost + output_cost

        zar = await self._get_zar_rate()

        def fmt(usd: float, tokens: int) -> str:
            s = f"${usd:.5f}"
            if zar:
                s += f"/R{usd * zar:.4f}"
            return f"{s}({tokens:,})"

        parts = [f"in={fmt(input_cost, total_input_tokens)}", f"out={fmt(output_cost, total_output_tokens)}"]
        total_str = f"${cost_usd:.5f}"
        if zar:
            total_str += f"/R{cost_usd * zar:.4f}"
        parts.append(f"total={total_str}")
        breakdown = "  " + "  ".join(parts)
        console.print(f"[dim]{breakdown}[/dim]")

        self._last_cost_breakdown = breakdown
        await self._logger.log_cost(cost_usd, total_input_tokens, total_output_tokens, trigger)
        self._save_history()
        return ("" if live_to_slack else final_text), cost_usd

    async def _handle_tool_calls(
        self,
        tool_calls: list[_ToolCall],
        trigger: str,
    ) -> list[dict]:
        tier1_calls: list[_ToolCall] = []
        mutating_calls: list[_ToolCall] = []
        resolved_map: dict[str, Any] = {}

        for tc in tool_calls:
            inp = tc.input
            agent_tier = inp.get("agent_proposed_tier")
            agent_reason = inp.get("agent_reasoning")
            target = self._infer_target_resource(tc.name, inp)

            resolved = self._safety.resolve_tier(
                tc.name,
                target,
                agent_tier,
                agent_reason,
                command=inp.get("command") if tc.name == "run_shell" else None,
            )
            resolved_map[tc.id] = resolved

            if (
                agent_tier is not None
                and self._safety.log_agent_tier_reasoning
            ):
                await self._logger.log_tier_reasoning(
                    tool=tc.name,
                    agent_proposed_tier=agent_tier,
                    reasoning=agent_reason or "",
                    safe_mode_active=resolved.safe_mode_active,
                    effective_tier=resolved.tier,
                    override_reason=resolved.override_reason,
                    guard_matched_list=resolved.guard_matched_list,
                    guard_matched_pattern=resolved.guard_matched_pattern,
                )

            if resolved.tier == 1:
                tier1_calls.append(tc)
            else:
                mutating_calls.append(tc)

        results: dict[str, str] = {}

        if tier1_calls:
            async def _exec_tier1(tc: _ToolCall) -> tuple[str, str]:
                self._print_tool_call(tc, resolved_map[tc.id])
                res = await self._tools.execute(tc.name, tc.input)
                await self._logger.log_action_taken(
                    tool=tc.name,
                    tool_input=tc.input,
                    outcome=res,
                    tier=resolved_map[tc.id].tier,
                    safe_mode_active=resolved_map[tc.id].safe_mode_active,
                    trigger=trigger,
                )
                return tc.id, res

            gathered = await asyncio.gather(*[_exec_tier1(tc) for tc in tier1_calls])
            for tid, res in gathered:
                results[tid] = res

        for tc in mutating_calls:
            resolved = resolved_map[tc.id]
            self._print_tool_call(tc, resolved)
            res = await self._handle_approval_flow(tc, resolved, trigger)
            results[tc.id] = res

        for tc in tool_calls:
            res = results.get(tc.id, "ERROR: result missing")
            if res.startswith("ERROR:"):
                console.print(f"\n  [bold red]Tool error ({tc.name}):[/bold red] {res}")
                console.print("  [dim]Waiting for agent to report and ask for instructions...[/dim]\n")

        # Return one tool message per call (Ollama native format)
        return [
            {
                "role": "tool",
                "content": results.get(tc.id, "ERROR: result missing"),
            }
            for tc in tool_calls
        ]

    async def _handle_approval_flow(
        self,
        tc: _ToolCall,
        resolved: Any,
        trigger: str,
    ) -> str:
        plan_id = f"plan-{secrets.token_hex(4)}"
        tool_input = tc.input
        plan_text = self._format_plan(tc.name, tool_input)
        veto_seconds = self._veto_window if resolved.tier == 2 else None

        message_ref = await self._slack.notify_plan(
            plan_id,
            plan_text,
            veto_seconds,
            tool_name=tc.name,
            command=tool_input.get("command", ""),
        )
        await self._logger.log_plan_proposed(
            plan_id=plan_id,
            tool=tc.name,
            tool_input=tool_input,
            plan_text=plan_text,
            tier=resolved.tier,
            safe_mode_active=resolved.safe_mode_active,
            trigger=trigger,
        )

        console.print(f"\n  [bold yellow]Plan ID:[/bold yellow] {plan_id}")
        console.print(f"  [yellow]{plan_text}[/yellow]")
        if veto_seconds is not None:
            console.print(f"  Type [bold]y[/bold] to approve, [bold]n[/bold] to deny, or a message to cancel with context (auto-cancels in {veto_seconds}s)")
        else:
            console.print(f"  Type [bold]y[/bold] to approve, [bold]n[/bold] to deny, or a message to cancel with context")

        fut = self._pending.register(plan_id, tc.name, plan_text, resolved.tier)
        approved: bool
        reason: str

        try:
            if veto_seconds is not None:
                approved, reason = await asyncio.wait_for(asyncio.shield(fut), timeout=veto_seconds)
            else:
                approved, reason = await fut
        except asyncio.TimeoutError:
            approved = False
            reason = "timeout"
            self._pending.resolve(plan_id, False, "timeout")

        if not approved:
            await self._logger.log_plan_cancelled(plan_id, tc.name, reason)
            detail = f" — user said: {reason}" if reason and not reason.startswith("slack:") and reason != "timeout" else ""
            return f"[cancelled: {reason}{detail}]"

        await self._logger.log_plan_approved(plan_id, tc.name)
        self._active_execution = {
            "plan_id": plan_id,
            "tool": tc.name,
            "input": {k: v for k, v in tool_input.items() if k not in ("agent_proposed_tier", "agent_reasoning")},
            "started_at": datetime.now(timezone.utc),
        }
        try:
            result = await self._tools.execute(tc.name, tool_input)
        finally:
            self._active_execution = None
        await self._logger.log_action_taken(
            tool=tc.name,
            tool_input=tool_input,
            outcome=result,
            tier=resolved.tier,
            safe_mode_active=resolved.safe_mode_active,
            trigger=trigger,
        )
        if message_ref:
            await self._slack.update_plan_result(*message_ref, plan_id, plan_text, result)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _infer_target_resource(self, tool_name: str, inp: dict) -> str | None:
        for key in ("service_name", "stack_name", "node"):
            if key in inp:
                return inp[key]
        return None

    def _format_plan(self, tool_name: str, tool_input: dict) -> str:
        def _truncate(v: object, limit: int = 300) -> str:
            s = str(v)
            return s if len(s) <= limit else s[:limit] + "…(truncated)"

        inp_lines = "\n".join(f"  {k}: {_truncate(v)}" for k, v in tool_input.items()
                              if k not in ("agent_proposed_tier", "agent_reasoning"))
        return f"*Tool:* `{tool_name}`\n*Inputs:*\n{inp_lines}"

    def _print_tool_call(self, tc: _ToolCall, resolved: Any) -> None:
        inp = tc.input
        params = ", ".join(
            f"{k}={v}" for k, v in inp.items()
            if k not in ("agent_proposed_tier", "agent_reasoning")
        )
        console.print(f"  [yellow]> {tc.name}({params})[/yellow]")
        if resolved.safe_mode_active:
            original = f"would have been tier {resolved.original_tier}" if resolved.original_tier is not None else "original tier unknown"
            console.print(f"  [bold yellow]  [SAFE MODE — tier forced to 3, {original}][/bold yellow]")
        if resolved.agent_reasoning:
            console.print(f"  [dim italic]  tier reasoning: {resolved.agent_reasoning}[/dim italic]")

    async def _get_zar_rate(self) -> float | None:
        now = datetime.now(timezone.utc)
        if self._zar_rate is not None and self._zar_rate_fetched_at is not None:
            age = (now - self._zar_rate_fetched_at).total_seconds()
            if age < 3600:
                return self._zar_rate
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get("https://api.frankfurter.app/latest?from=USD&to=ZAR")
                self._zar_rate = resp.json()["rates"]["ZAR"]
                self._zar_rate_fetched_at = now
        except Exception:
            pass
        return self._zar_rate

    def _load_history(self) -> list[dict]:
        if self._history_path.exists():
            try:
                return json.loads(self._history_path.read_text())
            except Exception as e:
                console.print(f"[yellow]Warning: could not load history: {e}[/yellow]")
        return []

    def _save_history(self) -> None:
        self._history_path.write_text(json.dumps(self._history, indent=2))

    def _trim_history(self) -> None:
        """Keep at most MAX_HISTORY_TURNS turn-pairs, removing orphaned tool messages."""
        max_entries = MAX_HISTORY_TURNS * 2
        if len(self._history) > max_entries:
            self._history = self._history[-max_entries:]

        # Remove leading messages that would form an incomplete tool call group
        while self._history:
            first = self._history[0]
            role = first.get("role")

            # Orphaned tool response with no preceding assistant+tool_calls
            if role == "tool":
                self._history.pop(0)
                continue

            # Assistant message with tool_calls whose responses were trimmed off
            if role == "assistant" and first.get("tool_calls"):
                n_calls = len(first["tool_calls"])
                n_responses = sum(
                    1 for m in self._history[1:n_calls + 1]
                    if m.get("role") == "tool"
                )
                if n_responses < n_calls:
                    self._history.pop(0)
                    continue

            break

    # ------------------------------------------------------------------
    # Approval listener lifecycle
    # ------------------------------------------------------------------

    async def start_approval_listener(
        self,
        host: str,
        port: int,
        event_queue: asyncio.Queue | None = None,
        controller: Any = None,
    ) -> tuple[asyncio.Task, uvicorn.Server]:
        host = _resolve_listener_host(host, self._slack.signature_verification_enabled)
        app = build_approval_app(self._pending, self._slack, event_queue, controller)
        server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(server_config)

        async def _serve() -> None:
            await server.serve()

        task = asyncio.create_task(_serve())
        return task, server

    async def cancel_all(self) -> None:
        self._pending.cancel_all("emergency stop")
        if self._active_task is not None:
            self._active_task.cancel()

    async def aclose(self) -> None:
        await self._slack.aclose()
