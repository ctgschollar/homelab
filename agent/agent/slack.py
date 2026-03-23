import httpx


_COLORS = {
    "info": "#378ADD",
    "warning": "#EF9F27",
    "error": "#E24B4A",
    "success": "#639922",
    "action": "#7F77DD",
}


class SlackClient:
    def __init__(self, webhook_url: str, channel: str) -> None:
        self._webhook_url = webhook_url
        self._channel = channel
        self._client = httpx.AsyncClient(timeout=10.0)

    @property
    def configured(self) -> bool:
        return bool(self._webhook_url) and not self._webhook_url.startswith("${")

    async def _post(self, payload: dict) -> None:
        if not self.configured:
            return
        payload.setdefault("channel", self._channel)
        try:
            await self._client.post(self._webhook_url, json=payload)
        except Exception:
            pass

    def _attachment(self, color_key: str, title: str, text: str) -> dict:
        return {
            "color": _COLORS.get(color_key, _COLORS["info"]),
            "title": title,
            "text": text,
            "mrkdwn_in": ["text"],
        }

    async def notify_action_taken(self, action: str, service: str, reason: str) -> None:
        await self._post({
            "attachments": [
                self._attachment(
                    "action",
                    f":white_check_mark: Action taken: `{action}`",
                    f"*Service:* {service}\n*Reason:* {reason}",
                )
            ]
        })

    async def notify_plan(
        self,
        plan_id: str,
        plan_text: str,
        veto_seconds: int | None,
    ) -> None:
        if veto_seconds is not None:
            footer = (
                f"Reply with `APPROVE {plan_id}` to execute or `STOP {plan_id}` to cancel.\n"
                f"Auto-cancels in {veto_seconds}s if no response."
            )
            color = "warning"
        else:
            footer = (
                f"Reply with `APPROVE {plan_id}` to execute or `STOP {plan_id}` to cancel.\n"
                f"*Waiting indefinitely for explicit approval.*"
            )
            color = "error"

        await self._post({
            "attachments": [
                self._attachment(
                    color,
                    f":hourglass: Plan proposed — ID: `{plan_id}`",
                    f"{plan_text}\n\n{footer}",
                )
            ]
        })

    async def notify_alert(self, service: str, running: int, desired: int, last_error: str) -> None:
        await self._post({
            "attachments": [
                self._attachment(
                    "error",
                    f":rotating_light: Service degraded: `{service}`",
                    f"*Replicas:* {running}/{desired}\n*Last error:* {last_error}",
                )
            ]
        })

    async def notify_resolved(self, service: str, how: str) -> None:
        await self._post({
            "attachments": [
                self._attachment(
                    "success",
                    f":white_check_mark: Service recovered: `{service}`",
                    f"*How:* {how}",
                )
            ]
        })

    async def notify(self, text: str, color_key: str = "info") -> None:
        """Generic notification for use by the slack_notify tool."""
        await self._post({
            "attachments": [
                self._attachment("info", ":robot_face: Homelab Agent", text)
            ]
        })

    async def aclose(self) -> None:
        await self._client.aclose()
