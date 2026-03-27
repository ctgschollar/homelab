# Fix 4 — Incident Sessions

**Date:** 2026-03-24
**Scope:** `agent/` directory only
**PR:** standalone (one PR per fix)

---

## Problem

`_trim_history` drops the oldest turn-pairs when `len(history) > MAX_HISTORY_TURNS * 2`. After trimming, the agent has no record of what actions it already executed this session. In a long incident — monitor alert → diagnose → deploy → investigate again — the agent may re-diagnose problems it already resolved or re-propose actions it already ran.

The original FIXES.md proposed injecting a `prior_actions` summary keyed by a `_started_at` timestamp. This spec supersedes that design with a full session model. A timestamp-based slice is ambiguous when the process restarts mid-incident or when multiple monitor events arrive close together. A session ID is an unambiguous, stable key.

---

## Design

### 1. IncidentSession dataclass

Add to `agent/agent/agent.py`, near the top (after imports, before `ActionLogger`):

```python
import uuid
from dataclasses import dataclass, field

@dataclass
class IncidentSession:
    session_id: str                    # UUID4, e.g. "a3f2b1c9-4e2d-..."
    started_at: datetime               # set by Python at session open; never guessed
    triggers: set[str]                 # service names from monitor events
    recovered: set[str]                # subset of triggers that have since recovered
    cli_messages: int                  # count of CLI/Slack user_message events this session
```

`session_id` is always produced with `str(uuid.uuid4())`. It is never inferred from log data.

`started_at` is a timezone-aware `datetime` set at session open via `datetime.now(timezone.utc)`. It is stored as a Python `datetime` object on the dataclass; when serialised to the action log it is written as an ISO 8601 UTC string via `session.started_at.isoformat()` (e.g. `"2026-03-24T14:01:33.412000+00:00"`).

`triggers` and `recovered` hold Docker service names exactly as received from the `service_down` / `services_down` event data (e.g. `"jellyfin_jellyfin"`). `cli_messages` counts how many `user_message` events arrived while this session was active; this value is recorded in the session log for observability (e.g. incident report context) but does **not** influence when the session closes. CLI messages alone do not keep a session open — a CLI-only session (one with no monitor triggers) closes immediately after the agent's response, and a monitor-triggered session closes only when all service triggers have recovered, regardless of how many CLI messages were received.

---

### 2. Session lifecycle

`HomelabAgent` gains one new field:

```python
self._current_session: IncidentSession | None = None
```

Initialised to `None` in `HomelabAgent.__init__`.

Session transitions are managed in two places: `handle_event` (for monitor events) and `event_consumer` in `cli.py` (for CLI/Slack messages and session closing after responses). The full transition table:

| Current state | Incoming event | Action |
|---|---|---|
| No session | `service_down` or `services_down` | Open new session. `triggers` = set of service names from event. Log `session_started`. |
| Active session | `service_down` or `services_down` | Add service names to `triggers`. No new session. No new `session_started` log. |
| No session | `user_message` (CLI or Slack) | Open new session. `triggers` = empty set. `cli_messages` = 1. Log `session_started`. |
| Active session | `user_message` (CLI or Slack) | Increment `cli_messages`. Session continues unchanged. |
| Active session | `service_recovered` (name in `triggers`) | Add service name to `recovered`. If `triggers` is non-empty and `recovered == triggers` → close session (log `session_ended`). |
| Active session | `service_recovered` (name NOT in `triggers`) | Ignore entirely — spurious recovery event. Do not add to `recovered`. Do not close session. |
| Active session (CLI-only, `triggers` empty) | Agent response completes | Close session (log `session_ended`). |
| Active session (monitor-triggered) | Agent response completes | Session remains open until all triggers are recovered. |

**Opening a session:**

```python
def _open_session(self, triggers: set[str]) -> IncidentSession:
    session = IncidentSession(
        session_id=str(uuid.uuid4()),
        started_at=datetime.now(timezone.utc),
        triggers=triggers,
        recovered=set(),
        cli_messages=0,
    )
    self._current_session = session
    return session
```

