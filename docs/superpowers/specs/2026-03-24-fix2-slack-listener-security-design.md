# Fix 2 — Slack Listener Signature Verification

**Date:** 2026-03-24
**Scope:** `agent/` directory only
**PR:** standalone (one PR per fix)

---

## Problem

`SlackClient.verify_signature` exists and is called in both `/slack/events` and `/slack/interactions`. However, the signature check guard in both handlers is `if slack.configured`, where `configured` is `bool(self._token) and not self._token.startswith("${")`. This means signature verification is skipped entirely whenever no bot token is set — even if a signing secret is configured. The approval listener still binds to `0.0.0.0` and accepts any POST that contains a valid plan ID.

Plan IDs are currently `f"plan-{secrets.token_hex(2)}"` — 4 hex characters representing 65,536 possible values. This is low enough to brute-force on a LAN within minutes. An attacker on the same network can iterate through all plan IDs and approve an arbitrary pending action without possessing the Slack signing secret.

---

## Design

### 1. `signature_verification_enabled` property (`slack.py`)

Add a new property to `SlackClient`:

```python
@property
def signature_verification_enabled(self) -> bool:
    return bool(self._secret) and not self._secret.startswith("${")
```

This property is independent of `configured` (which checks `self._token`). A deployment can have a signing secret without a bot token (e.g. receive-only webhook mode), and should still verify incoming requests.

`signing_secret = ""` (empty string) is treated as unconfigured by `signature_verification_enabled`: `bool(self._secret)` returns `False` for both `None` and `""`, so the property returns `False` in both cases and no verification is attempted.

Note on `verify_signature` internals: the guard inside `verify_signature` is `if self._secret is None: return False`. An empty string passes that `None` check and would reach `self._secret.encode()` without raising, but the resulting HMAC would be wrong — any signature would fail. Passing an empty string is therefore an invalid configuration; `signature_verification_enabled` prevents it from being used by returning `False` before `verify_signature` is ever called. Deployments should treat an empty `signing_secret` as unconfigured (Pydantic validation in `config_schema.py` is the appropriate place to enforce this as an error if stricter checking is wanted).

**Update both endpoint handlers in `build_approval_app` (`agent.py`)** to use this property in place of `slack.configured` for the signature check guard:

In `/slack/events`:
```python
if slack.signature_verification_enabled and not slack.verify_signature(timestamp, raw_body, signature):
    return Response(content="Invalid signature", status_code=403)
```

In `/slack/interactions`:
```python
if slack.signature_verification_enabled and not slack.verify_signature(timestamp, raw_body, signature):
    console.print("  [bold red]Slack signature verification failed[/bold red]")
    return Response(content="Invalid signature", status_code=403)
```

The existing `verify_signature` method itself is unchanged. Its internal guard is `if self._secret is None: return False`, which prevents an `AttributeError` on `None.encode()`. An empty-string secret would not raise there but would produce an incorrect HMAC — this is why `signature_verification_enabled` must return `False` for empty strings before `verify_signature` is invoked. The guard change in the endpoint handlers only affects when verification is attempted.

### 2. Listener host enforcement (`agent.py`)

Extract the host-resolution logic into a standalone module-level helper:

```python
def _resolve_listener_host(host: str, signing_secret_configured: bool) -> str:
    if not signing_secret_configured and host == "0.0.0.0":
        console.print("[bold red]WARNING: Slack signing secret not configured — approval listener restricted to localhost[/bold red]")
        return "127.0.0.1"
    return host
```

Then in `start_approval_listener`, before constructing the `uvicorn.Config`, call the helper:

```python
host = _resolve_listener_host(host, self._slack.signature_verification_enabled)
```

This enforcement happens in Python before uvicorn binds, so the socket never opens on a non-loopback interface without a verified secret. The `host` parameter passed to `uvicorn.Config` is the resolved value returned by the helper.

The helper receives `signing_secret_configured=self._slack.signature_verification_enabled` (not `self._slack.configured`) so that a bot-token-less deployment with a signing secret correctly keeps `0.0.0.0` binding. Extracting the guard into `_resolve_listener_host` also makes the logic directly unit-testable without constructing a `HomelabAgent` or starting uvicorn.

No change is made to `ApprovalListenerConfig` in `config_schema.py`. The default remains `host = "0.0.0.0"` — the runtime enforcement in `start_approval_listener` is the safety net, not a config-level restriction.

### 3. Plan ID entropy (`agent.py`)

In `_handle_approval_flow`, change the plan ID generation from:

```python
plan_id = f"plan-{secrets.token_hex(2)}"
```

to:

```python
plan_id = f"plan-{secrets.token_hex(4)}"
```

