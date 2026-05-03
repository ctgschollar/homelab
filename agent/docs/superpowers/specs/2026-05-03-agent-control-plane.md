# Agent Control Plane — Design Spec

## Goal

Introduce an `AgentController` layer that centralises all runtime state and routing decisions, replacing scattered coordination logic in `cli.py`. Built on top of it: transient issue grace periods, Slack control commands, `run_shell` safety hardening, and a command whitelist.

## Architecture

```
MonitorDaemon ──→ asyncio.Queue ──→ AgentController ──→ HomelabAgent (Claude)
SlackMessages ──→ asyncio.Queue ──→ AgentController ──→ (future: LocalLLMAgent)
```

The controller is the single point that answers: *should anything happen, and if so, who handles it?* `cli.py`'s `event_consumer` becomes a thin loop that feeds the controller. All coordination logic moves out of `cli.py`.

---

## Components

### 1. `agent/agent_base.py` — AgentBase Protocol

Minimal interface all agent implementations must satisfy. Enables future routing to local LLMs for info-gathering tasks.

```python
class AgentBase(Protocol):
    async def chat(self, message: str, trigger: str) -> tuple[str, float]: ...
    async def handle_event(self, event: dict) -> tuple[str, float]: ...
    async def cancel_all(self) -> None: ...
```

`HomelabAgent` implements all three. `cancel_all()` is new — cancels pending approvals and the active task.

### 2. `agent/controller.py` — AgentController

Owns all runtime state and routes events.

**Persistent state** (survives restart):
- `mode: Literal["monitor", "act"]` — stored in `config.yaml`. Last written value persists across restarts.
- `whitelist: set[str]` — stored in `whitelist.json`. Exact `run_shell` command strings pre-approved for tier 1.

**Transient state** (in-memory, resets on restart):
- `stopped: bool` — emergency brake. Always `False` on startup.
- `deferred: dict[str, DeferredAlert]` — pending investigations in their grace period.

**Agent registry:**
- `agents: dict[str, AgentBase]` — `{"default": HomelabAgent}` today. Extended for routing when local LLMs are introduced.

**Event routing logic:**

```python
async def handle_event(self, event: dict) -> None:
    if self.stopped:
        return
    if self.mode == "monitor":
        await self._notify_slack_only(event)
        return
    if event["source"] == "monitor" and event["type"] == "services_down":
        await self._defer(event)
        return
    await self.agents["default"].handle_event(event)
```

**`DeferredAlert` dataclass:**

```python
@dataclass
class DeferredAlert:
    alert_id: str
    event: dict
    services: list[str]
    timer_task: asyncio.Task
    slack_message_ref: tuple[str, str] | None  # (channel, ts)
    deferred_at: datetime
```

### 3. Grace Period

**Config** (`config.yaml`):
```yaml
monitor:
  poll_interval: 30
  grace_period_seconds: 600  # new — default 10 minutes
```

**On `services_down` event:** controller posts a Slack message with service names, countdown, and [Start Now] / [Ignore] buttons. Starts a `grace_period_seconds` asyncio timer.

**[Start Now]:** cancel timer, pass event to agent immediately.

**[Ignore]:** cancel timer, discard. No RAG entry.

**Timer expires, service still down:** pass event to agent.

**`service_recovered` during grace period:** cancel timer, post Slack notification, write a minimal incident to RAG directly (no agent loop):
```python
{
    "id": inc_id,
    "title": f"{service}-self-healed",
    "tags": ["recovery", "self-healed"],
    "inciting_incident": f"{service} degraded and recovered without agent intervention after {duration}s.",
    "resolution": "Cluster software self-healed the service. No agent action taken.",
    "tools_used": [],
}
```

The controller holds a direct reference to `IncidentRAG` (passed from `cli.py`) for this write.

**`service_recovered` when no grace period is active** (agent was already investigating): pass event to agent as before.

### 4. Slack Control Commands

Parsed in the FastAPI `/slack/events` handler before messages reach the event queue. `build_approval_app()` gains a `controller` parameter.

| Message | Effect | Slack response |
|---|---|---|
| `stop` | `stopped = True`, cancel all deferred timers, call `agent.cancel_all()` | "🛑 Stopped. All pending work cancelled. Type `start` to resume." |
| `start` | `stopped = False` | "✅ Resumed." |
| `queue` | Read `self.deferred` | Lists each pending alert: services, age, time remaining. "No pending investigations." if empty. |
| `mode monitor` | `mode = "monitor"`, write `config.yaml` | "👁 Monitor-only mode. I'll notify but not act." |
| `mode act` | `mode = "act"`, write `config.yaml` | "⚡ Act mode. I'll investigate and propose actions." |

