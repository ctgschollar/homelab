# Fix 4: Incident Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace timestamp-based incident tracking with UUID-keyed `IncidentSession` objects that track trigger sets and session lifecycle events.

**Architecture:** An `IncidentSession` dataclass is added to `agent.py` to hold per-incident state (UUID, triggers, recovered set, CLI message count). `HomelabAgent` gains a single `_current_session` field and three methods (`_open_session`, `_close_session`, `_read_prior_actions`) that manage session lifecycle in response to monitor and CLI events. Session state is injected into the system prompt on every API call and propagated as `session_id` on every action log entry, replacing the fragile `start_time`-based log slice used by `write_incident_report`.

**Tech Stack:** Python 3.11, uuid, asyncio, pytest, pytest-asyncio

---

## Task 1: Add `IncidentSession` dataclass to `agent/agent/agent.py`

**Write tests first** in `agent/tests/test_incident_session.py`, then add the dataclass.

### Step 1.1 — Write tests

- [ ] Create `agent/tests/test_incident_session.py`
- [ ] Test: `IncidentSession` can be instantiated with all required fields
- [ ] Test: `session_id` is a non-empty string
- [ ] Test: `started_at` is a timezone-aware `datetime`
- [ ] Test: `triggers` and `recovered` are `set[str]`
- [ ] Test: `cli_messages` defaults to `0`
- [ ] Test: `triggers - recovered` correctly identifies open (unresolved) triggers

Run tests (should fail):
```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_incident_session.py -v
```

### Step 1.2 — Add the dataclass

In `/home/chris/src/homelab/agent/agent/agent.py`, add to the top-level imports:

```python
import uuid
from dataclasses import dataclass, field
```

Add after the imports block, before the `ActionLogger` class:

```python
@dataclass
class IncidentSession:
    session_id: str                    # UUID4, e.g. "a3f2b1c9-4e2d-..."
    started_at: datetime               # set by Python at session open; never guessed
    triggers: set[str]                 # service names from monitor events
    recovered: set[str]                # subset of triggers that have since recovered
    cli_messages: int                  # count of CLI/Slack user_message events this session
```

### Step 1.3 — Run tests (should pass)

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_incident_session.py -v
```

### Step 1.4 — Commit

```
git -C /home/chris/src/homelab add agent/agent/agent.py agent/tests/test_incident_session.py
git -C /home/chris/src/homelab commit -m "feat: add IncidentSession dataclass"
```

---

## Task 2: Add `log_session_started` and `log_session_ended` to `ActionLogger`; add `session_id` param to all existing log methods

**Write tests first** in `agent/tests/test_action_logger_sessions.py`.

### Step 2.1 — Write tests

- [ ] Create `agent/tests/test_action_logger_sessions.py`
- [ ] Test: `log_session_started` writes a record with `event = "session_started"`, `session_id`, `triggers` (sorted list), `started_at` (ISO string), and `ts`
- [ ] Test: `log_session_ended` writes a record with `event = "session_ended"`, `session_id`, `triggers`, `recovered`, `duration_seconds` (int >= 0), `cli_messages`, and `ts`
- [ ] Test: `log_session_ended` `duration_seconds` is non-negative and approximately correct
- [ ] Test: `log` with `session_id=None` does NOT add a `session_id` key to the written record
- [ ] Test: `log` with `session_id="abc-123"` adds `"session_id": "abc-123"` to the written record
- [ ] Test: `log_action_taken` passes `session_id` through to `log` when provided
- [ ] Test: `log_cost` passes `session_id` through to `log` when provided
- [ ] Test: `log_plan_proposed` passes `session_id` through to `log` when provided
- [ ] Test: `log_plan_approved` passes `session_id` through to `log` when provided
- [ ] Test: `log_plan_cancelled` passes `session_id` through to `log` when provided
- [ ] Test: `log_tier_reasoning` passes `session_id` through to `log` when provided

Run tests (should fail):
```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_action_logger_sessions.py -v
```

### Step 2.2 — Update `ActionLogger.log`

In `/home/chris/src/homelab/agent/agent/agent.py`, update `ActionLogger.log`:

```python
async def log(self, record: dict, session_id: str | None = None) -> None:
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    if session_id is not None:
        record["session_id"] = session_id
    async with self._lock:
        with open(self._path, "a") as f:
            f.write(json.dumps(record) + "\n")