The `_open_session` call is immediately followed by `await self._logger.log_session_started(session)` (see Section 4).

**Closing a session:**

```python
async def _close_session(self) -> None:
    session = self._current_session
    if session is None:
        return
    self._current_session = None
    await self._logger.log_session_ended(session)
```

**Where session management lives in `handle_event`:**

At the top of `handle_event`, before calling `self.chat(...)`:

```python
async def handle_event(self, event: dict) -> tuple[str, float]:
    event_type = event.get("type", "")
    data = event.get("data", {})

    if event_type == "services_down":
        service_names = [s["service"] for s in data.get("services", [])]
        if self._current_session is None:
            session = self._open_session(triggers=set(service_names))
            await self._logger.log_session_started(session)
        else:
            self._current_session.triggers.update(service_names)

    elif event_type == "service_down":
        service_name = data.get("service", "unknown")
        if self._current_session is None:
            session = self._open_session(triggers={service_name})
            await self._logger.log_session_started(session)
        else:
            self._current_session.triggers.add(service_name)

    elif event_type == "service_recovered":
        service_name = data.get("service", "unknown")
        if self._current_session is not None:
            if service_name in self._current_session.triggers:
                self._current_session.recovered.add(service_name)
            # else: spurious recovery — not in triggers, ignore entirely

    # ... build msg, call self.chat(...), then check for session close below
```

Specifically:

- `services_down` event: service names are `[s["service"] for s in data["services"]]`. If no session, open one with `triggers = set(service_names)`. If session active, extend `self._current_session.triggers.update(service_names)`.
- `service_down` event (legacy): service name is `data["service"]`. Same logic as above for a single name.
- `service_recovered` event: service name is `data["service"]`. If no session, do nothing (recovery with no active session is a no-op — log it normally but no session to close). If session active, check whether the service name is in `session.triggers`. If it is NOT in `triggers` (spurious recovery event), ignore it entirely — do not add to `recovered`, do not close the session. If it IS in `triggers`, add to `recovered`. If `session.triggers` is non-empty and `session.recovered == session.triggers`, call `await self._close_session()` before the chat call returns.

The `service_recovered` close happens **after** `await self.chat(msg, trigger=trigger)` returns, because the agent's response (the Slack notification) is part of the same session. Close sequence:

```python
response_text, cost = await self.chat(msg, trigger=trigger)
if self._current_session and self._current_session.triggers:
    if self._current_session.recovered == self._current_session.triggers:
        await self._close_session()
return response_text, cost
```

**Where session management lives in `event_consumer` (cli.py):**

After the agent response completes for a `user_message` event:

```python
# If there is an active session with no triggers (pure CLI session), close it.
if agent._current_session is not None and not agent._current_session.triggers:
    await agent._close_session()
```

This runs after both `await agent.chat(...)` and `await agent.handle_event(...)` complete for `user_message` events. For monitor events, `handle_event` manages session close internally.

---

### 3. ActionLogger changes (session_id propagation)

`session_id` propagates to **every** log entry written during an active session. All `ActionLogger` logging methods gain an optional `session_id: str | None = None` parameter. When `session_id` is not `None`, it is added to the record before writing.

**`ActionLogger.log` signature change:**

```python
async def log(self, record: dict, session_id: str | None = None) -> None:
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    if session_id is not None:
        record["session_id"] = session_id
    async with self._lock:
        with open(self._path, "a") as f:
            f.write(json.dumps(record) + "\n")
```

**All other `ActionLogger` methods** gain `session_id: str | None = None` and pass it through to `self.log(...)`. Updated signatures:

