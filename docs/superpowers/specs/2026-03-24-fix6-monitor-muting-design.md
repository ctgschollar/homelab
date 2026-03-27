# Fix 6 — Monitor Muting

**Date:** 2026-03-24
**Scope:** `agent/` directory only
**PR:** standalone

---

## Problem

`MonitorConfig.watched_stacks` is misleading — the health check in `MonitorDaemon._poll` already
iterates over all Docker Swarm services via `client.services.list()` without any stack filtering.
The field exists in the config but has no effect on runtime behaviour.

More critically, there is no mechanism to suppress a stack that the agent cannot recover. If
`jellyfin` is persistently degraded due to a hardware constraint (e.g. the GPU node is offline),
`MonitorDaemon` emits a new `services_down` event on every poll cycle, triggering a full agent
loop each time — consuming API budget indefinitely and spamming Slack.

The desired behaviour is:

- Monitor every replicated service in the swarm (already the case — just remove the dead config).
- Any degraded service triggers autonomous recovery via the agent loop.
- After repeated failed attempts, the agent proposes muting the stack to Slack; the operator
  approves; the muted stack is silently skipped on future polls.
- When a muted stack's services return to healthy, the mute is lifted automatically and normal
  monitoring resumes.

---

## Design

### 1. Remove `watched_stacks`

**`agent/agent/config_schema.py`** — remove the `watched_stacks` field from `MonitorConfig`:

```python
class MonitorConfig(BaseModel):
    poll_interval: int
    # watched_stacks removed — monitor covers all swarm services
```

The field currently has no runtime effect (confirmed by reading `MonitorDaemon._poll`, which calls
`client.services.list()` with no label filter). Removing it eliminates the misleading implication
that the list controls which stacks are watched.

**`agent/config.yaml`** — remove the `watched_stacks` block from the `monitor` section:

```yaml
monitor:
  poll_interval: 30
```

Remove these lines entirely:
```yaml
  watched_stacks:
  - traefik
  - monitoring
  - postgres
  - coredns
```

---

### 2. MuteConfig (`config_schema.py` + `config.yaml`)

Add a new top-level config section to hold mute store configuration.

**`agent/agent/config_schema.py`** — add `MuteConfig` class and `mute` field on `AgentConfig`:

```python
class MuteConfig(BaseModel):
    path: str = "./muted_stacks.json"
```

Add to `AgentConfig`:

```python
class AgentConfig(BaseSettings):
    # ... existing fields ...
    mute: MuteConfig = MuteConfig()
```

The field has a default so existing `config.yaml` files without a `mute` section continue to work.

**`agent/config.yaml`** — add the `mute` section after the `monitor` section:

```yaml
# -----------------------------------------------------------------------
# Mute store
# -----------------------------------------------------------------------
mute:
  path: "./muted_stacks.json"
```

---

### 3. `MuteEntry` and `MuteStore` (`monitor.py`)

Both classes live in `agent/agent/monitor.py`. Add them after the existing imports and before
`MonitorDaemon`.

**Imports to add at the top of `monitor.py`:**

```python
import json
from dataclasses import dataclass
```

`asyncio` and `datetime`/`timezone` are already imported.

**`MuteEntry` dataclass:**

```python
@dataclass
class MuteEntry:
    stack: str
    muted_at: datetime   # UTC, stored as ISO string in JSON
    reason: str
```

**`MuteStore` class:**

```python
class MuteStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._entries: dict[str, MuteEntry] = {}
        self._load()

    def _load(self) -> None:
        """Read JSON file at startup. Missing file → empty store."""
        try:
            with open(self._path) as f:
                raw: dict = json.load(f)
            for stack, data in raw.items():
                self._entries[stack] = MuteEntry(
                    stack=stack,
                    muted_at=datetime.fromisoformat(data["muted_at"]),
                    reason=data["reason"],
                )
        except FileNotFoundError:
            pass

    def _write(self) -> None:
        """Write current state to JSON file. Must be called with _lock held."""
        data = {
            stack: {
                "muted_at": entry.muted_at.isoformat(),
                "reason": entry.reason,
            }
            for stack, entry in self._entries.items()
        }
        with open(self._path, "w") as f:
            json.dump(data, f, indent=2)

    async def mute(self, stack: str, reason: str) -> None:
        async with self._lock:
            self._entries[stack] = MuteEntry(
                stack=stack,
                muted_at=datetime.now(timezone.utc),
                reason=reason,
            )
            self._write()

    async def unmute(self, stack: str) -> None:
        async with self._lock:
            self._entries.pop(stack, None)
            self._write()

    def is_muted(self, stack: str) -> bool:
        return stack in self._entries

    def all_muted(self) -> list[MuteEntry]:
        return list(self._entries.values())
```

