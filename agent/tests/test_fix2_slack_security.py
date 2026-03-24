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