```python
async def log_action_taken(
    self,
    tool: str,
    tool_input: dict,
    outcome: str,
    tier: int,
    safe_mode_active: bool,
    trigger: str,
    session_id: str | None = None,
) -> None: ...

async def log_plan_proposed(
    self,
    plan_id: str,
    tool: str,
    tool_input: dict,
    plan_text: str,
    tier: int,
    safe_mode_active: bool,
    trigger: str,
    session_id: str | None = None,
) -> None: ...

async def log_plan_approved(
    self,
    plan_id: str,
    tool: str,
    session_id: str | None = None,
) -> None: ...

async def log_plan_cancelled(
    self,
    plan_id: str,
    tool: str,
    reason: str,
    session_id: str | None = None,
) -> None: ...

async def log_tier_reasoning(
    self,
    tool: str,
    agent_proposed_tier: int,
    reasoning: str,
    safe_mode_active: bool,
    effective_tier: int,
    session_id: str | None = None,
) -> None: ...

async def log_cost(
    self,
    cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    trigger: str,
    session_id: str | None = None,
) -> None: ...
```

**Call sites in `HomelabAgent`** pass `session_id=self._current_session.session_id if self._current_session else None` at every call. The affected call sites are:

- `_run_loop` → `self._logger.log_cost(...)`
- `_handle_tool_calls` (outer scope, before tier-1/tier-2 branching) → `self._logger.log_tier_reasoning(...)`
- `_handle_tool_calls` → `_exec_tier1` (inner closure) → `self._logger.log_action_taken(...)`
- `_handle_approval_flow` → `self._logger.log_plan_proposed(...)`, `self._logger.log_plan_cancelled(...)`, `self._logger.log_plan_approved(...)`, `self._logger.log_action_taken(...)`

`MonitorDaemon` also calls `self._logger.log(...)` directly (for `monitor_alert` and `monitor_recovered` events). These do **not** receive a `session_id` — the daemon has no session reference. Session-level context is not needed in monitor-internal log entries; session tracking is agent-side only.

---

### 4. Session log events (session_started, session_ended)

Two new `ActionLogger` methods are added. They do not take a `session_id` parameter — the session object is passed directly.

```python
async def log_session_started(self, session: "IncidentSession") -> None:
    await self.log({
        "event": "session_started",
        "session_id": session.session_id,
        "triggers": sorted(session.triggers),
        "started_at": session.started_at.isoformat(),
    })

async def log_session_ended(self, session: "IncidentSession") -> None:
    duration = int(
        (datetime.now(timezone.utc) - session.started_at).total_seconds()
    )
    await self.log({
        "event": "session_ended",
        "session_id": session.session_id,
        "triggers": sorted(session.triggers),
        "recovered": sorted(session.recovered),
        "duration_seconds": duration,
        "cli_messages": session.cli_messages,
    })
```

**`session_started` log entry schema:**

```json
{
  "event": "session_started",
  "session_id": "a3f2b1c9-4e2d-4c1a-b5f2-d9e3f1a2b3c4",
  "triggers": ["jellyfin_jellyfin", "sonarr_sonarr"],
  "started_at": "2026-03-24T14:01:33.412+00:00",
  "ts": "2026-03-24T14:01:33.413+00:00"
}
```

`triggers` is an empty list `[]` for CLI-only sessions.

**`session_ended` log entry schema:**

```json
{
  "event": "session_ended",
  "session_id": "a3f2b1c9-4e2d-4c1a-b5f2-d9e3f1a2b3c4",
  "triggers": ["jellyfin_jellyfin", "sonarr_sonarr"],
  "recovered": ["jellyfin_jellyfin", "sonarr_sonarr"],
  "duration_seconds": 312,
  "cli_messages": 0,
  "ts": "2026-03-24T14:06:45.823+00:00"
}
```

For CLI-only sessions: `triggers` is `[]`, `recovered` is `[]`.

---

### 5. `_get_session_log` (replaces `_slice_action_log`)

`_slice_action_log(start_time: str) -> list[dict]` is **removed** from `ToolExecutor`.

A new method replaces it:

