# Fix 2: Slack Listener Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the Slack approval listener with request signature verification and localhost enforcement when no signing secret is configured.

**Architecture:** Add a `signature_verification_enabled` property to `SlackClient` that checks for a real (non-None, non-empty, non-placeholder) signing secret, and use it in place of `slack.configured` in both endpoint handlers. Extract a module-level `_resolve_listener_host` helper in `agent.py` that restricts the listener to `127.0.0.1` when no signing secret is present, and call it from `start_approval_listener` before uvicorn binds. Increase plan ID entropy from `token_hex(2)` (4 hex chars, 65k values) to `token_hex(4)` (8 hex chars, 4B values).

**Tech Stack:** Python 3.11, Pydantic v2, pytest, pytest-asyncio, aiohttp

---

## Task 1: Add `signature_verification_enabled` property to `SlackClient`

**File:** `/home/chris/src/homelab/agent/agent/slack.py`

### What to change

Add a new `@property` to `SlackClient` immediately after the existing `configured` property (line 35):

```python
@property
def signature_verification_enabled(self) -> bool:
    return bool(self._secret) and not self._secret.startswith("${")
```

This is independent of `configured` (which checks `self._token`). It returns `False` for `None`, `""`, and `"${SLACK_SIGNING_SECRET}"` placeholders. The existing `verify_signature` method is unchanged — its internal guard is `if self._secret is None: return False`.

### Tests to write

**File:** `/home/chris/src/homelab/agent/tests/test_fix2_slack_security.py`

```python
"""Tests for Fix 2: Slack Listener Security."""
from __future__ import annotations

from agent.agent.slack import SlackClient


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
```

### Steps

- [ ] Write the failing tests in `agent/tests/test_fix2_slack_security.py` (just the `TestSignatureVerificationEnabled` class)
- [ ] Run tests and confirm they fail: `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py -v`
- [ ] Add `signature_verification_enabled` property to `SlackClient` in `agent/agent/slack.py` after line 35 (after the `configured` property)
- [ ] Run tests and confirm they pass: `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestSignatureVerificationEnabled -v`
- [ ] Commit:
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/slack.py agent/tests/test_fix2_slack_security.py && git commit -m "$(cat <<'EOF'
  feat(fix2): add signature_verification_enabled property to SlackClient

  Adds a property that returns True only when a real (non-None, non-empty,
  non-placeholder) signing secret is configured. Independent of `configured`
  which checks the bot token.

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Task 2: Add `_resolve_listener_host` helper and enforce in `start_approval_listener`

**File:** `/home/chris/src/homelab/agent/agent/agent.py`

### What to change

Add a module-level helper function after the `console = Console()` line (around line 25), before the `ActionLogger` class:

```python
def _resolve_listener_host(host: str, signing_secret_configured: bool) -> str:
    if not signing_secret_configured and host == "0.0.0.0":
        console.print("[bold red]WARNING: Slack signing secret not configured — approval listener restricted to localhost[/bold red]")
        return "127.0.0.1"
    return host
```

Then modify `start_approval_listener` in `HomelabAgent` (currently lines 781–795) to call this helper before passing `host` to `uvicorn.Config`:

```python
async def start_approval_listener(
    self,
    host: str,
    port: int,
    event_queue: asyncio.Queue | None = None,
) -> tuple[asyncio.Task, uvicorn.Server]:
    host = _resolve_listener_host(host, self._slack.signature_verification_enabled)
    app = build_approval_app(self._pending, self._slack, event_queue)
    server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(server_config)

    async def _serve() -> None:
        await server.serve()

    task = asyncio.create_task(_serve())
    return task, server
```

### Tests to write

Append to `agent/tests/test_fix2_slack_security.py`:

```python
from agent.agent.agent import _resolve_listener_host


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
```

### Steps