```

### Step 2.3 — Add `session_id` parameter to all existing `ActionLogger` methods

Update each of these methods to accept `session_id: str | None = None` and pass it through to `self.log(...)`:

- `log_action_taken`
- `log_plan_proposed`
- `log_plan_approved`
- `log_plan_cancelled`
- `log_tier_reasoning`
- `log_cost`

### Step 2.4 — Add `log_session_started` and `log_session_ended`

Add to `ActionLogger` in `/home/chris/src/homelab/agent/agent/agent.py`:

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

### Step 2.5 — Run tests (should pass)

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_action_logger_sessions.py -v
```

### Step 2.6 — Commit

```
git -C /home/chris/src/homelab add agent/agent/agent.py agent/tests/test_action_logger_sessions.py
git -C /home/chris/src/homelab commit -m "feat: add session_id to ActionLogger; add log_session_started/ended"
```

---

## Task 3: Add session open/close logic to `HomelabAgent`

**Write tests first** in `agent/tests/test_homelab_agent_sessions.py`.

### Step 3.1 — Write tests

- [ ] Create `agent/tests/test_homelab_agent_sessions.py`
- [ ] Test: `HomelabAgent.__init__` sets `_current_session = None`
- [ ] Test: `_open_session(triggers={"svc_a"})` creates an `IncidentSession` with correct fields and assigns it to `_current_session`
- [ ] Test: `_open_session` returns the new session
- [ ] Test: `_open_session` generates a valid UUID4 for `session_id` (matches UUID4 pattern)
- [ ] Test: `_open_session` sets `started_at` to a timezone-aware UTC datetime close to now
- [ ] Test: `_close_session` sets `_current_session = None`
- [ ] Test: `_close_session` calls `log_session_ended` with the session
- [ ] Test: `_close_session` when `_current_session is None` is a no-op (no error, no log call)

Run tests (should fail):
```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_homelab_agent_sessions.py -v
```

### Step 3.2 — Add `_current_session` field to `HomelabAgent.__init__`

In `/home/chris/src/homelab/agent/agent/agent.py`, in `HomelabAgent.__init__`, add:

```python
self._current_session: IncidentSession | None = None
```

Also remove `self._system_prompt = build_system_prompt()` — the system prompt will be built dynamically in `_api_create` (see Task 6).

### Step 3.3 — Add `_open_session` and `_close_session` methods

Add to `HomelabAgent` in `/home/chris/src/homelab/agent/agent/agent.py`:

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

async def _close_session(self) -> None:
    session = self._current_session
    if session is None:
        return
    self._current_session = None
    await self._logger.log_session_ended(session)
```

### Step 3.4 — Run tests (should pass)

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_homelab_agent_sessions.py -v
```

### Step 3.5 — Commit

```
git -C /home/chris/src/homelab add agent/agent/agent.py agent/tests/test_homelab_agent_sessions.py
git -C /home/chris/src/homelab commit -m "feat: add _current_session, _open_session, _close_session to HomelabAgent"
```

---

## Task 4: Propagate `session_id` to all action log call sites in `HomelabAgent`

**Write tests first.**

### Step 4.1 — Write tests

Add to `agent/tests/test_homelab_agent_sessions.py` (or a new file `agent/tests/test_session_id_propagation.py`):

- [ ] Test: When `_current_session` is set with a known `session_id`, a `log_action_taken` call from within `_run_loop` (via a mocked tier-1 tool response) writes a record containing `"session_id": <known_session_id>` to the log file
- [ ] Test: When `_current_session` is set, `log_cost` written at end of `_run_loop` contains `"session_id"`
- [ ] Test: When `_current_session` is set, `log_plan_proposed` (triggered by a mocked tier-2 tool response) contains `"session_id"`
- [ ] Test: When `_current_session` is None, `log_action_taken` does NOT contain `"session_id"` key

Run tests (should fail):
```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_session_id_propagation.py -v
```

### Step 4.2 — Update all `ActionLogger` call sites in `HomelabAgent`

The pattern at every call site is:

```python
session_id=self._current_session.session_id if self._current_session else None
```

Update the following call sites in `/home/chris/src/homelab/agent/agent/agent.py`:

- `_run_loop` → `self._logger.log_cost(..., session_id=...)`
- `_handle_tool_calls` (tier-reasoning block) → `self._logger.log_tier_reasoning(..., session_id=...)`
- `_handle_tool_calls` → inner `_exec_tier1` closure → `self._logger.log_action_taken(..., session_id=...)`
- `_handle_approval_flow` → `self._logger.log_plan_proposed(..., session_id=...)`
- `_handle_approval_flow` → `self._logger.log_plan_cancelled(..., session_id=...)`
- `_handle_approval_flow` → `self._logger.log_plan_approved(..., session_id=...)`
- `_handle_approval_flow` → `self._logger.log_action_taken(..., session_id=...)` (post-execution)

Note: `MonitorDaemon` calls `self._logger.log(...)` directly for `monitor_alert` and `monitor_recovered` events. These do NOT receive a `session_id` — leave them unchanged.

### Step 4.3 — Run tests (should pass)

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_session_id_propagation.py -v
```

### Step 4.4 — Commit

```
git -C /home/chris/src/homelab add agent/agent/agent.py agent/tests/test_session_id_propagation.py
git -C /home/chris/src/homelab commit -m "feat: propagate session_id to all action log call sites"
```

---

## Task 5: Update `handle_event` and `event_consumer` for full session lifecycle

**Write tests first** in `agent/tests/test_fix4_incident_sessions.py`.

### Step 5.1 — Write tests

These are the 17 tests specified in the spec. Create `agent/tests/test_fix4_incident_sessions.py`:

**Session opening — monitor events**

- [ ] `test_service_down_no_session_opens_session` — `handle_event` with `service_down` when no session → `_current_session.triggers == {"jellyfin_jellyfin"}` and `log_session_started` called once
- [ ] `test_services_down_no_session_opens_session_with_all_triggers` — `handle_event` with `services_down` (two services) when no session → `triggers == {"jellyfin_jellyfin", "sonarr_sonarr"}` and `log_session_started` called once
- [ ] `test_service_down_active_session_adds_trigger` — pre-set session with `triggers = {"jellyfin_jellyfin"}`, call `handle_event` with `service_down` for `"sonarr_sonarr"` → `triggers == {"jellyfin_jellyfin", "sonarr_sonarr"}` and `log_session_started` NOT called

**Session opening — CLI events**

- [ ] `test_user_message_no_session_opens_session` — `event_consumer` processes `user_message` when no session → new session opened with `triggers == set()` and `log_session_started` called once
- [ ] `test_user_message_active_session_increments_cli_messages` — pre-set CLI session with `cli_messages = 2`, process `user_message` → `cli_messages == 3` and `log_session_started` NOT called

**Session closing — recovery**

- [ ] `test_service_recovered_closes_session_when_all_recovered` — session with `triggers = {"jellyfin_jellyfin"}`, call `handle_event` with `service_recovered` → `_current_session is None` and `log_session_ended` called once
- [ ] `test_service_recovered_partial_recovery_keeps_session_open` — session with `triggers = {"jellyfin_jellyfin", "sonarr_sonarr"}`, recover `"jellyfin_jellyfin"` → session still open, `recovered == {"jellyfin_jellyfin"}`, `log_session_ended` NOT called
- [ ] `test_service_recovered_spurious_ignored` — session with `triggers = {"jellyfin_jellyfin"}`, recover `"sonarr_sonarr"` (not in triggers) → session unchanged, `recovered` still empty, `log_session_ended` NOT called

**Session closing — CLI-only**

- [ ] `test_cli_session_closes_after_response` — `event_consumer` processes `user_message` with active CLI session (no triggers) → after `agent.chat(...)` returns, `_close_session` called, `log_session_ended` called with `triggers=set()`, `recovered=set()`

**session_id in log entries**

- [ ] `test_action_log_entries_include_session_id` — pre-set `_current_session` with known `session_id`, mock `_api_create` returning a tier-1 tool call, assert `log_action_taken` record contains `"session_id": <known>`; also check `log_cost` and `log_plan_proposed` (tier-2 mock)
- [ ] `test_log_entries_without_session_have_no_session_id` — `_current_session` is None, assert written record does NOT contain `session_id` key

**System prompt**

- [ ] `test_build_system_prompt_with_session_includes_session_section` — construct `IncidentSession`, call `build_system_prompt(session=session, prior_actions=[])`, assert contains `"Session ID: test-id"`, `"Open triggers: svc_a (service down)"`, `"No actions taken yet."`
- [ ] `test_build_system_prompt_with_actions_renders_action_list` — 3 mock `action_taken` entries → each appears as `- HH:MM tool(...)` line
- [ ] `test_build_system_prompt_without_session_returns_original_prompt` — no session arg → result does NOT contain `"## Current session"` or `"## Actions taken this session"`
- [ ] `test_build_system_prompt_caps_at_20_actions` — 25 mock entries → prompt contains exactly 20 action lines (lines starting with `"- "` in the actions section)

**`_get_session_log`**

- [ ] `test_get_session_log_returns_only_matching_session_id` — JSONL log with 3 entries tagged `"target"` and 2 tagged `"other"`, call `_get_session_log("target")` → length 3, all with correct `session_id`
- [ ] `test_get_session_log_missing_file_returns_empty` — call with nonexistent log path → `[]`

**`write_incident_report` with session_id**

- [ ] `test_write_incident_report_uses_session_log` — JSONL log with 4 entries tagged known `session_id` and 1 with another, call `_tool_write_incident_report` with that `session_id` → `_get_session_log` called with correct id; report `## Action Log` contains only 4 matching entries

