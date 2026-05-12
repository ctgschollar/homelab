import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

logger = logging.getLogger("homelab.agent")

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from rich.console import Console
from rich.text import Text

from .config_schema import AgentConfig, LlmConfig
from .hints import HintEngine
from .llm import LLMBackend, LLMResponse, ToolCall, create_backend
from .prompts import build_system_prompt
from .rag import IncidentRAG
from .safety import SafetyPolicy
from .slack import SlackClient
from .tools import TOOL_DEFINITIONS, ToolExecutor

if TYPE_CHECKING:
    from .config_schema import ModelEntry

MAX_ITERATIONS = 15
MAX_HISTORY_TURNS = 20  # trim when history exceeds this many turn-pairs

console = Console()


def _resolve_listener_host(host: str, signing_secret_configured: bool) -> str:
    if not signing_secret_configured and host == "0.0.0.0":
        console.print("[bold red]WARNING: Slack signing secret not configured — approval listener restricted to localhost[/bold red]")
        return "127.0.0.1"
    return host


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
        # future result is (approved: bool, reason: str)
        self._futures: dict[str, asyncio.Future] = {}
        self._meta: dict[str, dict] = {}  # plan_id -> {tool, plan_text, tier, proposed_at}

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
        """Cancel all pending plans. Returns the cancelled plan IDs."""
        ids = list(self._futures.keys())
        for plan_id in ids:
            self.resolve(plan_id, False, reason)
        return ids

    def known_ids(self) -> list[str]:
        return list(self._futures.keys())

    def all_plans(self) -> list[dict]:
        """Return metadata for all pending plans, oldest first."""
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
) -> FastAPI:  # type: ignore[name-defined]  # noqa: F821
    app = FastAPI()

    # plan_id -> (channel, ts, plan_text) so we can update the message after resolution
    _message_cache: dict[str, tuple[str, str, str]] = {}

    @app.post("/slack/events")
    async def slack_events(request: Request) -> Response:
        raw_body = await request.body()

        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if slack.signature_verification_enabled and not slack.verify_signature(timestamp, raw_body, signature):
            return Response(content="Invalid signature", status_code=403)

        body = json.loads(raw_body)

        # Slack sends this once when you register the endpoint
        if body.get("type") == "url_verification":
            return Response(content=json.dumps({"challenge": body["challenge"]}), media_type="application/json")

        if body.get("type") == "event_callback":
            event = body.get("event", {})
            if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
                text = event.get("text", "").strip()
                if text and event_queue is not None:
                    if controller is not None and controller.is_command(text):
                        async def _run_command(t: str = text) -> None:
                            result = await controller.handle_command(t)
                            await slack.notify(result)
                        asyncio.create_task(_run_command())
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

        # Verify Slack signature
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        console.print(f"  [dim]Slack interaction received — timestamp={timestamp!r} sig={signature[:20]!r}…[/dim]")

        if slack.signature_verification_enabled and not slack.verify_signature(timestamp, raw_body, signature):
            console.print("  [bold red]Slack signature verification failed[/bold red]")
            return Response(content="Invalid signature", status_code=403)

        # Interactions arrive as application/x-www-form-urlencoded with a `payload` field
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
        interaction_type = payload.get("type")
        console.print(f"  [dim]Slack interaction type: {interaction_type!r}[/dim]")

        # ---- Button click: open confirmation modal -----------------------
        if interaction_type == "block_actions":
            for action in payload.get("actions", []):
                action_id = action.get("action_id")
                value = action.get("value", "")

                # --- Deferred alert buttons ---
                if action_id == "alert_start":
                    if controller is not None:
                        await controller.start_alert(value)
                    return Response(content="", status_code=200)

                if action_id == "alert_ignore":
                    if controller is not None:
                        await controller.ignore_alert(value)
                    return Response(content="", status_code=200)

                # --- Plan approval buttons ---
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

                # Find the plan_text from the original message for the modal
                plan_text = ""
                for block in payload.get("message", {}).get("blocks", []):
                    if block.get("type") == "section":
                        plan_text = block.get("text", {}).get("text", "")
                        break

                channel = payload.get("channel", {}).get("id", "")
                ts = payload.get("message", {}).get("ts", "")
                user = payload.get("user", {}).get("name", "slack")

                if approved:
                    # Approve immediately — no modal needed
                    if channel and ts:
                        await slack.resolve_plan_message(channel, ts, plan_id, plan_text, True, "", user)
                    pending.resolve(plan_id, True, reason="")
                else:
                    # Deny — open modal so user can optionally add context
                    if channel and ts:
                        _message_cache[plan_id] = (channel, ts, plan_text)
                    trigger_id = payload.get("trigger_id", "")
                    modal = slack._approval_modal(plan_id, plan_text, approved)
                    await slack.open_modal(trigger_id, modal)

            return Response(content="", status_code=200)

        # ---- Modal submission: resolve plan ------------------------------
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
                # Deny with context: cancel and re-run so agent sees the context
                reason = context
            else:
                # Deny without context: stop outright
                reason = f"slack:denied by {user}"

            # Update the original Slack message before resolving the future,
            # so the HTTP call completes before the caller can cancel the server.
            if plan_id in _message_cache:
                channel, ts, plan_text = _message_cache.pop(plan_id)
                await slack.resolve_plan_message(channel, ts, plan_id, plan_text, approved, context, user)

            found = pending.resolve(plan_id, approved, reason=reason)

            # Returning None closes the modal with no error
            return Response(content="", status_code=200)

        return Response(content="", status_code=200)

    return app