**JSON file format** (`muted_stacks.json`):

```json
{
  "jellyfin": {
    "muted_at": "2026-03-24T14:03:00+00:00",
    "reason": "GPU node dks01 offline — placement constraint unsatisfiable"
  },
  "postgres": {
    "muted_at": "2026-03-24T15:22:00+00:00",
    "reason": "LINSTOR volume attachment failing, storage team investigating"
  }
}
```

The lock protects `_write` only. `is_muted` and `all_muted` read `self._entries` directly — this
is safe because dict reads are thread-safe in CPython and these methods are called from the
asyncio event loop (single-threaded), never from a thread pool.

---

### 4. `MonitorDaemon` integration

**Stack name extraction:**

Services are named `<stack>_<service>` (e.g. `jellyfin_jellyfin`, `traefik_traefik`,
`monitoring_prometheus`). Extract the stack name by splitting on the first `_`:

```python
stack_name = service_name.split("_", 1)[0]
```

Examples:
- `"jellyfin_jellyfin"` → `"jellyfin"`
- `"traefik_traefik"` → `"traefik"`
- `"monitoring_prometheus"` → `"monitoring"`

Stack name extraction: `stack_name = service_name.split('_', 1)[0]`. If the service name contains no underscore, the entire service name is used as the stack name. Standalone (non-stack) services are therefore only muteable by their full name. This is acceptable since the mute store is keyed by the exact stack name provided to `mute_stack`.

**`MonitorDaemon.__init__` signature change:**

```python
def __init__(
    self,
    config: AgentConfig,
    event_queue: asyncio.Queue,
    action_logger: ActionLogger,
    mute_store: MuteStore,
) -> None:
    self._poll_interval: int = config.monitor.poll_interval
    self._docker_socket: str = config.docker.socket
    self._event_queue = event_queue
    self._logger = action_logger
    self._mute_store = mute_store
    self._down_since: dict[str, datetime] = {}
```

**`_check_once` changes:**

The existing `_check_once` method has two code paths: the degraded branch (service down) and the
recovery branch (was down, now up). Both need mute-awareness.

**Degraded path** — in the `if running < desired:` branch, after recording `_down_since` and
before appending to `newly_down`, insert a mute check:

```python
if running < desired:
    if name not in self._down_since:
        self._down_since[name] = now
        stack_name = name.split("_", 1)[0]
        if self._mute_store.is_muted(stack_name):
            pass  # silently skip — no log, no queue, no Slack
        else:
            await self._logger.log({
                "event": "monitor_alert",
                "service": name,
                "running": running,
                "desired": desired,
                "last_error": last_error,
            })
            newly_down.append({
                "service": name,
                "running": running,
                "desired": desired,
                "last_error": last_error,
            })
```

For services that are already in `_down_since` (repeat polls while still degraded), the existing
code does nothing — this behaviour is unchanged. The mute check only suppresses the *first*
detection event, but since `_down_since` tracks state, repeated polls for an already-known-down
service never re-trigger an alert regardless of muting.

**Recovery path** — in the `else:` branch (service is now healthy), after computing `duration`,
check if this stack was muted and handle auto-unmute:

```python
else:
    if name in self._down_since:
        down_since = self._down_since.pop(name)
        duration = int((now - down_since).total_seconds())
        stack_name = name.split("_", 1)[0]
        was_muted = self._mute_store.is_muted(stack_name)

        if was_muted:
            await self._mute_store.unmute(stack_name)
            await self._logger.log({
                "event": "stack_unmuted",
                "stack": stack_name,
                "reason": "auto: service recovered",
                # "session_id" field will be added by Fix 4
                "ts": now.isoformat(),
            })
            await self._slack.notify(
                f"*{stack_name}* has recovered and monitoring has resumed "
                f"(was muted, down for {duration}s)."
            )
        else:
            await self._logger.log({
                "event": "monitor_recovered",
                "service": name,
                "down_duration_seconds": duration,
            })
            await self._event_queue.put({
                "source": "monitor",
                "type": "service_recovered",
                "data": {
                    "service": name,
                    "down_duration_seconds": duration,
                },
                "timestamp": now,
            })
```