Commands are matched case-insensitively against the start of the message text.

### 5. `HomelabAgent.cancel_all()`

New method implementing `AgentBase.cancel_all()`:

```python
async def cancel_all(self) -> None:
    self._pending.cancel_all("emergency stop")
    if self._active_task is not None:
        self._active_task.cancel()
```

`_active_task: asyncio.Task | None` is a new field on `HomelabAgent`. The event consumer (now inside the controller) sets it when it starts an event and clears it on completion:

```python
task = asyncio.create_task(self.agents["default"].handle_event(event))
self.agents["default"]._active_task = task
try:
    await task
finally:
    self.agents["default"]._active_task = None
```

`asyncio.CancelledError` propagates naturally through the agentic loop.

### 6. `run_shell` Tier Fix — `agent/safety.py`

In `_base_tier()`, collapse tier 2 out of agent discretion for `run_shell`:

```python
if configured == "agent":
    if tool_name == "run_shell" and agent_proposed_tier is not None and command is not None:
        tier = self._check_shell_command(command, agent_proposed_tier)
        # Only tier 1 (read-only) or tier 3 (explicit approval).
        # Tier 2 veto windows are never appropriate for shell commands.
        return 1 if tier == 1 else 3
    return agent_proposed_tier if agent_proposed_tier is not None else 3
```

### 7. Command Whitelist — `whitelist.json` + `agent/safety.py`

**Storage:** `whitelist.json` at the agent root — a plain JSON array of exact command strings:
```json
["docker service inspect jellyfin_jellyfin", "df -h"]
```

**`SafetyPolicy`** gains a `whitelist: set[str]` field. Loaded at startup via controller. The controller calls `agent._safety.update_whitelist(commands)` when the file changes.

**Tier resolution** — whitelist check after safe mode, before everything else:

```python
def resolve_tier(self, tool_name, target_resource, agent_proposed_tier,
                 agent_reasoning, command=None):
    # Safe mode always wins
    if self.global_safe_mode or self._resource_in_safe_mode(target_resource):
        return ResolvedTier(tier=3, safe_mode_active=True, ...)

    # Whitelisted commands run immediately
    if tool_name == "run_shell" and command and command in self.whitelist:
        return ResolvedTier(tier=1, safe_mode_active=False, original_tier=None,
                            agent_reasoning=None)

    # ... existing resolution logic unchanged
```

**[Approve + Whitelist] Slack button:** appears only on `run_shell` plan messages. On click: approves current execution AND appends exact command string to `whitelist.json`, reloads into `SafetyPolicy`.

---

## Files Changed / Created

| File | Change |
|---|---|
| `agent/agent_base.py` | **Create** — `AgentBase` Protocol |
| `agent/controller.py` | **Create** — `AgentController`, `DeferredAlert` |
| `agent/agent/agent.py` | **Modify** — add `cancel_all()`, `_active_task`, implement `AgentBase`; update `build_approval_app()` to accept controller |
| `agent/agent/safety.py` | **Modify** — `run_shell` tier fix; whitelist check in `resolve_tier`; `update_whitelist()` method |
| `agent/agent/slack.py` | **Modify** — add `notify_deferred_alert()` for grace period Slack messages with [Start Now]/[Ignore] buttons; add [Approve + Whitelist] button to `notify_plan()` for `run_shell` |
| `agent/agent/config_schema.py` | **Modify** — add `grace_period_seconds` to `MonitorConfig`; add `mode` to a new `ControllerConfig` section |
| `agent/cli.py` | **Modify** — wire up controller; slim down `event_consumer`; pass `rag` and `whitelist_path` to controller |
| `agent/config.yaml` | **Modify** — add `monitor.grace_period_seconds: 600`; add `controller.mode: "monitor"` (default is monitor-only; user switches to `"act"` via Slack command) |
| `agent/whitelist.json` | **Create** — empty JSON array `[]` |
| `tests/test_controller.py` | **Create** — unit tests for controller routing, grace period, commands |
| `tests/test_safety.py` | **Modify** — add tests for tier-2 collapse and whitelist resolution |

---

## Out of Scope

- Web UI (planned separately)
- Local LLM agent implementation (controller architecture supports it; wiring deferred)
- The command obfuscation and `write_file → run_shell` bypass vectors (separate hardening effort)