`secrets.token_hex(4)` produces 8 hex characters (4 bytes = 32 bits), giving 4,294,967,296 possible values — making brute-force infeasible even on a fast LAN.

The `plan-` prefix and overall string format are unchanged. No other code parses or validates the plan ID format, so this is a clean drop-in change with no ripple effects.

### 4. `agent/README.md`

Create `agent/README.md` documenting the Slack app configuration requirements. This file does not currently exist. The file must follow this top-level structure exactly:

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

Each section must contain 2–4 sentences minimum. The two Slack sub-sections that configure endpoints must include the exact endpoint paths:

- **Events API configuration** — state that the Request URL must be set to `https://<host>/slack/events` in the Slack app under "Event Subscriptions".
- **Interactivity configuration** — state that the Request URL must be set to `https://<host>/slack/interactions` under "Interactivity & Shortcuts".

Additional content requirements:

- The signing secret is required for the approval listener to bind on a non-loopback interface. Without it, the listener restricts to `127.0.0.1` automatically.
- How to obtain the signing secret from the Slack app configuration page (Basic Information → App Credentials → Signing Secret).
- The bot token (`SLACK_BOT_TOKEN`) and signing secret (`SLACK_SIGNING_SECRET`) are loaded from environment variables, not from `config.yaml` directly (see `YamlConfigSettingsSource` in `config_schema.py`).

---

## Files changed

| File | Change |
|------|--------|
| `agent/agent/slack.py` | Add `signature_verification_enabled` property to `SlackClient` |
| `agent/agent/agent.py` | Update signature check guards in `build_approval_app`; extract `_resolve_listener_host` helper and add host enforcement in `start_approval_listener`; change `token_hex(2)` to `token_hex(4)` in `_handle_approval_flow` |
| `agent/README.md` | New file — Slack app configuration documentation |
| `agent/tests/test_fix2_slack_security.py` | New file — unit tests (see Tests section) |

No changes to `config_schema.py`, `config.yaml`, `safety.py`, or any other file.

---

## Tests

New file: `agent/tests/test_fix2_slack_security.py`

All tests are unit tests. No live Slack API calls, no uvicorn processes. Use `pytest` and `httpx.AsyncClient` with FastAPI's `TestClient` or `AsyncClient` for endpoint tests.

### Test fixtures

`HomelabAgent` cannot be instantiated directly in unit tests because `__init__` requires a fully-populated `AgentConfig` (Pydantic model with required fields for Anthropic credentials, file paths, etc.). Tests for listener host enforcement must not attempt to construct a real `HomelabAgent`.

Instead, test the enforcement logic directly by extracting it into a standalone helper:

```python
def _resolve_listener_host(host: str, signing_secret_configured: bool) -> str:
    ...
```

This helper encapsulates the `if not signing_secret_configured and host == "0.0.0.0"` guard from `start_approval_listener`. Unit tests call this function directly — no agent, no uvicorn. For tests that must exercise the full `start_approval_listener` path, patch `uvicorn.Config` and `uvicorn.Server` with `MagicMock` / `AsyncMock` to prevent socket binding, and build a minimal `HomelabAgent`-like object using `unittest.mock.MagicMock(spec=HomelabAgent)` with `_slack` pre-configured. Prefer testing the guard logic via `_resolve_listener_host` over constructing a full agent.

### `SlackClient` property tests

**`test_signature_verification_enabled_with_secret`**
Construct `SlackClient(bot_token=None, signing_secret="mysecret", channel="#ops")`. Assert `client.signature_verification_enabled is True`.

**`test_signature_verification_enabled_without_secret`**
Construct `SlackClient(bot_token=None, signing_secret=None, channel="#ops")`. Assert `client.signature_verification_enabled is False`.

**`test_signature_verification_enabled_with_placeholder_secret`**
Construct `SlackClient(bot_token=None, signing_secret="${SLACK_SIGNING_SECRET}", channel="#ops")`. Assert `client.signature_verification_enabled is False`.

**`test_configured_unchanged_checks_token_not_secret`**
Construct `SlackClient(bot_token="xoxb-real-token", signing_secret=None, channel="#ops")`. Assert `client.configured is True`. Construct with `bot_token=None`. Assert `client.configured is False`. This confirms `configured` is not affected by the new property.

### Listener host enforcement tests

These tests call `_resolve_listener_host` directly — the standalone helper extracted from `start_approval_listener`. No agent construction, no uvicorn mocking required. Use `AgentConfig.model_construct()` (bypass-validation constructor) if any test needs an `AgentConfig` instance with only the fields relevant to the assertion; do not build a fully-populated config with all required fields just to test host resolution.

**`test_listener_no_secret_public_host_forced_to_localhost`**
Call `_resolve_listener_host(host="0.0.0.0", signing_secret_configured=False)`. Assert the return value is `"127.0.0.1"`.

