import asyncio
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any

import anthropic
import uvicorn
from fastapi import FastAPI, Request, Response
from rich.console import Console
from rich.text import Text

from .prompts import build_system_prompt
from .safety import SafetyPolicy
from .slack import SlackClient
from .tools import TOOL_DEFINITIONS, ToolExecutor

MAX_ITERATIONS = 15
MAX_HISTORY_TURNS = 20  # trim when history exceeds this many turn-pairs

console = Console()


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
    ) -> None:
        await self.log({
            "event": "tier_reasoning",
            "tool": tool,
            "agent_proposed_tier": agent_proposed_tier,
            "reasoning": reasoning,
            "safe_mode_active": safe_mode_active,
            "effective_tier": effective_tier,
        })

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
) -> FastAPI:  # type: ignore[name-defined]  # noqa: F821
    app = FastAPI()

    # plan_id -> (channel, ts, plan_text) so we can update the message after resolution
    _message_cache: dict[str, tuple[str, str, str]] = {}

    @app.post("/slack/events")
    async def slack_events(request: Request) -> Response:
        raw_body = await request.body()

        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if slack.configured and not slack.verify_signature(timestamp, raw_body, signature):
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

        if slack.configured and not slack.verify_signature(timestamp, raw_body, signature):
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
                plan_id = action.get("value", "")
                if action_id not in ("plan_approve", "plan_deny"):
                    continue

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
    def __init__(self, config: dict) -> None:
        self._config = config
        anthropic_cfg = config.get("anthropic", {})
        self._model: str = anthropic_cfg.get("model", "claude-sonnet-4-20250514")
        self._client = anthropic.AsyncAnthropic(api_key=anthropic_cfg.get("api_key", ""))
        self._input_cost_per_mtok: float = anthropic_cfg.get("input_cost_per_mtok", 3.0)
        self._output_cost_per_mtok: float = anthropic_cfg.get("output_cost_per_mtok", 15.0)

        slack_cfg = config.get("slack", {})
        self._slack = SlackClient(
            bot_token=slack_cfg.get("bot_token", ""),
            signing_secret=slack_cfg.get("signing_secret", ""),
            channel=slack_cfg.get("channel", "#homelab-alerts"),
        )
        self._veto_window: int = slack_cfg.get("veto_window_seconds", 300)

        log_path = config.get("action_log", {}).get("path", "./action.log")
        self._logger = ActionLogger(log_path)
        self._safety = SafetyPolicy(config)
        self._tools = ToolExecutor(config, self._slack)
        self._pending = PendingApprovals()

        self._history: list[dict] = []
        self._system_prompt = build_system_prompt()
        self._active_execution: dict | None = None  # set while a tool is executing

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

        if etype == "service_down":
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
        """Call messages.create with exponential backoff on overloaded (529) errors."""
        delay = 5
        for attempt in range(5):
            try:
                return await self._client.messages.create(
                    model=self._model,
                    max_tokens=4096,
                    system=self._system_prompt,
                    messages=self._history,
                    tools=TOOL_DEFINITIONS,
                )
            except anthropic.APIStatusError as exc:
                if exc.status_code == 529 and attempt < 4:
                    console.print(f"  [dim]API overloaded, retrying in {delay}s…[/dim]")
                    await self._slack.notify(f"⏳ Anthropic API overloaded — retrying in {delay}s…")
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    raise

    async def _run_loop(self, trigger: str) -> tuple[str, float]:
        final_text = ""
        total_input_tokens = 0
        total_output_tokens = 0

        for _ in range(MAX_ITERATIONS):
            response = await self._api_create()
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            self._history.append({"role": "assistant", "content": response.content})
            self._trim_history()

            # Print text blocks
            for block in response.content:
                if block.type == "text":
                    final_text = block.text
                    label = Text("Agent: ", style="bold cyan")
                    console.print(label, end="")
                    console.print(block.text)

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                try:
                    tool_results = await self._handle_tool_calls(response.content, trigger)
                except Exception as exc:
                    # Always append error tool_results to keep history valid.
                    # An unmatched tool_use block corrupts all subsequent API calls.
                    # Continuing the loop lets the agent see the error and respond.
                    tool_results = [
                        {"type": "tool_result", "tool_use_id": b.id, "content": f"ERROR: {exc}"}
                        for b in response.content
                        if hasattr(b, "type") and b.type == "tool_use"
                    ]
                self._history.append({"role": "user", "content": tool_results})
                self._trim_history()

        cost_usd = (
            total_input_tokens / 1_000_000 * self._input_cost_per_mtok
            + total_output_tokens / 1_000_000 * self._output_cost_per_mtok
        )
        console.print(
            f"  [dim]Cost: ${cost_usd:.4f} "
            f"({total_input_tokens:,}↑ {total_output_tokens:,}↓)[/dim]"
        )
        await self._logger.log_cost(cost_usd, total_input_tokens, total_output_tokens, trigger)
        return final_text, cost_usd

    async def _handle_tool_calls(
        self,
        blocks: list[Any],
        trigger: str,
    ) -> list[dict]:
        tool_use_blocks = [b for b in blocks if b.type == "tool_use"]

        # Separate tier-1 read-only calls from mutating calls
        tier1_blocks = []
        mutating_blocks = []
        resolved_map: dict[str, Any] = {}

        for block in tool_use_blocks:
            inp = block.input or {}
            agent_tier = inp.get("agent_proposed_tier")
            agent_reason = inp.get("agent_reasoning")
            target = self._infer_target_resource(block.name, inp)

            resolved = self._safety.resolve_tier(
                block.name, target, agent_tier, agent_reason
            )
            resolved_map[block.id] = resolved

            # Log tier reasoning for "agent"-discretion tools
            if (
                agent_tier is not None
                and self._safety.log_agent_tier_reasoning
            ):
                await self._logger.log_tier_reasoning(
                    tool=block.name,
                    agent_proposed_tier=agent_tier,
                    reasoning=agent_reason or "",
                    safe_mode_active=resolved.safe_mode_active,
                    effective_tier=resolved.tier,
                )

            if resolved.tier == 1:
                tier1_blocks.append(block)
            else:
                mutating_blocks.append(block)

        results: dict[str, str] = {}

        # Gather tier-1 calls concurrently
        if tier1_blocks:
            async def _exec_tier1(b: Any) -> tuple[str, str]:
                self._print_tool_call(b, resolved_map[b.id])
                res = await self._tools.execute(b.name, b.input or {})
                await self._logger.log_action_taken(
                    tool=b.name,
                    tool_input=b.input or {},
                    outcome=res,
                    tier=resolved_map[b.id].tier,
                    safe_mode_active=resolved_map[b.id].safe_mode_active,
                    trigger=trigger,
                )
                return b.id, res

            gathered = await asyncio.gather(*[_exec_tier1(b) for b in tier1_blocks])
            for bid, res in gathered:
                results[bid] = res

        # Sequential gated calls for tier 2/3
        for block in mutating_blocks:
            resolved = resolved_map[block.id]
            self._print_tool_call(block, resolved)
            res = await self._handle_approval_flow(block, resolved, trigger)
            results[block.id] = res

        # Print errors prominently before returning to the loop
        for b in tool_use_blocks:
            res = results.get(b.id, "ERROR: result missing")
            if res.startswith("ERROR:"):
                console.print(f"\n  [bold red]Tool error ({b.name}):[/bold red] {res}")
                console.print("  [dim]Waiting for agent to report and ask for instructions...[/dim]\n")

        # Reconstruct in original order
        return [
            {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": results.get(b.id, "ERROR: result missing"),
            }
            for b in tool_use_blocks
        ]

    async def _handle_approval_flow(
        self,
        block: Any,
        resolved: Any,
        trigger: str,
    ) -> str:
        plan_id = f"plan-{secrets.token_hex(2)}"
        tool_input = block.input or {}
        plan_text = self._format_plan(block.name, tool_input)
        veto_seconds = self._veto_window if resolved.tier == 2 else None

        message_ref = await self._slack.notify_plan(plan_id, plan_text, veto_seconds)
        await self._logger.log_plan_proposed(
            plan_id=plan_id,
            tool=block.name,
            tool_input=tool_input,
            plan_text=plan_text,
            tier=resolved.tier,
            safe_mode_active=resolved.safe_mode_active,
            trigger=trigger,
        )

        # Always show plan on terminal; when Slack is not configured this is
        # the only approval channel available.
        console.print(f"\n  [bold yellow]Plan ID:[/bold yellow] {plan_id}")
        console.print(f"  [yellow]{plan_text}[/yellow]")
        if veto_seconds is not None:
            console.print(f"  Type [bold]y[/bold] to approve, [bold]n[/bold] to deny, or a message to cancel with context (auto-cancels in {veto_seconds}s)")
        else:
            console.print(f"  Type [bold]y[/bold] to approve, [bold]n[/bold] to deny, or a message to cancel with context")

        fut = self._pending.register(plan_id, block.name, plan_text, resolved.tier)
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
            await self._logger.log_plan_cancelled(plan_id, block.name, reason)
            # Surface user's message to the agent so it can re-plan with context
            detail = f" — user said: {reason}" if reason and not reason.startswith("slack:") and reason != "timeout" else ""
            return f"[cancelled: {reason}{detail}]"

        await self._logger.log_plan_approved(plan_id, block.name)
        self._active_execution = {
            "plan_id": plan_id,
            "tool": block.name,
            "input": {k: v for k, v in tool_input.items() if k not in ("agent_proposed_tier", "agent_reasoning")},
            "started_at": datetime.now(timezone.utc),
        }
        try:
            result = await self._tools.execute(block.name, tool_input)
        finally:
            self._active_execution = None
        await self._logger.log_action_taken(
            tool=block.name,
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
        inp_lines = "\n".join(f"  {k}: {v}" for k, v in tool_input.items()
                              if k not in ("agent_proposed_tier", "agent_reasoning"))
        return f"*Tool:* `{tool_name}`\n*Inputs:*\n{inp_lines}"

    def _print_tool_call(self, block: Any, resolved: Any) -> None:
        inp = block.input or {}
        params = ", ".join(
            f"{k}={v}" for k, v in inp.items()
            if k not in ("agent_proposed_tier", "agent_reasoning")
        )
        console.print(f"  [yellow]> {block.name}({params})[/yellow]")
        if resolved.safe_mode_active:
            original = f"would have been tier {resolved.original_tier}" if resolved.original_tier is not None else "original tier unknown"
            console.print(f"  [bold yellow]  [SAFE MODE — tier forced to 3, {original}][/bold yellow]")
        if resolved.agent_reasoning:
            console.print(f"  [dim italic]  tier reasoning: {resolved.agent_reasoning}[/dim italic]")

    def _trim_history(self) -> None:
        """Keep at most MAX_HISTORY_TURNS turn-pairs, never splitting a tool_use/tool_result pair."""
        max_entries = MAX_HISTORY_TURNS * 2
        if len(self._history) > max_entries:
            self._history = self._history[-max_entries:]

        # Walk forward until the first message is not a tool_result user message.
        # Slicing by count can leave an orphaned tool_result whose tool_use was trimmed away,
        # which the API rejects with a 400.
        while self._history:
            first = self._history[0]
            content = first.get("content", [])
            if (
                first.get("role") == "user"
                and isinstance(content, list)
                and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
            ):
                self._history = self._history[1:]
            else:
                break

    # ------------------------------------------------------------------
    # Approval listener lifecycle
    # ------------------------------------------------------------------

    async def start_approval_listener(
        self,
        host: str,
        port: int,
        event_queue: asyncio.Queue | None = None,
    ) -> tuple[asyncio.Task, uvicorn.Server]:
        app = build_approval_app(self._pending, self._slack, event_queue)
        server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(server_config)

        async def _serve() -> None:
            await server.serve()

        task = asyncio.create_task(_serve())
        return task, server

    async def aclose(self) -> None:
        await self._slack.aclose()