```python
def _get_session_log(self, session_id: str) -> list[dict]:
    entries: list[dict] = []
    try:
        with open(self._action_log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("session_id") == session_id:
                        entries.append(entry)
                except (json.JSONDecodeError, ValueError):
                    pass
    except FileNotFoundError:
        pass
    return entries
```

The method is synchronous (no `async`), matching the existing `_slice_action_log` pattern. It scans the entire log file but is only called once per `write_incident_report` invocation.

---

### 6. `write_incident_report` tool changes

**`TOOL_DEFINITIONS` entry for `write_incident_report`:**

- Remove the `start_time` property from `input_schema.properties`.
- Remove `start_time` from the description text that references it.
- Add `session_id` as a required string property:

```python
"session_id": {
    "type": "string",
    "description": (
        "The session ID for this incident, provided in the system prompt "
        "under '## Current session'. Pass it exactly as shown."
    ),
},
```

- Add `"session_id"` to `input_schema.required`.

The full updated `required` list: `["title", "tags", "inciting_incident", "resolution", "tools_used", "session_id"]`.

**`_tool_write_incident_report` implementation changes:**

Replace the `start_time` / `_slice_action_log` block with:

```python
session_id = inp["session_id"]
log_entries = self._get_session_log(session_id)
```

Remove all `start_time`, `raw_start`, `start_time_valid`, and `parsed` variables. Remove the `abs((now_utc - parsed).total_seconds()) <= 86400` validity check — session_id lookup is unambiguous and needs no date sanity check.

The rest of `_tool_write_incident_report` — incident numbering, slug generation, rejected plans extraction, shell commands extraction, narrative assembly, git commit, Slack notify — is unchanged.

The return string uses `session_id` in place of `start_time` in the confirmation message:

```python
return (
    f"INC-{num:04d} written and committed: `reports/{filename}` "
    f"({len(log_entries)} action log entries, session: {session_id}, tags: {tags_str}).\n{git_result}"
)
```

**`BEHAVIOUR_RULES` in `prompts.py`** — update the incident report instruction to replace `start_time` with `session_id`:

```
- Use `session_id` from the `## Current session` section of the system prompt.
  Never fabricate a session_id. If no session section is present, do not call write_incident_report.
```

Remove the `start_time` guidance paragraph entirely.

---

### 7. System prompt injection (`build_system_prompt`)

The `IncidentSession` type is imported with `TYPE_CHECKING` to avoid a circular import:

```python
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .agent import IncidentSession
```

**No session:** `build_system_prompt()` returns the existing prompt unchanged. The `## Current session` and `## Actions taken this session` sections are omitted entirely. This is the current behaviour.

**With session:** Two sections are appended after the existing `BEHAVIOUR_RULES` block.

**Section format:**

```
## Current session
Session ID: a3f2b1c9-4e2d-4c1a-b5f2-d9e3f1a2b3c4
Started: 2026-03-24T14:01:33+00:00
Open triggers: jellyfin_jellyfin (service down), sonarr_sonarr (service down)
Resolved: —

## Actions taken this session
- 14:03 docker_service_inspect(jellyfin_jellyfin) → [result preview 120 chars] [tier 1]
- 14:04 read_logs(jellyfin_jellyfin) → [result preview 120 chars] [tier 1]
```

Rules for rendering `## Current session`:

- `Open triggers`: list service names from `session.triggers - session.recovered`, each followed by `(service down)`. Comma-separated. If empty: `—`.
- `Resolved`: list service names from `session.recovered`. Comma-separated. If empty: `—`.
- `Started` is formatted as `session.started_at.strftime("%Y-%m-%dT%H:%M:%S+00:00")` (always UTC, always with offset, no microseconds).

Rules for rendering `## Actions taken this session`:

- Read the action log file synchronously from `_api_create`. See Section 8 for where this read happens.
- Filter to entries where `entry.get("session_id") == session.session_id` and `entry.get("event") == "action_taken"`.
- Take the last 20 matching entries (tail, not head).
- For each entry, format one line:
  ```
  - HH:MM tool_name(input_preview) → [outcome preview 120 chars] [tier N]
  ```
  Where:
  - `HH:MM` is parsed from `entry["ts"]` in UTC.
  - `tool_name` is `entry["tool"]`.
  - `input_preview` is a compact representation of `entry["input"]` — join key=value pairs for non-internal fields (excluding `agent_proposed_tier` and `agent_reasoning`), truncated to 60 characters total.
  - `outcome preview` is `(entry.get("outcome") or "")[:120].replace("\n", " ")`.
  - `tier N` is `entry.get("tier", "?")`.
- If no `action_taken` entries exist for this session: show `No actions taken yet.` as the sole line of the section.

**`build_system_prompt` implementation:**

```python
def build_system_prompt(
    session: "IncidentSession | None" = None,
    prior_actions: list[dict] | None = None,
) -> str:
    parts = [
        "You are a homelab sysadmin agent managing a Docker Swarm cluster.",
        INFRA_CONTEXT,
        TIER_RULES,
        BEHAVIOUR_RULES,
    ]
    if session is not None:
        parts.append(_render_session_section(session, prior_actions or []))
    return "\n\n".join(parts)
```

`prior_actions` is the pre-filtered list of `action_taken` log entries for this session (fetched by `_api_create` before calling `build_system_prompt`).

**`_render_session_section` helper (module-level in `prompts.py`):**

```python
def _render_session_section(
    session: "IncidentSession",
    prior_actions: list[dict],
) -> str:
    open_triggers = sorted(session.triggers - session.recovered)
    resolved = sorted(session.recovered)
    triggers_str = (
        ", ".join(f"{t} (service down)" for t in open_triggers)
        if open_triggers else "—"
    )
    resolved_str = ", ".join(resolved) if resolved else "—"
    started_str = session.started_at.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    lines = [
        "## Current session",
        f"Session ID: {session.session_id}",
        f"Started: {started_str}",
        f"Open triggers: {triggers_str}",
        f"Resolved: {resolved_str}",
        "",
        "## Actions taken this session",
    ]

    recent = prior_actions[-20:] if prior_actions else []
    if not recent:
        lines.append("No actions taken yet.")
    else:
        for entry in recent:
            ts_str = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                hhmm = ts.strftime("%H:%M")
            except (ValueError, TypeError):
                hhmm = "??:??"
            tool = entry.get("tool", "?")
            inp = entry.get("input", {})
            inp_parts = [
                f"{k}={v}" for k, v in inp.items()
                if k not in ("agent_proposed_tier", "agent_reasoning")
            ]
            inp_preview = ", ".join(inp_parts)[:60]
            outcome = (entry.get("outcome") or "")[:120].replace("\n", " ")
            tier = entry.get("tier", "?")
            lines.append(f"- {hhmm} {tool}({inp_preview}) → {outcome} [tier {tier}]")

    return "\n".join(lines)
```

---

### 8. `_api_create` integration

`_api_create` is responsible for assembling the system prompt on every API call. It must read the session log at call time so the system prompt always reflects the most recent actions, even after history trimming.

**Changes to `_api_create`:**

Replace:
```python
system = [{"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}}]
```

With:
```python
prior_actions = self._read_prior_actions()
system_text = build_system_prompt(session=self._current_session, prior_actions=prior_actions)
system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
```

Remove `self._system_prompt` from `HomelabAgent.__init__` — the system prompt is now built dynamically on every call. There is no cached `_system_prompt` attribute.

**`_read_prior_actions` method (new, in `HomelabAgent`):**

```python
def _read_prior_actions(self) -> list[dict]:
    if self._current_session is None:
        return []
    session_id = self._current_session.session_id
    entries: list[dict] = []
    try:
        with open(self._logger._path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if (
                        entry.get("session_id") == session_id
                        and entry.get("event") == "action_taken"
                    ):
                        entries.append(entry)
                except (json.JSONDecodeError, ValueError):
                    pass
    except FileNotFoundError:
        pass
    return entries[-20:]
```