Note: `MonitorDaemon` does not currently hold a reference to the Slack client. It must receive
one to send the unmute notification. See the wiring section below.

**`MonitorDaemon` Slack client wiring:**

`MonitorDaemon.__init__` needs to accept `slack_client` in addition to `mute_store`. The Slack
client is a `SlackClient` instance (from `agent/agent/slack.py`). Since `MonitorDaemon` only calls
`notify()`, the type annotation can use `TYPE_CHECKING`:

```python
if TYPE_CHECKING:
    from .agent import ActionLogger
    from .config_schema import AgentConfig
    from .slack import SlackClient

class MonitorDaemon:
    def __init__(
        self,
        config: AgentConfig,
        event_queue: asyncio.Queue,
        action_logger: ActionLogger,
        mute_store: MuteStore,
        slack_client: SlackClient,
    ) -> None:
        ...
        self._mute_store = mute_store
        self._slack = slack_client
```

---

### 5. `mute_stack` tool (`tools.py`)

**Add to `TOOL_DEFINITIONS` in `agent/agent/tools.py`:**

Append as the last entry in the list (maintaining the existing ordering pattern):

```python
{
    "name": "mute_stack",
    "description": (
        "Mute a stack that cannot be recovered. "
        "The stack will be silently skipped on all future monitor polls until it recovers on its own, "
        "at which point it is automatically unmuted and normal monitoring resumes. "
        "Use this after repeated failed recovery attempts — do not retry indefinitely."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "stack": {
                "type": "string",
                "description": "The stack name to mute (e.g. 'jellyfin', 'traefik').",
            },
            "reason": {
                "type": "string",
                "description": "Why the stack cannot be recovered. Include root cause and what was attempted.",
            },
        },
        "required": ["stack", "reason"],
    },
},
```

**Add `"mute_stack": 3` to `_DEFAULT_TIERS` in `agent/agent/safety.py`:**

```python
_DEFAULT_TIERS: dict[str, int] = {
    # ... existing entries ...
    "mute_stack": 3,
}
```

**Add `mute_stack: 3` to `tool_tiers` in `agent/config.yaml`:**

```yaml
tool_tiers:
    # ... existing entries ...
    mute_stack:              3       # irreversible until auto-recovery; always requires approval
```

**`ToolExecutor` changes in `agent/agent/tools.py`:**

`ToolExecutor.__init__` receives `mute_store` and `action_logger` parameters:

```python
class ToolExecutor:
    def __init__(
        self,
        config: "AgentConfig",
        slack_client: Any,
        mute_store: "MuteStore",
        action_logger: "ActionLogger",
    ) -> None:
        # ... existing assignments ...
        self._mute_store = mute_store
        self._logger = action_logger
```

The `MuteStore` and `ActionLogger` imports are added under `TYPE_CHECKING` to avoid circular imports:

```python
if TYPE_CHECKING:
    from .agent import ActionLogger
    from .config_schema import AgentConfig
    from .monitor import MuteStore
```

Add tool implementation method:

```python
async def _tool_mute_stack(self, inp: dict) -> str:
    stack = inp["stack"]
    reason = inp["reason"]
    await self._mute_store.mute(stack, reason)
    await self._logger.log({
        "event": "stack_muted",
        "stack": stack,
        "reason": reason,
        # "session_id" field will be added by Fix 4
    })
    return f"Stack '{stack}' muted. It will be skipped on future monitor polls until it recovers."
```

The tier-3 approval gate in `_handle_approval_flow` (in `agent.py`) is already in place — the
`mute_stack` tool goes through the standard tier-3 flow (post to Slack, wait indefinitely for
explicit APPROVE) before `_tool_mute_stack` is called.

---

### 6. `BEHAVIOUR_RULES` update (`prompts.py`)

**`agent/agent/prompts.py`** — add a `## Monitor Muting` subsection to `BEHAVIOUR_RULES`:

```
## Monitor Muting

- When a monitor alert arrives for a stack, investigate and attempt recovery.
- After 3 consecutive failed recovery attempts for the same stack, call `mute_stack`
  with a clear explanation of the root cause and what was attempted.
- Do not retry a stack indefinitely. Escalate to muting if recovery is not progressing.
- `mute_stack` is tier 3 and requires explicit operator approval before taking effect.
- When a muted stack auto-recovers, a `stack_unmuted` log entry is written and Slack
  is notified automatically — no agent action required.
```

The count of "3 consecutive failed recovery attempts" is guidance to the model, not a code-level
counter. The agent infers this from the conversation context and action log summary (see Fix 4).

---

### 7. `/status` REPL command (`cli.py`)

**`agent/cli.py`** — the `/status` handler currently calls `run_check(config)` which lists
service health. Extend it to also display muted stacks.

The `run_repl` function signature needs access to the `MuteStore` instance. Update:

```python
async def run_repl(
    agent: HomelabAgent,
    config: AgentConfig,
    event_queue: asyncio.Queue,
    log_path: str,
    mute_store: MuteStore,
) -> None:
```

In the `/status` branch:

```python
elif line == "/status":
    await run_check(config)
    muted = mute_store.all_muted()
    if muted:
        console.print("\n  [bold yellow]Muted stacks:[/bold yellow]")
        for entry in muted:
            ts = entry.muted_at.strftime("%Y-%m-%d %H:%M UTC")
            console.print(f"    [yellow]{entry.stack}[/yellow]  muted at {ts}")
            console.print(f"      reason: {entry.reason}")
    else:
        console.print("\n  [dim]No muted stacks.[/dim]")
```

The import in `cli.py`:

```python
from agent.monitor import MonitorDaemon, MuteStore
```

---

### 8. Action log events

Two new event types are written to the action log. Both follow the existing schema
(`ActionLogger.log` adds `ts` automatically if not provided, but for these events `ts` is set
explicitly at the call site for clarity).

**`stack_muted`** — written by `ToolExecutor._tool_mute_stack` after `mute_store.mute` returns:

```python
await self._logger.log({
    "event": "stack_muted",
    "stack": stack,
    "reason": reason,
    # "session_id" field will be added by Fix 4
})
```

The `ToolExecutor` does not currently hold an `ActionLogger` reference. It needs one, passed from
`HomelabAgent`. See wiring section below.

**`stack_unmuted`** — written by `MonitorDaemon._check_once` on auto-recovery of a muted stack:

```python
await self._logger.log({
    "event": "stack_unmuted",
    "stack": stack_name,
    "reason": "auto: service recovered",
    # "session_id" field will be added by Fix 4
})
```

`MonitorDaemon` already holds `self._logger`.

**Note on `session_id`:** Fix 4 introduces a `session_id` field (set at agent startup). The spec
notes above include `session_id` as a field to add; the exact mechanism (stored on `ActionLogger`
or passed as a parameter) is determined by Fix 4's implementation. These two event types must
include `session_id` using whatever pattern Fix 4 establishes.

**Full example log entries:**

```json
{"event": "stack_muted", "stack": "jellyfin", "reason": "GPU node offline, placement constraint unsatisfiable after 3 attempts", "session_id": "abc123", "ts": "2026-03-24T14:03:00.123456+00:00"}
{"event": "stack_unmuted", "stack": "jellyfin", "reason": "auto: service recovered", "session_id": "abc123", "ts": "2026-03-24T18:41:00.456789+00:00"}
```

---

## Wiring through `HomelabAgent.__init__` and `amain`

### `amain` in `cli.py`

`MuteStore` is instantiated once in `amain`, from the config path:

```python
from agent.monitor import MonitorDaemon, MuteStore

async def amain(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    event_queue: asyncio.Queue = asyncio.Queue()

    mute_store = MuteStore(config.mute.path)          # new

    agent = HomelabAgent(config, mute_store=mute_store)   # updated

    log_path = config.action_log.path
    action_logger = ActionLogger(log_path)

    monitor = MonitorDaemon(                           # updated
        config,
        event_queue,
        action_logger,
        mute_store=mute_store,
        slack_client=agent._slack,  # agent._slack is a private attribute; this pattern is already used elsewhere in cli.py (cost_reporter) and is accepted for now.
    )
    ...
```

In the REPL branch, pass `mute_store` to `run_repl`:

```python
await run_repl(agent, config, event_queue, log_path, mute_store)
```