# ---------------------------------------------------------------------------
# HomelabAgent
# ---------------------------------------------------------------------------

class HomelabAgent:
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._backend: LLMBackend = create_backend(config.llm)
        self._model: str = config.llm.model
        self._input_cost_per_mtok: float = config.llm.input_cost_per_mtok
        self._output_cost_per_mtok: float = config.llm.output_cost_per_mtok
        self._hints = HintEngine(getattr(config, "hints_dir", "./hints"))

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
        self._summarize_threshold: int = config.llm.num_ctx * 3 // 4
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
        """Process a single user message through the agentic loop. Returns (text, cost_usd)."""
        self._history.append({"role": "user", "content": message})
        self._trim_history()
        return await self._run_loop(trigger)

    async def handle_event(self, event: dict) -> tuple[str, float]:
        """Convert a queue event into a user message and run the loop. Returns (text, cost_usd)."""
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
            # Legacy single-service event (kept for safety)
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

    async def _run_loop(self, trigger: str) -> tuple[str, float]:
        final_text = ""
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_write_tokens = 0
        total_cache_read_tokens = 0
        live_to_slack = not trigger.startswith("cli:")

        for iteration in range(MAX_ITERATIONS):
            response = await self._backend.chat(self._system_prompt, self._history, TOOL_DEFINITIONS)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cache_write_tokens += response.cache_write_tokens
            total_cache_read_tokens += response.cache_read_tokens
            self._history.append(response.assistant_history_entry)
            self._trim_history()

            if response.input_tokens >= self._summarize_threshold:
                logger.debug(
                    "auto-summarize triggered: input_tokens=%d threshold=%d",
                    response.input_tokens, self._summarize_threshold,
                )
                await self._summarize_history()

            if response.text:
                final_text = response.text
                label = Text("Agent: ", style="bold cyan")
                console.print(label, end="")
                console.print(response.text)
                if live_to_slack and response.text.strip():
                    if response.stop:
                        slack_text = f"✅ {response.text}"
                    elif iteration == 0:
                        slack_text = f"📋 {response.text}"
                    else:
                        slack_text = f"🔍 {response.text}"
                    console.print(f"  [dim cyan]→ Slack notify ({len(slack_text)} chars)[/dim cyan]")
                    try:
                        await self._slack.notify(slack_text)
                    except Exception as exc:
                        console.print(f"  [yellow]Slack notify failed: {exc}[/yellow]")

            if response.stop:
                break

            try:
                results = await self._handle_tool_calls(response.tool_calls, trigger)
            except Exception as exc:
                results = [(tc.id, f"ERROR: {exc}") for tc in response.tool_calls]
            for msg in self._backend.format_tool_results(results):
                self._history.append(msg)
            self._trim_history()

        input_cost   = total_input_tokens       / 1_000_000 * self._input_cost_per_mtok
        write_cost   = total_cache_write_tokens  / 1_000_000 * self._input_cost_per_mtok * 1.25
        read_cost    = total_cache_read_tokens   / 1_000_000 * self._input_cost_per_mtok * 0.10
        output_cost  = total_output_tokens       / 1_000_000 * self._output_cost_per_mtok
        cost_usd     = input_cost + write_cost + read_cost + output_cost

        zar = await self._get_zar_rate()

        def fmt(usd: float, tokens: int) -> str:
            s = f"${usd:.5f}"
            if zar:
                s += f"/R{usd * zar:.4f}"
            return f"{s}({tokens:,})"

        parts = [f"in={fmt(input_cost, total_input_tokens)}"]
        if total_cache_write_tokens:
            parts.append(f"cW={fmt(write_cost, total_cache_write_tokens)}")
        if total_cache_read_tokens:
            parts.append(f"cR={fmt(read_cost, total_cache_read_tokens)}")
        parts.append(f"out={fmt(output_cost, total_output_tokens)}")
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
        tool_calls: list[ToolCall],
        trigger: str,
    ) -> list[tuple[str, str]]:
        tier1_calls: list[ToolCall] = []
        mutating_calls: list[ToolCall] = []
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

            if agent_tier is not None and self._safety.log_agent_tier_reasoning:
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
            async def _exec_tier1(tc: ToolCall) -> tuple[str, str]:
                self._print_tool_call(tc, resolved_map[tc.id])
                res = await self._tools.execute(tc.name, tc.input)
                res = self._hints.enrich(tc.name, res)
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

        return [(tc.id, results.get(tc.id, "ERROR: result missing")) for tc in tool_calls]

    async def _handle_approval_flow(
        self,
        tc: ToolCall,
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
            result = self._hints.enrich(tc.name, result)
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
        """Best-effort extraction of the primary resource target from tool input."""
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

    def _print_tool_call(self, tc: ToolCall, resolved: Any) -> None:
        params = ", ".join(
            f"{k}={v}" for k, v in tc.input.items()
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
        serialized = [self._backend.serialize_message(msg) for msg in self._history]
        self._history_path.write_text(json.dumps(serialized, indent=2))

    def _trim_history(self) -> None:
        """Keep at most MAX_HISTORY_TURNS turn-pairs, never leaving orphaned tool messages."""
        max_entries = MAX_HISTORY_TURNS * 2
        if len(self._history) > max_entries:
            self._history = self._history[-max_entries:]

        while self._history:
            first = self._history[0]
            if self._backend.is_orphaned_tool_result(first):
                self._history.pop(0)
                continue
            if self._backend.has_incomplete_tool_calls(first, self._history[1:]):
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

    def clear_history(self) -> None:
        self._history = []
        if self._history_path.exists():
            self._history_path.unlink()

    async def get_summary(self) -> str:
        logger.debug("get_summary: history has %d messages", len(self._history))
        if not self._history:
            return ""
        return await self._call_summary(self._history)

    async def _summarize_history(self) -> str:
        if not self._history:
            return ""
        keep = 3  # last 3 messages verbatim
        to_summarize = self._history[:-keep] if len(self._history) > keep else self._history
        recent = self._history[-keep:] if len(self._history) > keep else []
        try:
            summary = await self._call_summary(to_summarize)
        except Exception as exc:
            logger.error("Failed to summarize history: %s", exc)
            return ""
        self._history = [
            {"role": "user", "content": "[Earlier conversation summary — use this as context]"},
            {"role": "assistant", "content": summary},
        ] + recent
        try:
            await self._slack.notify(f"📋 *History summarized* (context filling up):\n{summary}")
        except Exception as exc:
            logger.warning("Failed to notify Slack of summary: %s", exc)
        return summary

    @staticmethod
    def _flatten_for_summary(messages: list[dict]) -> list[dict]:
        """Convert tool call/result messages to plain text so the model summarizes instead of calling tools."""
        result = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "tool":
                result.append({"role": "user", "content": f"[Tool output]: {content}"})
            elif role == "assistant" and m.get("tool_calls"):
                names = [tc["function"]["name"] for tc in m.get("tool_calls", [])]
                text = content or f"[Called: {', '.join(names)}]"
                result.append({"role": "assistant", "content": text})
            elif isinstance(content, str) and content.strip():
                result.append({"role": role, "content": content})
        return result

    async def _call_summary(self, messages: list[dict]) -> str:
        logger.debug("_call_summary: input %d messages", len(messages))
        summary_system = (
            "Summarize this infrastructure troubleshooting conversation in 150 words or fewer. "
            "Cover: what alert or question triggered the investigation, key findings "
            "(errors, service states, commands run), actions taken or proposed, and "
            "any unresolved issues. Include specific service names and error messages. "
            "Be terse — this summary replaces the conversation in context, so facts matter more than prose."
        )
        flat = self._flatten_for_summary(messages)
        logger.debug("_call_summary: flattened to %d messages", len(flat))
        if not flat:
            logger.debug("_call_summary: nothing to summarize after flattening")
            return ""
        response = await self._backend.chat(summary_system, flat, [])
        logger.debug("_call_summary: response text length=%d", len(response.text))
        return response.text.strip()

    def switch_backend(self, entry: "ModelEntry") -> None:
        """Switch to a different LLM backend, preserving text-only history.

        Tool call/result messages are stripped because Anthropic and Ollama use
        incompatible formats for them. Plain assistant/user text is kept so the
        new model has context for the ongoing investigation.
        """
        new_config = LlmConfig(
            provider=entry.provider,
            model=entry.name,
            base_url=entry.base_url,
            api_key=entry.api_key,
            input_cost_per_mtok=entry.input_cost_per_mtok,
            output_cost_per_mtok=entry.output_cost_per_mtok,
            num_ctx=entry.num_ctx,
            available_models=self._config.llm.available_models,
        )
        self._backend = create_backend(new_config)
        self._model = entry.name
        self._input_cost_per_mtok = entry.input_cost_per_mtok
        self._output_cost_per_mtok = entry.output_cost_per_mtok
        self._history = [m for m in self._history if self._is_plain_text(m)]

    @staticmethod
    def _is_plain_text(msg: dict) -> bool:
        """Return True if msg is a plain text user/assistant message with no tool data."""
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return False
        if msg.get("tool_calls"):
            return False
        content = msg.get("content", "")
        if isinstance(content, list):
            return False
        return bool(content)

    def update_num_ctx(self, num_ctx: int) -> None:
        """Update context window size without clearing history."""
        if hasattr(self._backend, "_num_ctx"):
            self._backend._num_ctx = num_ctx  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        await self._slack.aclose()