This reads the log file synchronously in the async event loop. The log file is a local JSONL file and reads are fast enough that this does not warrant an executor. The method returns at most 20 entries (the cap applied here means `build_system_prompt` receives at most 20 entries and the slicing in `_render_session_section` is a no-op, but it is still correct to have the cap in both places for defensive safety).

**Prompt caching note:** Because the system prompt now includes session state and recent actions, it changes on (nearly) every API call within a session. This means prompt cache hits on the system block will be less frequent than today. This is acceptable — the session context is more valuable than cache savings.

---

### 9. `event_consumer` and session closing

`event_consumer` in `cli.py` gains session close logic. The full updated function:

```python
async def event_consumer(agent: HomelabAgent, event_queue: asyncio.Queue) -> None:
    while True:
        event = await event_queue.get()
        try:
            if event["type"] == "user_message":
                source = event.get("source", "cli")
                # Open or continue a session for this user message
                if agent._current_session is None:
                    agent._open_session(triggers=set())
                    agent._current_session.cli_messages += 1
                    await agent._logger.log_session_started(agent._current_session)
                else:
                    agent._current_session.cli_messages += 1
                response, cost_usd = await agent.chat(
                    event["data"]["message"], trigger=f"{source}:user_message"
                )
                # Close CLI-only sessions (no monitor triggers) after response
                if (
                    agent._current_session is not None
                    and not agent._current_session.triggers
                ):
                    await agent._close_session()
                if source != "cli":
                    if response:
                        await agent._slack.notify(response)
                    await _post_cost(agent, cost_usd)
            else:
                _, cost_usd = await agent.handle_event(event)
                await _post_cost(agent, cost_usd)
        except Exception as exc:
            console.print(f"\n[bold red]Event consumer error:[/bold red] {exc}")
        finally:
            event_queue.task_done()
```

Note: `_open_session` is synchronous (no `await`). It sets `self._current_session` immediately. `cli_messages` is incremented to 1 before `log_session_started` is awaited, so the opening log entry already reflects the correct count. The key constraint is that `log_session_started` must be awaited before `agent.chat` is called so the `session_started` event appears in the log before any `action_taken` events for the session.

**Simpler alternative for session opening in `event_consumer`:**

Factor session opening out of `handle_event` for monitor events, and have `event_consumer` handle all session open/close transitions explicitly. This keeps session lifecycle in one place. The implementation may choose this approach as long as the transition table in Section 2 is respected exactly.

**Summary of what `event_consumer` is responsible for:**

- Opening sessions for `user_message` events (no session active).
- Incrementing `cli_messages` for `user_message` events (session already active).
- Closing CLI-only sessions after response.
- Calling `_post_cost` after every event.

**Summary of what `handle_event` is responsible for:**

- Opening or extending sessions for `service_down` / `services_down` events.
- Adding to `recovered` and closing sessions for `service_recovered` events (after the chat call).

---

## Files changed

| File | Change |
|------|--------|
| `agent/agent/agent.py` | Add `IncidentSession` dataclass; add `_current_session` field to `HomelabAgent.__init__`; add `_open_session`, `_close_session`, `_read_prior_actions` methods; update `handle_event` for session lifecycle; update all `ActionLogger` call sites to pass `session_id`; remove `self._system_prompt`; update `_api_create` to call `build_system_prompt` dynamically; update all `ActionLogger` method signatures |
| `agent/agent/prompts.py` | Update `build_system_prompt` signature to `(session: IncidentSession \| None = None, prior_actions: list[dict] \| None = None)`; add `_render_session_section` helper; update `BEHAVIOUR_RULES` to replace `start_time` guidance with `session_id` guidance |
| `agent/agent/tools.py` | Remove `_slice_action_log`; add `_get_session_log(session_id: str) -> list[dict]`; update `_tool_write_incident_report` to use `session_id` parameter; update `TOOL_DEFINITIONS` for `write_incident_report` |
| `agent/cli.py` | Update `event_consumer` to manage session open/close for `user_message` events |
| `agent/tests/test_fix4_incident_sessions.py` | New — unit tests (see Tests section) |