### `HomelabAgent.__init__` in `agent.py`

`HomelabAgent.__init__` receives `mute_store` and passes it to `ToolExecutor`:

```python
class HomelabAgent:
    def __init__(self, config: AgentConfig, mute_store: MuteStore) -> None:
        ...
        self._tools = ToolExecutor(config, self._slack, mute_store)
```

`ToolExecutor._tool_mute_stack` needs to write a `stack_muted` action log entry. `ToolExecutor`
does not currently hold an `ActionLogger`. Add `action_logger` as a parameter to
`ToolExecutor.__init__`:

```python
class ToolExecutor:
    def __init__(
        self,
        config: "AgentConfig",
        slack_client: Any,
        mute_store: "MuteStore",
        action_logger: "ActionLogger",
    ) -> None:
        ...
        self._mute_store = mute_store
        self._logger = action_logger
```

In `HomelabAgent.__init__`, the `ActionLogger` is created before `ToolExecutor`:

```python
self._logger = ActionLogger(config.action_log.path)
self._tools = ToolExecutor(config, self._slack, mute_store, self._logger)
```

The `TYPE_CHECKING` block in `tools.py` already imports `AgentConfig`. Add:

```python
if TYPE_CHECKING:
    from .agent import ActionLogger
    from .config_schema import AgentConfig
    from .monitor import MuteStore
```

### Known limitations

**Dual `ActionLogger` instances:** `HomelabAgent` creates its own `ActionLogger` internally, and `amain` creates a separate `ActionLogger` for `MonitorDaemon`. Both write to the same file using separate `asyncio.Lock()` instances. These locks are not mutually exclusive — this is a pre-existing pattern in the codebase. Concurrent writes from `MonitorDaemon` and `ToolExecutor` are theoretically racy but acceptable in practice given low write frequency. Fixing this is out of scope.

---

## Files changed

| File | Change |
|------|--------|
| `agent/agent/config_schema.py` | Remove `watched_stacks` from `MonitorConfig`; add `MuteConfig`; add `mute: MuteConfig = MuteConfig()` to `AgentConfig` |
| `agent/agent/monitor.py` | Add `MuteEntry`, `MuteStore`; update `MonitorDaemon.__init__` (add `mute_store`, `slack_client`); update `_check_once` (mute checks, auto-unmute) |
| `agent/agent/tools.py` | Add `mute_stack` to `TOOL_DEFINITIONS`; update `ToolExecutor.__init__` (add `mute_store`, `action_logger`); add `_tool_mute_stack` |
| `agent/agent/safety.py` | Add `"mute_stack": 3` to `_DEFAULT_TIERS` |
| `agent/agent/agent.py` | Update `HomelabAgent.__init__` to accept `mute_store`; pass `mute_store` and `action_logger` to `ToolExecutor` |
| `agent/agent/prompts.py` | Add `## Monitor Muting` subsection to `BEHAVIOUR_RULES` |
| `agent/cli.py` | Import `MuteStore`; instantiate in `amain`; pass to `HomelabAgent`, `MonitorDaemon`, `run_repl`; update `run_repl` signature and `/status` handler |
| `agent/config.yaml` | Remove `watched_stacks`; add `mute` section; add `mute_stack: 3` to `tool_tiers` |

---

## Tests

File: `agent/tests/test_fix6_monitor_muting.py`

All tests are unit tests using `unittest.mock` and `pytest`. No Docker or Slack connections
required.

### Test list

**1. Service down for muted stack — no queue event emitted, no log entry**

Set up a `MuteStore` with `"jellyfin"` muted. Call `_check_once` with a mock `_poll` returning
`jellyfin_jellyfin` as `0/1` replicas. Assert `event_queue.put` was never called. Assert
`action_logger.log` was never called.

**2. Service down for non-muted stack — queue event emitted as normal**

Empty `MuteStore`. Call `_check_once` with `jellyfin_jellyfin` as `0/1`. Assert
`event_queue.put` was called once with event type `"services_down"`. Assert `action_logger.log`
was called with `event="monitor_alert"`.

**3. Service recovers for muted stack — `unmute` called, `stack_unmuted` logged, Slack notified**

