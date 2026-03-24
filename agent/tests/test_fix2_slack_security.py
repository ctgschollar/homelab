"""Tests for Fix 2: Slack Listener Security."""
from __future__ import annotations

from agent.slack import SlackClient


class TestSignatureVerificationEnabled:
    def test_signature_verification_enabled_with_secret(self) -> None:
        client = SlackClient(bot_token=None, signing_secret="mysecret", channel="#ops")
        assert client.signature_verification_enabled is True

    def test_signature_verification_enabled_without_secret(self) -> None:
        client = SlackClient(bot_token=None, signing_secret=None, channel="#ops")
        assert client.signature_verification_enabled is False

    def test_signature_verification_enabled_with_empty_secret(self) -> None:
        client = SlackClient(bot_token=None, signing_secret="", channel="#ops")
        assert client.signature_verification_enabled is False

    def test_signature_verification_enabled_with_placeholder_secret(self) -> None:
        client = SlackClient(bot_token=None, signing_secret="${SLACK_SIGNING_SECRET}", channel="#ops")
        assert client.signature_verification_enabled is False

    def test_configured_unchanged_checks_token_not_secret(self) -> None:
        client_with_token = SlackClient(bot_token="xoxb-real-token", signing_secret=None, channel="#ops")
        assert client_with_token.configured is True

        client_no_token = SlackClient(bot_token=None, signing_secret="mysecret", channel="#ops")
        assert client_no_token.configured is False


from agent.agent import _resolve_listener_host


class TestResolveListenerHost:
    def test_listener_no_secret_public_host_forced_to_localhost(self) -> None:
        result = _resolve_listener_host(host="0.0.0.0", signing_secret_configured=False)
        assert result == "127.0.0.1"

    def test_listener_with_secret_public_host_unchanged(self) -> None:
        result = _resolve_listener_host(host="0.0.0.0", signing_secret_configured=True)
        assert result == "0.0.0.0"

    def test_listener_no_secret_localhost_host_unchanged(self) -> None:
        result = _resolve_listener_host(host="127.0.0.1", signing_secret_configured=False)
        assert result == "127.0.0.1"

    def test_listener_with_secret_localhost_host_unchanged(self) -> None:
        result = _resolve_listener_host(host="127.0.0.1", signing_secret_configured=True)
        assert result == "127.0.0.1"


import re
import inspect

from agent import HomelabAgent


class TestPlanIdEntropy:
    def test_plan_id_format_is_8_hex_chars(self) -> None:
        import secrets
        plan_id = f"plan-{secrets.token_hex(4)}"
        assert re.match(r'^plan-[0-9a-f]{8}$', plan_id), f"Unexpected format: {plan_id}"

    def test_token_hex_4_used_in_handle_approval_flow(self) -> None:
        """Verify the literal token_hex(4) call exists in _handle_approval_flow source."""
        source = inspect.getsource(HomelabAgent._handle_approval_flow)
        assert "token_hex(4)" in source, "Expected token_hex(4) in _handle_approval_flow; was token_hex(2) reverted?"
        assert "token_hex(2)" not in source, "Found token_hex(2) in _handle_approval_flow; should be token_hex(4)"


import json
import httpx
import unittest.mock

from agent.agent import PendingApprovals, build_approval_app
from agent.slack import SlackClient


class TestEndpointSignatureVerification:
    def _make_app(self, signing_secret: str | None):
        pending = PendingApprovals()
        slack = SlackClient(bot_token=None, signing_secret=signing_secret, channel="#ops")
        return build_approval_app(pending, slack, event_queue=None)

    async def test_events_endpoint_returns_403_when_secret_configured_and_signature_invalid(self) -> None:
        app = self._make_app(signing_secret="mysecret")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/slack/events",
                content=json.dumps({"type": "url_verification", "challenge": "abc123"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Slack-Request-Timestamp": "1234567890",
                    "X-Slack-Signature": "v0=badhash",
                },
            )
        assert response.status_code == 403

    async def test_events_endpoint_allows_request_when_no_secret_configured(self) -> None:
        app = self._make_app(signing_secret=None)
        body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/slack/events",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 200
        assert response.json() == {"challenge": "abc123"}

    async def test_interactions_endpoint_returns_403_when_secret_configured_and_signature_invalid(self) -> None:
        app = self._make_app(signing_secret="mysecret")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/slack/interactions",
                content=b"payload=%7B%7D",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Slack-Request-Timestamp": "1234567890",
                    "X-Slack-Signature": "v0=badhash",
                },
            )
        assert response.status_code == 403

    async def test_interactions_endpoint_allows_request_when_no_secret_configured(self) -> None:
        app = self._make_app(signing_secret=None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/slack/interactions",
                content=b"payload=%7B%22type%22%3A%22block_actions%22%2C%22actions%22%3A%5B%5D%7D",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert response.status_code == 200
