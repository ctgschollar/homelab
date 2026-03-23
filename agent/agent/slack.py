from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
from rich.console import Console

_console = Console()


_COLORS = {
    "info":    "#378ADD",
    "warning": "#EF9F27",
    "error":   "#E24B4A",
    "success": "#639922",
    "action":  "#7F77DD",
}

_API = "https://slack.com/api"


class SlackClient:
    def __init__(self, bot_token: str, signing_secret: str, channel: str) -> None:
        self._token = bot_token
        self._secret = signing_secret
        self._channel = channel
        self._http = httpx.AsyncClient(timeout=10.0)

    @property
    def configured(self) -> bool:
        return bool(self._token) and not self._token.startswith("${")

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    def verify_signature(self, timestamp: str, raw_body: bytes, signature: str) -> bool:
        """Return True if the request signature from Slack is valid."""
        if abs(time.time() - float(timestamp)) > 300:
            return False
        base = f"v0:{timestamp}:{raw_body.decode()}"
        expected = "v0=" + hmac.new(
            self._secret.encode(), base.encode(), digestmod=hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # Low-level API helpers
    # ------------------------------------------------------------------

    async def _call(self, method: str, payload: dict) -> dict:
        if not self.configured:
            return {}
        resp = await self._http.post(
            f"{_API}/{method}",
            headers={"Authorization": f"Bearer {self._token}"},
            json=payload,
        )
        data = resp.json()
        if not data.get("ok"):
            _console.print(f"  [bold red]Slack API error ({method}):[/bold red] {data.get('error', data)}")
        return data

    async def _post_message(self, blocks: list, text: str = "") -> dict:
        return await self._call("chat.postMessage", {
            "channel": self._channel,
            "text": text,
            "blocks": blocks,
        })

    async def _update_message(self, channel: str, ts: str, blocks: list, text: str = "") -> dict:
        return await self._call("chat.update", {
            "channel": channel,
            "ts": ts,
            "text": text,
            "blocks": blocks,
        })

    async def open_modal(self, trigger_id: str, view: dict) -> dict:
        return await self._call("views.open", {
            "trigger_id": trigger_id,
            "view": view,
        })

    # ------------------------------------------------------------------
    # Block builders
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_blocks(plan_id: str, plan_text: str, veto_seconds: int | None) -> list:
        timeout_note = (
            f"\n_Auto-cancels in {veto_seconds}s if no response._"
            if veto_seconds is not None
            else "\n_Waiting indefinitely for explicit approval._"
        )
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "⏳ Plan proposed"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Plan ID:* `{plan_id}`\n{plan_text}{timeout_note}"},
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve"},
                        "style": "primary",
                        "action_id": "plan_approve",
                        "value": plan_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Deny"},
                        "style": "danger",
                        "action_id": "plan_deny",
                        "value": plan_id,
                    },
                ],
            },
        ]

    @staticmethod
    def _approval_modal(plan_id: str, plan_text: str, approved: bool) -> dict:
        action_label = "Approve" if approved else "Deny"
        colour = "✅" if approved else "❌"
        return {
            "type": "modal",
            "callback_id": "plan_confirm",
            "private_metadata": json.dumps({"plan_id": plan_id, "approved": approved}),
            "title": {"type": "plain_text", "text": f"{colour} {action_label} plan"},
            "submit": {"type": "plain_text", "text": f"{colour} Confirm {action_label}"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Plan ID:* `{plan_id}`\n{plan_text}"},
                },
                {"type": "divider"},
                {
                    "type": "input",
                    "block_id": "context_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Context (optional)"},
                    "hint": {
                        "type": "plain_text",
                        "text": "Add any additional context for the agent, e.g. 'I've brought dks03 back online'",
                    },
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "context_input",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "Optional context…"},
                    },
                },
            ],
        }

    @staticmethod
    def _resolved_blocks(plan_id: str, plan_text: str, approved: bool, context: str, by: str) -> list:
        icon = "✅" if approved else "❌"
        label = "Approved" if approved else "Denied"
        status_line = f"{icon} *{label}* by {by}"
        if context:
            status_line += f" — {context}"
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{icon} Plan {label}: {plan_id}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": plan_text},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": status_line},
            },
        ]

    # ------------------------------------------------------------------
    # High-level notification methods
    # ------------------------------------------------------------------

    async def notify_plan(
        self,
        plan_id: str,
        plan_text: str,
        veto_seconds: int | None,
    ) -> str | None:
        """Post the plan message. Returns the message timestamp (for later update)."""
        blocks = self._plan_blocks(plan_id, plan_text, veto_seconds)
        result = await self._post_message(blocks, text=f"Plan proposed: {plan_id}")
        return result.get("ts")

    async def resolve_plan_message(
        self,
        channel: str,
        ts: str,
        plan_id: str,
        plan_text: str,
        approved: bool,
        context: str,
        by: str,
    ) -> None:
        """Update the original plan message to show the resolution."""
        blocks = self._resolved_blocks(plan_id, plan_text, approved, context, by)
        await self._update_message(channel, ts, blocks)

    async def update_plan_result(
        self,
        ts: str,
        plan_id: str,
        plan_text: str,
        result: str,
    ) -> None:
        """Final update: replace the plan message with approval + execution result."""
        success = not result.startswith("ERROR:")
        icon = "✅" if success else "❌"
        label = "Completed" if success else "Failed"
        output = result if len(result) <= 2800 else result[:2800] + "\n…(truncated)"
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{icon} Plan {label}: {plan_id}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": plan_text},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Result:*\n```{output}```"},
            },
        ]
        await self._update_message(self._channel, ts, blocks, text=f"Plan {label}: {plan_id}")

    async def notify_action_taken(self, action: str, service: str, reason: str) -> None:
        await self._post_message([
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *Action taken:* `{action}`\n*Service:* {service}\n*Reason:* {reason}",
                },
            }
        ], text=f"Action taken: {action}")

    async def notify_alert(self, service: str, running: int, desired: int, last_error: str) -> None:
        await self._post_message([
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🚨 *Service degraded:* `{service}`\n*Replicas:* {running}/{desired}\n*Last error:* {last_error}",
                },
            }
        ], text=f"Service degraded: {service}")

    async def notify_resolved(self, service: str, how: str) -> None:
        await self._post_message([
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *Service recovered:* `{service}`\n*How:* {how}",
                },
            }
        ], text=f"Service recovered: {service}")

    async def notify(self, text: str) -> None:
        await self._post_message([
            {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        ], text=text)

    async def aclose(self) -> None:
        await self._http.aclose()