---

## Tests

File: `agent/tests/test_fix4_incident_sessions.py`

All tests are unit tests using `pytest` and `unittest.mock`. No Docker, no Slack, no filesystem writes (use `tmp_path` where log files are needed).

### Test list

**Session opening — monitor events**

1. `test_service_down_no_session_opens_session`
   - Call `handle_event` with a `service_down` event when `_current_session` is `None`.
   - Assert `agent._current_session` is not `None`.
   - Assert `agent._current_session.triggers == {"jellyfin_jellyfin"}`.
   - Assert `log_session_started` was called once with the new session.

2. `test_services_down_no_session_opens_session_with_all_triggers`
   - Call `handle_event` with a `services_down` event (two services) when `_current_session` is `None`.
   - Assert `agent._current_session.triggers == {"jellyfin_jellyfin", "sonarr_sonarr"}`.
   - Assert `log_session_started` called once.

3. `test_service_down_active_session_adds_trigger`
   - Pre-set `agent._current_session` to a session with `triggers = {"jellyfin_jellyfin"}`.
   - Call `handle_event` with a `service_down` event for `"sonarr_sonarr"`.
   - Assert `agent._current_session.triggers == {"jellyfin_jellyfin", "sonarr_sonarr"}`.
   - Assert `log_session_started` was NOT called (session already open).

**Session opening — CLI events**

4. `test_user_message_no_session_opens_session`
   - Simulate a `user_message` event processed by `event_consumer` when `_current_session` is `None`.
   - Assert a new session is opened with `triggers == set()`.
   - Assert `log_session_started` called once.

5. `test_user_message_active_session_increments_cli_messages`
   - Pre-set `agent._current_session` to an existing CLI session with `cli_messages = 2`.
   - Simulate a `user_message` event.
   - Assert `agent._current_session.cli_messages == 3`.
   - Assert `log_session_started` NOT called.

**Session closing — recovery**

6. `test_service_recovered_closes_session_when_all_recovered`
   - Pre-set `agent._current_session` with `triggers = {"jellyfin_jellyfin"}`, `recovered = set()`.
   - Call `handle_event` with a `service_recovered` event for `"jellyfin_jellyfin"`.
   - Assert `agent._current_session` is `None` after the call.
   - Assert `log_session_ended` was called once.

7. `test_service_recovered_partial_recovery_keeps_session_open`
   - Pre-set `agent._current_session` with `triggers = {"jellyfin_jellyfin", "sonarr_sonarr"}`, `recovered = set()`.
   - Call `handle_event` with a `service_recovered` event for `"jellyfin_jellyfin"`.
   - Assert `agent._current_session` is not `None` (session still open).
   - Assert `agent._current_session.recovered == {"jellyfin_jellyfin"}`.
   - Assert `log_session_ended` NOT called.

**Session closing — CLI-only**

8. `test_cli_session_closes_after_response`
   - Set up `event_consumer` loop to process one `user_message` with an active CLI session (no triggers).
   - After `agent.chat(...)` returns, assert `_close_session` was called.
   - Assert `log_session_ended` called with `triggers = set()`, `recovered = set()`.

**session_id in log entries**

9. `test_action_log_entries_include_session_id`
   - Pre-set `agent._current_session` with a known `session_id`.
   - Mock `_api_create` to return a response that triggers one tier-1 tool call.
   - After the tool executes and `log_action_taken` is called, assert the log entry written to the log file contains `"session_id": <known_session_id>`.
   - Repeat check for `log_cost`, `log_plan_proposed` (using a tier-2 tool mock).

