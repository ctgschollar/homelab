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