- [ ] Add the `TestResolveListenerHost` tests to `agent/tests/test_fix2_slack_security.py`
- [ ] Run tests and confirm they fail (import error — `_resolve_listener_host` does not exist yet): `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestResolveListenerHost -v`
- [ ] Add `_resolve_listener_host` module-level function to `agent/agent/agent.py` after `console = Console()` (line 25)
- [ ] Modify `start_approval_listener` to call `_resolve_listener_host` as shown above
- [ ] Run tests and confirm they pass: `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestResolveListenerHost -v`
- [ ] Run the full test suite to confirm no regressions: `cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v`
- [ ] Commit:
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/agent.py agent/tests/test_fix2_slack_security.py && git commit -m "$(cat <<'EOF'
  feat(fix2): extract _resolve_listener_host helper and enforce localhost when no signing secret

  Adds a module-level helper that forces the approval listener to bind on
  127.0.0.1 when no signing secret is configured, preventing unauthenticated
  access on LAN interfaces. Called from start_approval_listener before uvicorn
  binds.

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Task 3: Increase plan ID entropy with `token_hex(4)`

**File:** `/home/chris/src/homelab/agent/agent/agent.py`

### What to change

In `_handle_approval_flow` (line 617), change:

```python
plan_id = f"plan-{secrets.token_hex(2)}"
```

to:

```python
plan_id = f"plan-{secrets.token_hex(4)}"
```

This changes plan IDs from 4 hex chars (65,536 values) to 8 hex chars (4,294,967,296 values), making brute-force infeasible even on a fast LAN. The `plan-` prefix and overall format are unchanged; no other code parses the plan ID format.

### Tests to write

Append to `agent/tests/test_fix2_slack_security.py`:

```python
import re
import secrets
import inspect

from agent.agent import agent as agent_module


class TestPlanIdEntropy:
    def test_plan_id_format_is_8_hex_chars(self) -> None:
        plan_id = f"plan-{secrets.token_hex(4)}"
        assert re.match(r'^plan-[0-9a-f]{8}$', plan_id), f"Unexpected format: {plan_id}"

    def test_token_hex_4_used_in_handle_approval_flow(self) -> None:
        """Verify the literal token_hex(4) call exists in _handle_approval_flow source."""
        source = inspect.getsource(agent_module.HomelabAgent._handle_approval_flow)
        assert "token_hex(4)" in source, "Expected token_hex(4) in _handle_approval_flow; was token_hex(2) reverted?"
        assert "token_hex(2)" not in source, "Found token_hex(2) in _handle_approval_flow; should be token_hex(4)"
```

### Steps

- [ ] Add the `TestPlanIdEntropy` tests to `agent/tests/test_fix2_slack_security.py`
- [ ] Run tests and confirm `test_token_hex_4_used_in_handle_approval_flow` fails: `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestPlanIdEntropy -v`
- [ ] Change `token_hex(2)` to `token_hex(4)` in `_handle_approval_flow` in `agent/agent/agent.py` (line 617)
- [ ] Run tests and confirm they pass: `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestPlanIdEntropy -v`
- [ ] Run the full test suite: `cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v`
- [ ] Commit:
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/agent.py agent/tests/test_fix2_slack_security.py && git commit -m "$(cat <<'EOF'
  fix(fix2): increase plan ID entropy from token_hex(2) to token_hex(4)

  Changes plan IDs from 4 hex chars (65,536 values) to 8 hex chars
  (4,294,967,296 values), making brute-force on a LAN infeasible.

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Task 4: Add HMAC signature verification to endpoint handlers

**File:** `/home/chris/src/homelab/agent/agent/agent.py`

### What to change

In `build_approval_app`, update the signature check guard in both endpoint handlers to use `signature_verification_enabled` instead of `configured`.

**`/slack/events` handler** (currently line 200):

Change:
```python
if slack.configured and not slack.verify_signature(timestamp, raw_body, signature):
    return Response(content="Invalid signature", status_code=403)
```

To:
```python
if slack.signature_verification_enabled and not slack.verify_signature(timestamp, raw_body, signature):
    return Response(content="Invalid signature", status_code=403)
```

**`/slack/interactions` handler** (currently line 232):

Change:
```python
if slack.configured and not slack.verify_signature(timestamp, raw_body, signature):
    console.print("  [bold red]Slack signature verification failed[/bold red]")
    return Response(content="Invalid signature", status_code=403)
```

To:
```python
if slack.signature_verification_enabled and not slack.verify_signature(timestamp, raw_body, signature):
    console.print("  [bold red]Slack signature verification failed[/bold red]")
    return Response(content="Invalid signature", status_code=403)
```