Run tests (should fail):
```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix4_incident_sessions.py -v
```

### Step 5.2 — Update `handle_event` in `/home/chris/src/homelab/agent/agent/agent.py`

Replace the existing `handle_event` method body with:

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

    # Build message for each event type (preserve existing message construction logic)
    if event_type == "services_down":
        services = data.get("services", [])
        lines = []
        for s in services:
            lines.append(
                f"  - {s['service']}: {s['running']}/{s['desired']} replicas"
                + (f" (error: {s['last_error']})" if s.get("last_error") else "")
            )
        svc_list = "\n".join(lines)
        msg = (
            f"[MONITOR ALERT] {len(services)} service(s) are degraded:\n{svc_list}\n"
            "Investigate the common root cause and take appropriate action per your autonomy tier rules."
        )
        trigger = "monitor:services_down"
    elif event_type == "service_down":
        svc = data["service"]
        running = data["running"]
        desired = data["desired"]
        err = data.get("last_error", "none")
        msg = (
            f"[MONITOR ALERT] Service {svc} is degraded: {running}/{desired} replicas running. "
            f"Last error: {err}. Investigate and take appropriate action per your autonomy tier rules."
        )
        trigger = "monitor:service_down"
    elif event_type == "service_recovered":
        svc = data["service"]
        dur = data.get("down_duration_seconds", 0)
        msg = f"[MONITOR] Service {svc} has recovered after {dur}s. Notify Slack."
        trigger = "monitor:service_recovered"
    else:
        msg = str(data)
        trigger = f"{event.get('source', 'unknown')}:{event_type}"

    response_text, cost = await self.chat(msg, trigger=trigger)

    # Close session after recovery chat if all triggers are resolved
    if self._current_session and self._current_session.triggers:
        if self._current_session.recovered == self._current_session.triggers:
            await self._close_session()

    return response_text, cost
```

### Step 5.3 — Update `event_consumer` in `/home/chris/src/homelab/agent/cli.py`

Replace the existing `event_consumer` function with:

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

### Step 5.4 — Run all session lifecycle tests (should pass)

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix4_incident_sessions.py -v
```

### Step 5.5 — Commit

```
git -C /home/chris/src/homelab add agent/agent/agent.py agent/cli.py agent/tests/test_fix4_incident_sessions.py
git -C /home/chris/src/homelab commit -m "feat: implement session lifecycle in handle_event and event_consumer"
```

---

## Task 6: Update system prompt rendering with active session info

**Write tests first** (tests 11–14 from Task 5 cover this; write them in `test_fix4_incident_sessions.py` as specified above or in a dedicated `agent/tests/test_prompts_session.py`).

### Step 6.1 — Tests (already specified in Task 5)

The four system prompt tests (`test_build_system_prompt_*`) are part of `test_fix4_incident_sessions.py`. If running them in isolation:

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix4_incident_sessions.py -k "prompt" -v
```

### Step 6.2 — Update `build_system_prompt` in `/home/chris/src/homelab/agent/agent/prompts.py`

Add import at top of file:

```python
from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import IncidentSession
```

Add the `_render_session_section` helper (module-level):

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

Update `build_system_prompt` signature and body:

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

Also update `BEHAVIOUR_RULES` in `/home/chris/src/homelab/agent/agent/prompts.py` — in the `## Incident Reports` section, replace:

```
- Use `start_time` = the actual timestamp when the event or request was first received **in the current session** (today's date). Never use a historical or placeholder date.
```

with:

```
- Use `session_id` from the `## Current session` section of the system prompt.
  Never fabricate a session_id. If no session section is present, do not call write_incident_report.
```

### Step 6.3 — Add `_read_prior_actions` to `HomelabAgent` and update `_api_create`

Add to `HomelabAgent` in `/home/chris/src/homelab/agent/agent/agent.py`:

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

In `_api_create`, replace:

```python
system = [{"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}}]
```

with:

```python
prior_actions = self._read_prior_actions()
system_text = build_system_prompt(session=self._current_session, prior_actions=prior_actions)
system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
```

Also update the `build_system_prompt` import at the top of `agent.py` to match the new signature (it is already imported from `.prompts` — no change needed to the import line itself).

### Step 6.4 — Update tools in `/home/chris/src/homelab/agent/agent/tools.py`

**Remove `_slice_action_log` and add `_get_session_log`:**

Remove the existing `_slice_action_log` method and replace with:

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

**Update `TOOL_DEFINITIONS` for `write_incident_report`:**

- Remove the `start_time` property from `input_schema.properties`
- Remove `"start_time"` from `input_schema.required`
- Add `session_id` property:
  ```python
  "session_id": {
      "type": "string",
      "description": (
          "The session ID for this incident, provided in the system prompt "
          "under '## Current session'. Pass it exactly as shown."
      ),
  },
  ```
- Add `"session_id"` to `input_schema.required`
- Full required list: `["title", "tags", "inciting_incident", "resolution", "tools_used", "session_id"]`
- Remove `start_time` reference from the tool description text

**Update `_tool_write_incident_report`:**

Replace the `raw_start` / `start_time` / `start_time_valid` block and the `log_entries` line with:

```python
session_id = inp["session_id"]
log_entries = self._get_session_log(session_id)
```

Update the return string to use `session_id` in place of `start_time`:

```python
return (
    f"INC-{num:04d} written and committed: `reports/{filename}` "
    f"({len(log_entries)} action log entries, session: {session_id}, tags: {tags_str}).\n{git_result}"
)
```

### Step 6.5 — Run the full test suite

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
```

### Step 6.6 — Commit

```
git -C /home/chris/src/homelab add agent/agent/agent.py agent/agent/prompts.py agent/agent/tools.py agent/tests/test_fix4_incident_sessions.py
git -C /home/chris/src/homelab commit -m "feat: inject session context into system prompt; replace start_time with session_id in write_incident_report"
```

---

## Final verification

Run the full test suite one more time to confirm all tasks pass together:

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
```

## Files changed summary

| File | Changes |
|------|---------|
| `agent/agent/agent.py` | Add `IncidentSession` dataclass; add `uuid` + `dataclasses` imports; update `ActionLogger.log`, all `ActionLogger` methods, and add `log_session_started`/`log_session_ended`; add `_current_session` to `HomelabAgent.__init__`; remove `self._system_prompt`; add `_open_session`, `_close_session`, `_read_prior_actions`; update `handle_event`; update all logger call sites to pass `session_id` |
| `agent/agent/prompts.py` | Update `build_system_prompt` signature; add `_render_session_section`; update `BEHAVIOUR_RULES` incident report guidance |
| `agent/agent/tools.py` | Remove `_slice_action_log`; add `_get_session_log`; update `_tool_write_incident_report`; update `TOOL_DEFINITIONS` for `write_incident_report` |
| `agent/cli.py` | Update `event_consumer` to manage session open/close for `user_message` events |
| `agent/tests/test_incident_session.py` | New — dataclass field tests |
| `agent/tests/test_action_logger_sessions.py` | New — `ActionLogger` session method tests |
| `agent/tests/test_homelab_agent_sessions.py` | New — `HomelabAgent` session open/close tests |
| `agent/tests/test_session_id_propagation.py` | New — session_id propagation to log entries |
| `agent/tests/test_fix4_incident_sessions.py` | New — full integration tests (17 tests per spec) |