10. `test_log_entries_without_session_have_no_session_id`
    - Ensure `_current_session` is `None` when a call is made.
    - Assert the written log entry does NOT contain a `session_id` key.

**System prompt**

11. `test_build_system_prompt_with_session_includes_session_section`
    - Construct an `IncidentSession` with `session_id = "test-id"`, known `started_at`, `triggers = {"svc_a"}`, `recovered = set()`.
    - Call `build_system_prompt(session=session, prior_actions=[])`.
    - Assert the returned string contains `"Session ID: test-id"`.
    - Assert it contains `"Open triggers: svc_a (service down)"`.
    - Assert it contains `"No actions taken yet."`.

12. `test_build_system_prompt_with_actions_renders_action_list`
    - Construct a session and a list of 3 mock `action_taken` log entries with known `ts`, `tool`, `input`, `outcome`, `tier`.
    - Call `build_system_prompt(session=session, prior_actions=mock_entries)`.
    - Assert each entry appears as a `- HH:MM tool(...)` line.

13. `test_build_system_prompt_without_session_returns_original_prompt`
    - Call `build_system_prompt()` (no session argument).
    - Assert the result does NOT contain `"## Current session"`.
    - Assert the result does NOT contain `"## Actions taken this session"`.

14. `test_build_system_prompt_caps_at_20_actions`
    - Pass 25 mock `action_taken` entries.
    - Assert the rendered prompt contains exactly 20 action lines (count lines starting with `"- "`).

**`_get_session_log`**

15. `test_get_session_log_returns_only_matching_session_id`
    - Write a JSONL log file with 5 entries: 3 with `session_id = "target"`, 2 with `session_id = "other"`.
    - Call `_get_session_log("target")`.
    - Assert the result has length 3 and all entries have `session_id == "target"`.

16. `test_get_session_log_missing_file_returns_empty`
    - Call `_get_session_log("any-id")` with a nonexistent log path.
    - Assert the result is `[]`.

**`write_incident_report` with session_id**

17. `test_write_incident_report_uses_session_log`
    - Write a JSONL log file with 4 entries tagged with a known `session_id` and one entry with a different `session_id`.
    - Call `_tool_write_incident_report` with `session_id = <known>`.
    - Assert `_get_session_log` was called with `<known>`.
    - Assert the generated report markdown contains only the 4 matching log entries in its `## Action Log` section.

---

## Out of scope

- No changes to `MonitorDaemon` — it continues to emit raw `service_down`, `services_down`, and `service_recovered` events unchanged. Session management is entirely in `HomelabAgent` and `event_consumer`.
- No backwards compatibility for `start_time` in `write_incident_report` — the parameter is removed entirely. Any existing agent conversation mid-session will need to restart to pick up the new tool definition.
- No changes to the `/log` viewer in `cli.py` — session_id appears in log entries and is visible in raw log output, but no session-aware filtering UI is added in this fix.
- No changes to `config.yaml` or `config_schema.py` — sessions require no config.
- No `_trim_history` changes — history trimming continues to work as before. The session log in the system prompt is the complement, not a replacement.
- The single-message mode (`args.message` path in `amain`) calls `agent.chat()` directly rather than via `event_consumer`. Session management does not apply to this path — `_current_session` remains `None` throughout. This is acceptable for single-shot queries.
- No changes to `MonitorDaemon`'s own `ActionLogger.log` calls (`monitor_alert`, `monitor_recovered`) — these remain sessionless and are not filtered by `_get_session_log`.
- No persistent session recovery across agent restarts — if the agent process restarts mid-incident, `_current_session` starts as `None` and a new session opens on the next event. The old session is left without a `session_ended` entry in the log; this is acceptable.
- Fixing Fix 2 (Slack listener security), Fix 3 (concurrent shell gate), Fix 5 (Pydantic config), or Fix 6 (mute store) is out of scope here.