### Tests to write

These tests use `httpx.AsyncClient` with the FastAPI app directly. The project already has `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml`, so `@pytest.mark.asyncio` decorators are optional.

Append to `agent/tests/test_fix2_slack_security.py`:

```python
import json
import httpx
import pytest

from agent.agent.agent import PendingApprovals, build_approval_app
from agent.agent.slack import SlackClient


class TestEndpointSignatureVerification:
    def _make_app(self, signing_secret: str | None):
        pending = PendingApprovals()
        slack = SlackClient(bot_token=None, signing_secret=signing_secret, channel="#ops")
        return build_approval_app(pending, slack, event_queue=None)

    async def test_events_endpoint_returns_403_when_secret_configured_and_signature_invalid(self) -> None:
        app = self._make_app(signing_secret="mysecret")
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
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
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            response = await client.post(
                "/slack/events",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 200
        assert response.json() == {"challenge": "abc123"}

    async def test_interactions_endpoint_returns_403_when_secret_configured_and_signature_invalid(self) -> None:
        app = self._make_app(signing_secret="mysecret")
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
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
        async with httpx.AsyncClient(app=app, base_url="http://test") as client:
            response = await client.post(
                "/slack/interactions",
                content=b"payload=%7B%22type%22%3A%22block_actions%22%2C%22actions%22%3A%5B%5D%7D",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        # Request passes through (no signature error); returns 200 with empty body
        assert response.status_code == 200
```

**Note on `httpx.AsyncClient` with FastAPI:** `httpx` supports ASGI transport — pass `app=app` directly and it routes requests in-process without binding a port. This requires `httpx >= 0.20`. The project already has `httpx` as a dependency; no new packages needed.

### Steps

- [ ] Add the `TestEndpointSignatureVerification` tests to `agent/tests/test_fix2_slack_security.py`
- [ ] Run tests and confirm the 403 tests fail (currently returns 200 because `slack.configured` is False when no bot token): `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestEndpointSignatureVerification -v`
- [ ] Update the signature check guard in `/slack/events` handler in `agent/agent/agent.py`: change `slack.configured` to `slack.signature_verification_enabled`
- [ ] Update the signature check guard in `/slack/interactions` handler in `agent/agent/agent.py`: change `slack.configured` to `slack.signature_verification_enabled`
- [ ] Run tests and confirm they pass: `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestEndpointSignatureVerification -v`
- [ ] Run the full test suite to confirm no regressions: `cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v`
- [ ] Commit:
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/agent.py agent/tests/test_fix2_slack_security.py && git commit -m "$(cat <<'EOF'
  fix(fix2): use signature_verification_enabled in endpoint handlers

  Replaces slack.configured guard with slack.signature_verification_enabled
  in /slack/events and /slack/interactions handlers. A deployment with a
  signing secret but no bot token now correctly enforces HMAC verification.

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Task 5: Create `agent/README.md`

**File:** `/home/chris/src/homelab/agent/README.md` (new file)

### What to create

The spec requires a new `agent/README.md` documenting Slack app configuration. The file must follow this exact top-level structure:

```
# Homelab Agent

## Prerequisites
## Slack App Setup
### Required permissions
### Events API configuration
### Interactivity configuration
### Signing secret
## Environment Variables
## Running the agent
```

Required content:
- **Events API configuration** must state that the Request URL is `https://<host>/slack/events`
- **Interactivity configuration** must state that the Request URL is `https://<host>/slack/interactions`
- **Signing secret** section must note that the signing secret is required for the approval listener to bind on a non-loopback interface, and explain how to obtain it (Basic Information → App Credentials → Signing Secret)
- **Environment Variables** must note that `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` are loaded from environment variables, not from `config.yaml` directly (via `YamlConfigSettingsSource`)
- Each section must contain 2–4 sentences minimum

### Steps

