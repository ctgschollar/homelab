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


# ---------------------------------------------------------------------------
# Pending Approvals
# ---------------------------------------------------------------------------

class PendingApprovals:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    def register(self, plan_id: str) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._futures[plan_id] = fut
        return fut

    def resolve(self, plan_id: str, approved: bool) -> bool:
        fut = self._futures.pop(plan_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        return True

    def known_ids(self) -> list[str]:
        return list(self._futures.keys())


# ---------------------------------------------------------------------------
# Slack approval listener (FastAPI)
# ---------------------------------------------------------------------------

def build_approval_app(pending: PendingApprovals) -> FastAPI:
    app = FastAPI()

    @app.post("/slack")
    async def slack_webhook(request: Request) -> Response:
        body = await request.body()
        text = body.decode().strip()
        upper = text.upper()

        for command in ("APPROVE", "STOP"):
            if upper.startswith(command + " "):
                plan_id = text[len(command) + 1:].strip().lower()
                approved = command == "APPROVE"
                found = pending.resolve(plan_id, approved)
                if found:
                    action = "approved" if approved else "stopped"
                    return Response(content=f"Plan {plan_id} {action}.", media_type="text/plain")
                return Response(content=f"Unknown plan ID: {plan_id}", media_type="text/plain")

        return Response(content="Unrecognised command. Use APPROVE <id> or STOP <id>.", media_type="text/plain")

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

        slack_cfg = config.get("slack", {})
        self._slack = SlackClient(
            webhook_url=slack_cfg.get("webhook_url", ""),
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(self, message: str, trigger: str = "cli:user_message") -> str:
        """Process a single user message through the agentic loop. Returns final text."""
        self._history.append({"role": "user", "content": message})
        self._trim_history()
        return await self._run_loop(trigger)

    async def handle_event(self, event: dict) -> None:
        """Convert a queue event into a user message and run the loop."""
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

        await self.chat(msg, trigger=trigger)

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    async def _run_loop(self, trigger: str) -> str:
        final_text = ""
        for _ in range(MAX_ITERATIONS):
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=self._system_prompt,
                messages=self._history,
                tools=TOOL_DEFINITIONS,
            )
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
                tool_results = await self._handle_tool_calls(response.content, trigger)
                self._history.append({"role": "user", "content": tool_results})
                self._trim_history()

        return final_text

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

        await self._slack.notify_plan(plan_id, plan_text, veto_seconds)
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
        if not self._slack.configured:
            console.print("  [dim](Slack not configured — approve here in the terminal)[/dim]")
        if veto_seconds is not None:
            console.print(f"  Type [bold]APPROVE {plan_id}[/bold] or [bold]STOP {plan_id}[/bold] (auto-cancels in {veto_seconds}s)")
        else:
            console.print(f"  Type [bold]APPROVE {plan_id}[/bold] or [bold]STOP {plan_id}[/bold] (no timeout)")

        fut = self._pending.register(plan_id)
        approved: bool
        reason: str

        try:
            if veto_seconds is not None:
                approved = await asyncio.wait_for(asyncio.shield(fut), timeout=veto_seconds)
            else:
                approved = await fut
            reason = "slack:STOP" if not approved else ""
        except asyncio.TimeoutError:
            approved = False
            reason = "timeout"
            self._pending.resolve(plan_id, False)

        if not approved:
            await self._logger.log_plan_cancelled(plan_id, block.name, reason)
            return f"[cancelled: {reason}]"

        await self._logger.log_plan_approved(plan_id, block.name)
        result = await self._tools.execute(block.name, tool_input)
        await self._logger.log_action_taken(
            tool=block.name,
            tool_input=tool_input,
            outcome=result,
            tier=resolved.tier,
            safe_mode_active=resolved.safe_mode_active,
            trigger=trigger,
        )
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
            console.print(f"  [bold yellow]  [SAFE MODE — tier forced to 3][/bold yellow]")
        if resolved.agent_reasoning:
            console.print(f"  [dim italic]  tier reasoning: {resolved.agent_reasoning}[/dim italic]")

    def _trim_history(self) -> None:
        """Keep at most MAX_HISTORY_TURNS turn-pairs (user+assistant)."""
        # Each turn pair is 2 entries; trim from the front
        max_entries = MAX_HISTORY_TURNS * 2
        if len(self._history) > max_entries:
            self._history = self._history[-max_entries:]

    # ------------------------------------------------------------------
    # Approval listener lifecycle
    # ------------------------------------------------------------------

    async def start_approval_listener(self, host: str, port: int) -> asyncio.Task:
        app = build_approval_app(self._pending)
        server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(server_config)

        async def _serve() -> None:
            await server.serve()

        task = asyncio.create_task(_serve())
        return task

    async def aclose(self) -> None:
        await self._slack.aclose()