**`test_listener_with_secret_public_host_unchanged`**
Call `_resolve_listener_host(host="0.0.0.0", signing_secret_configured=True)`. Assert the return value is `"0.0.0.0"`.

**`test_listener_no_secret_localhost_host_unchanged`**
Call `_resolve_listener_host(host="127.0.0.1", signing_secret_configured=False)`. Assert the return value is `"127.0.0.1"`.

For tests that must exercise the full `start_approval_listener` path (e.g. to assert the console warning is printed), patch `uvicorn.Config` and `uvicorn.Server` with `MagicMock` / `AsyncMock` to prevent socket binding, and build a minimal agent stand-in using `unittest.mock.MagicMock(spec=HomelabAgent)` with `_slack` pre-configured. Do not construct a real `HomelabAgent` or a full `AgentConfig` for these tests.

### Endpoint signature verification tests

Async test functions must be decorated with `@pytest.mark.asyncio`. Add `pytest-asyncio` to the test dependencies in `agent/pyproject.toml` (or `requirements-dev.txt`, whichever the project uses) if it is not already present. Set `asyncio_mode = "auto"` in `pytest.ini` / `pyproject.toml` `[tool.pytest.ini_options]` to avoid adding the decorator to every test individually, or apply it per-test as needed.

Use `httpx.AsyncClient(app=app, base_url="http://test")` with `pytest-asyncio` or `anyio` to drive async FastAPI endpoints.

**`test_events_endpoint_returns_403_when_secret_configured_and_signature_invalid`**
Build the approval app with a `SlackClient` that has `signing_secret="mysecret"` (so `signature_verification_enabled` is `True`). POST to `/slack/events` with a valid JSON body but an invalid `X-Slack-Signature` header (e.g. `"v0=badhash"`). Assert response status is 403.

**`test_events_endpoint_allows_request_when_no_secret_configured`**
Build the approval app with a `SlackClient` that has `signing_secret=None` (so `signature_verification_enabled` is `False`). POST to `/slack/events` with a `{"type": "url_verification", "challenge": "abc123"}` body. Assert response status is 200 and body contains `{"challenge": "abc123"}`. This confirms requests pass through when verification is disabled.

### Plan ID format test

**`test_plan_id_is_8_hex_chars`**
Two approaches, both valid:

*Approach A (pure unit test, no agent):* Import `secrets` directly and assert that `f"plan-{secrets.token_hex(4)}"` matches `r'^plan-[0-9a-f]{8}$'`. This confirms the format string produces 8 hex characters. Then verify the literal `plan_id = f"plan-{secrets.token_hex(4)}"` assignment exists in `_handle_approval_flow` by inspecting the source — this test fails if someone reverts the change to `token_hex(2)`.

*Approach B (integration with mocks):* Replace `agent._slack` with an `AsyncMock` and `agent._logger` with an `AsyncMock`. Patch `secrets.token_hex` in the `agent.agent.agent` module namespace to return a known fixed string (e.g. `"aabbccdd"`). Register a plan via `agent._pending.register(plan_id, ...)` and immediately resolve it so `_handle_approval_flow` completes without blocking. Assert the plan ID that was registered matches `r'^plan-[0-9a-f]{8}$'` and specifically equals `"plan-aabbccdd"`. This confirms `token_hex(4)` is used at the `_handle_approval_flow` call site.

---

## Out of scope

- **`verify_signature` internals** — the HMAC logic, the 5-minute timestamp window, and the `v0=` prefix handling are correct and unchanged.
- **`configured` property** — only `signature_verification_enabled` is new. `configured` continues to gate API calls (`_call`) and is not affected.
- **`ApprovalListenerConfig` schema** — no change. The `host` default stays `"0.0.0.0"`; runtime enforcement in `start_approval_listener` is the safety mechanism.
- **Integration tests** — no live Slack webhook tests, no real uvicorn startup.
- **Fix 5 startup validation** — `config_schema.py` already has a `_warn_missing_signing_secret` model validator that emits a warning. Fix 2 does not add a second validator or change the schema.
- **Placeholder not warned at startup** — the `_warn_missing_signing_secret` Pydantic validator in `config_schema.py` will NOT fire when `signing_secret` is set to a `${...}` placeholder that has not been resolved (the string is truthy). However, `signature_verification_enabled` will correctly return `False` at runtime because of the `not self._secret.startswith("${")` guard. This is a known gap — Fix 2 does not address it.
- **Other fixes** — no interaction with Fix 1 (shell guards), Fix 3 (concurrent execution), Fix 4 (history), Fix 5 (config schema), or Fix 6 (monitor muting).