Pre-populate `_down_since` with `"jellyfin_jellyfin"`. Set up `MuteStore` with `"jellyfin"` muted.
Call `_check_once` with `jellyfin_jellyfin` at `1/1`. Assert `mute_store.unmute` was called with
`"jellyfin"`. Assert `action_logger.log` was called with `event="stack_unmuted"`. Assert
`slack_client.notify` was called. Assert `event_queue.put` was NOT called (no `service_recovered`
event for previously-muted stacks).

**4. Service recovers for non-muted stack — normal recovery flow (unchanged)**

Pre-populate `_down_since`. Empty `MuteStore`. Call `_check_once` with the service healthy.
Assert `event_queue.put` was called with `type="service_recovered"`. Assert
`action_logger.log` was called with `event="monitor_recovered"`. Assert `slack_client.notify`
was NOT called by the monitor (the agent handles Slack for recoveries in the normal path).

**5. `MuteStore.mute` persists to JSON file**

Create a `MuteStore` with a temp path. Call `await mute_store.mute("jellyfin", "test reason")`.
Read the JSON file. Assert it contains key `"jellyfin"` with `"reason": "test reason"` and a
parseable `"muted_at"` ISO timestamp.

**6. `MuteStore.unmute` removes entry from JSON file**

Mute `"jellyfin"`, then call `await mute_store.unmute("jellyfin")`. Read the JSON file. Assert
`"jellyfin"` is not in the file. Assert `mute_store.is_muted("jellyfin")` returns `False`.

**7. `MuteStore.is_muted` returns `True` after mute, `False` after unmute**

Straightforward state transition test. No file I/O assertion needed (covered by tests 5 and 6).

**8. `MuteStore` initialises from existing JSON file**

Write a valid JSON file manually to a temp path. Instantiate `MuteStore(path)`. Assert
`mute_store.is_muted("jellyfin")` returns `True`. Assert `mute_store.all_muted()` returns one
`MuteEntry` with correct `stack`, `reason`, and `muted_at`.

**9. `MuteStore` initialises cleanly when file does not exist**

Instantiate `MuteStore` with a path that does not exist. Assert no exception is raised. Assert
`mute_store.all_muted()` returns `[]`.

**10. `mute_stack` tool is tier 3 in `_DEFAULT_TIERS`**

```python
from agent.agent.safety import _DEFAULT_TIERS
assert _DEFAULT_TIERS["mute_stack"] == 3
```

**11. Stack name extraction from service names**

Test the `name.split("_", 1)[0]` logic directly via a helper or inline in a parameterised test:

| Input | Expected |
|-------|----------|
| `"jellyfin_jellyfin"` | `"jellyfin"` |
| `"traefik_traefik"` | `"traefik"` |
| `"monitoring_prometheus"` | `"monitoring"` |
| `"monitoring_grafana"` | `"monitoring"` |
| `"postgres_postgres"` | `"postgres"` |

**12. `/status` output includes muted stacks (data, not formatting)**

Construct a `MuteStore` with one muted entry. Call the data-extraction logic used by the `/status`
handler (i.e. `mute_store.all_muted()`). Assert the returned list contains an entry with the
correct `stack` and `reason` fields. This tests the data, not terminal Rich formatting.

---

## Out of scope

- **Mute expiry / TTL**: Mutes do not expire automatically. The only automatic exit is service
  recovery. A future fix could add a `mute_until` field to `MuteEntry` if operator-controlled
  TTLs are needed.
- **Manual unmute tool**: There is no `unmute_stack` agent tool. Operators can manually delete
  the entry from `muted_stacks.json` if needed. A future fix could add this.
- **Cross-service muting**: Muting operates at the stack level (all services with the same stack
  prefix). Individual service muting is not supported.
- **Mute persistence across restarts**: Handled by JSON file persistence — mutes survive agent
  restarts. No additional work needed.
- **Slack notification on mute**: When `mute_stack` is approved and executed, `ToolExecutor`
  returns a confirmation string. The tier-3 approval flow in `_handle_approval_flow` already
  calls `slack.update_plan_result` to update the approval message with the result. No additional
  Slack notification is required from the tool itself.
- **Fix 4 `session_id` dependency**: The `session_id` field in `stack_muted` / `stack_unmuted`
  log entries depends on Fix 4's implementation. If Fix 4 is not yet merged, omit `session_id`
  from these events and add it when Fix 4 lands. The two fixes are independent at the code level.