- [ ] Create `/home/chris/src/homelab/agent/README.md` with the required structure and content as specified in the spec
- [ ] Verify all required sections are present and each has at least 2 sentences
- [ ] Commit:
  ```bash
  cd /home/chris/src/homelab && git add agent/README.md && git commit -m "$(cat <<'EOF'
  docs(fix2): add agent/README.md with Slack app configuration guide

  Documents prerequisites, Slack app setup (permissions, Events API endpoint,
  Interactivity endpoint, signing secret), environment variables, and how to
  run the agent.

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Task 6: Fix `_tool_slack_notify` always returning success

**File:** `agent/agent/tools.py`

### What to change

`_tool_slack_notify` currently returns `"Slack notification sent."` unconditionally, even when the Slack API call fails (wrong channel, invalid token, network error). The `_call` method returns `{}` on failure, so a check on the result can detect this.

Change `_tool_slack_notify` to check the API response and return an error string on failure:

```python
async def _tool_slack_notify(self, inp: dict) -> str:
    message = inp["message"]
    result = await self._slack.notify(message)
    if result is None or not result.get("ok"):
        error = result.get("error", "unknown") if result else "slack not configured"
        return f"ERROR: Slack notification failed: {error}"
    return "Slack notification sent."
```

This requires `notify` to return the API response dict. Update `SlackClient.notify` in `slack.py` to return the result of `_post_message`:

```python
async def notify(self, text: str) -> dict:
    return await self._post_message([
        {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    ], text=text)
```

`_post_message` returns `{}` when not configured, and the API response dict otherwise. `{}` has no `"ok"` key so the error branch triggers with `"slack not configured"`.

### Tests to write

Append to `agent/tests/test_fix2_slack_security.py`:

```python
class TestSlackNotifyResult:
    async def test_notify_returns_error_when_not_configured(self) -> None:
        slack = SlackClient(bot_token=None, signing_secret=None, channel="#ops")
        executor = ToolExecutor.__new__(ToolExecutor)
        executor._slack = slack
        result = await executor._tool_slack_notify({"message": "hello"})
        assert result.startswith("ERROR:")

    async def test_notify_returns_success_string_when_api_ok(self) -> None:
        slack = SlackClient(bot_token=None, signing_secret=None, channel="#ops")
        executor = ToolExecutor.__new__(ToolExecutor)
        executor._slack = slack
        with unittest.mock.patch.object(slack, "_post_message", return_value={"ok": True}):
            result = await executor._tool_slack_notify({"message": "hello"})
        assert result == "Slack notification sent."
```

### Steps

- [ ] Add `TestSlackNotifyResult` tests to `agent/tests/test_fix2_slack_security.py`
- [ ] Run and confirm they fail: `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestSlackNotifyResult -v`
- [ ] Update `SlackClient.notify` in `agent/agent/slack.py` to return the `_post_message` result
- [ ] Update `_tool_slack_notify` in `agent/agent/tools.py` to check the result and return an error string on failure
- [ ] Run and confirm they pass: `cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix2_slack_security.py::TestSlackNotifyResult -v`
- [ ] Run full suite: `cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v`
- [ ] Commit:
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/slack.py agent/agent/tools.py agent/tests/test_fix2_slack_security.py && git commit -m "$(cat <<'EOF'
  fix: _tool_slack_notify now returns error string when Slack API fails

  Previously always returned "Slack notification sent." regardless of API
  result. Now checks the response dict and returns an ERROR: prefixed string
  when ok is false or Slack is not configured, so the agent knows when
  notifications fail.

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Final verification

After all tasks are complete, run the full test suite one final time:

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
```

Expected: all tests in `tests/test_fix2_slack_security.py` pass, no regressions in other test files.

### Summary of all files changed

| File | Change |
|------|--------|
| `agent/agent/slack.py` | Add `signature_verification_enabled` property after `configured`; `notify` returns the `_post_message` result dict |
| `agent/agent/agent.py` | Add `_resolve_listener_host` module-level helper; call it from `start_approval_listener`; change `token_hex(2)` to `token_hex(4)` in `_handle_approval_flow`; replace `slack.configured` with `slack.signature_verification_enabled` in both endpoint handlers |
| `agent/agent/tools.py` | `_tool_slack_notify` checks API response and returns error string on failure |
| `agent/README.md` | New file — Slack app configuration documentation |
| `agent/tests/test_fix2_slack_security.py` | New file — all unit and async endpoint tests |

No changes to `config_schema.py`, `cli.py`, or `safety.py`.
