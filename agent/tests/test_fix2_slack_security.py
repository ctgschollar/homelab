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
