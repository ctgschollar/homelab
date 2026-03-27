# Fix 6: Monitor Muting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-stack monitor muting with persistence, a `mute_stack` tool (tier 3), and auto-unmute on service recovery. Remove `watched_stacks` from config.

**Architecture:** `MuteEntry` and `MuteStore` are added to `agent/agent/monitor.py`; `MuteStore` loads and persists muted stacks as JSON at a configurable path from a new `MuteConfig` section in `AgentConfig`. `MonitorDaemon` receives `mute_store` and `slack_client` and checks mute state on every first-detection and every recovery. `ToolExecutor` receives `mute_store` and `action_logger` to implement the `mute_stack` tool, which writes a `stack_muted` action log entry; `MonitorDaemon` writes `stack_unmuted` on auto-recovery. All wiring flows through `amain` in `cli.py`.

**Tech Stack:** Python 3.11, asyncio, json, pytest, pytest-asyncio

---

## File Map

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
| `agent/tests/test_fix6_monitor_muting.py` | All 12 tests (new file) |

---

## Task 1: Add `MuteConfig` to `config_schema.py` and remove `watched_stacks`

**Files:**
- Modify: `agent/agent/config_schema.py`
- Modify: `agent/config.yaml`

### Tests first

- [ ] **Write `agent/tests/test_fix6_monitor_muting.py` — config tests only (Task 1 subset):**

```python
"""Fix 6 — Monitor Muting tests."""
from __future__ import annotations

import pytest

from agent.agent.config_schema import AgentConfig, MonitorConfig, load_agent_config


def test_monitor_config_has_no_watched_stacks() -> None:
    """watched_stacks must not exist on MonitorConfig."""
    assert not hasattr(MonitorConfig, "watched_stacks"), (
        "MonitorConfig.watched_stacks was not removed"
    )
    m = MonitorConfig(poll_interval=30)
    assert not hasattr(m, "watched_stacks")


def test_agent_config_has_mute_field_with_default() -> None:
    """AgentConfig.mute must exist and default to MuteConfig()."""
    from agent.agent.config_schema import MuteConfig
    m = MuteConfig()
    assert m.path == "./muted_stacks.json"
```

- [ ] **Run (expect failure):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "config"
  ```

### Implementation

- [ ] **Modify `agent/agent/config_schema.py`:**

  1. Remove `watched_stacks: list[str] = []` from `MonitorConfig`:
     ```python
     class MonitorConfig(BaseModel):
         poll_interval: int
         # watched_stacks removed — monitor covers all swarm services
     ```

  2. Add `MuteConfig` class after `RollbackConfig`:
     ```python
     class MuteConfig(BaseModel):
         path: str = "./muted_stacks.json"
     ```

  3. Add `mute` field to `AgentConfig` (after `rollback`):
     ```python
     mute: MuteConfig = MuteConfig()
     ```

- [ ] **Modify `agent/config.yaml`:**

  1. Replace the `monitor` section (remove `watched_stacks`):
     ```yaml
     monitor:
       poll_interval: 30
     ```

  2. Add the `mute` section after the `monitor` section:
     ```yaml
     # -----------------------------------------------------------------------
     # Mute store
     # -----------------------------------------------------------------------
     mute:
       path: "./muted_stacks.json"
     ```

  3. Add `mute_stack: 3` to `tool_tiers`:
     ```yaml
     tool_tiers:
         # ... existing entries ...
         mute_stack:              3       # irreversible until auto-recovery; always requires approval
     ```

- [ ] **Run tests (expect pass):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "config"
  ```

- [ ] **Commit:**
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/config_schema.py agent/config.yaml && git commit -m "fix: remove watched_stacks from MonitorConfig; add MuteConfig"
  ```

---

## Task 2: Add `MuteEntry` and `MuteStore` to `monitor.py`

**Files:**
- Modify: `agent/agent/monitor.py`

### Tests first

- [ ] **Add MuteStore tests to `agent/tests/test_fix6_monitor_muting.py`:**

```python
import asyncio
import json
import tempfile
import os
import pytest
from datetime import datetime, timezone


@pytest.fixture
def tmp_mute_path(tmp_path):
    return str(tmp_path / "muted_stacks.json")


def test_mute_store_init_missing_file(tmp_path) -> None:
    """MuteStore with a nonexistent path initialises cleanly."""
    from agent.agent.monitor import MuteStore
    path = str(tmp_path / "does_not_exist.json")
    store = MuteStore(path)
    assert store.all_muted() == []
    assert store.is_muted("jellyfin") is False


def test_mute_store_init_from_existing_file(tmp_path) -> None:
    """MuteStore loads entries from an existing JSON file."""
    from agent.agent.monitor import MuteStore
    path = str(tmp_path / "muted.json")
    data = {
        "jellyfin": {
            "muted_at": "2026-03-24T14:03:00+00:00",
            "reason": "GPU node offline",
        }
    }
    with open(path, "w") as f:
        json.dump(data, f)

    store = MuteStore(path)
    assert store.is_muted("jellyfin") is True
    entries = store.all_muted()
    assert len(entries) == 1
    assert entries[0].stack == "jellyfin"
    assert entries[0].reason == "GPU node offline"
    assert isinstance(entries[0].muted_at, datetime)


@pytest.mark.asyncio
async def test_mute_persists_to_json(tmp_mute_path) -> None:
    """mute() writes the entry to the JSON file."""
    from agent.agent.monitor import MuteStore
    store = MuteStore(tmp_mute_path)
    await store.mute("jellyfin", "test reason")

    with open(tmp_mute_path) as f:
        data = json.load(f)

    assert "jellyfin" in data
    assert data["jellyfin"]["reason"] == "test reason"
    datetime.fromisoformat(data["jellyfin"]["muted_at"])  # must be valid ISO


@pytest.mark.asyncio
async def test_unmute_removes_from_json(tmp_mute_path) -> None:
    """unmute() removes the entry from the JSON file."""
    from agent.agent.monitor import MuteStore
    store = MuteStore(tmp_mute_path)
    await store.mute("jellyfin", "test")
    await store.unmute("jellyfin")

    assert store.is_muted("jellyfin") is False
    with open(tmp_mute_path) as f:
        data = json.load(f)
    assert "jellyfin" not in data


@pytest.mark.asyncio
async def test_is_muted_state_transitions(tmp_mute_path) -> None:
    """is_muted returns True after mute, False after unmute."""
    from agent.agent.monitor import MuteStore
    store = MuteStore(tmp_mute_path)
    assert store.is_muted("traefik") is False
    await store.mute("traefik", "testing")
    assert store.is_muted("traefik") is True
    await store.unmute("traefik")
    assert store.is_muted("traefik") is False
```

- [ ] **Run (expect failure):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "mute_store"
  ```

### Implementation

- [ ] **Modify `agent/agent/monitor.py` — add imports at the top:**
  ```python
  import json
  from dataclasses import dataclass
  ```
  (`asyncio`, `datetime`, `timezone` are already imported.)

- [ ] **Add `MuteEntry` dataclass and `MuteStore` class after imports and before `MonitorDaemon`:**

  ```python
  @dataclass
  class MuteEntry:
      stack: str
      muted_at: datetime   # UTC, stored as ISO string in JSON
      reason: str


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

- [ ] **Run tests (expect pass):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "mute_store"
  ```

- [ ] **Commit:**
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/monitor.py agent/tests/test_fix6_monitor_muting.py && git commit -m "feat: add MuteEntry and MuteStore to monitor.py"
  ```

---

## Task 3: Add `mute_stack` tool definition and tier assignment

**Files:**
- Modify: `agent/agent/tools.py`
- Modify: `agent/agent/safety.py`

### Tests first

- [ ] **Add tool/tier tests to `agent/tests/test_fix6_monitor_muting.py`:**

```python
def test_mute_stack_tier_is_3() -> None:
    """mute_stack must be tier 3 in _DEFAULT_TIERS."""
    from agent.agent.safety import _DEFAULT_TIERS
    assert _DEFAULT_TIERS["mute_stack"] == 3


def test_mute_stack_tool_definition_exists() -> None:
    """mute_stack must appear in TOOL_DEFINITIONS with required fields."""
    from agent.agent.tools import TOOL_DEFINITIONS
    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert "mute_stack" in names
    defn = next(t for t in TOOL_DEFINITIONS if t["name"] == "mute_stack")
    required = defn["input_schema"]["required"]
    assert "stack" in required
    assert "reason" in required


def test_stack_name_extraction() -> None:
    """Stack name is extracted by splitting service name on first underscore."""
    cases = [
        ("jellyfin_jellyfin", "jellyfin"),
        ("traefik_traefik", "traefik"),
        ("monitoring_prometheus", "monitoring"),
        ("monitoring_grafana", "monitoring"),
        ("postgres_postgres", "postgres"),
        ("standalone", "standalone"),  # no underscore → full name
    ]
    for svc, expected in cases:
        assert svc.split("_", 1)[0] == expected
```

- [ ] **Run (expect failure for tool/tier tests):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "tier or tool_definition or stack_name"
  ```

### Implementation

- [ ] **Modify `agent/agent/safety.py` — add `"mute_stack": 3` to `_DEFAULT_TIERS`:**

  Add after `"write_file": 3`:
  ```python
  "mute_stack": 3,
  ```

- [ ] **Modify `agent/agent/tools.py` — append `mute_stack` as the last entry in `TOOL_DEFINITIONS`:**

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

- [ ] **Run tests (expect pass):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "tier or tool_definition or stack_name"
  ```

- [ ] **Commit:**
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/safety.py agent/agent/tools.py && git commit -m "feat: add mute_stack tool definition (tier 3)"
  ```

---

## Task 4: Integrate `MuteStore` into `MonitorDaemon._check_once`

**Files:**
- Modify: `agent/agent/monitor.py`

### Tests first

- [ ] **Add `MonitorDaemon` mute-integration tests to `agent/tests/test_fix6_monitor_muting.py`:**

```python
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio


def _make_monitor(mute_store, tmp_path):
    """Build a MonitorDaemon with mocked config, queue, logger, slack."""
    from agent.agent.monitor import MonitorDaemon

    config = MagicMock()
    config.monitor.poll_interval = 30
    config.docker.socket = "unix:///var/run/docker.sock"

    event_queue = asyncio.Queue()
    action_logger = MagicMock()
    action_logger.log = AsyncMock()
    slack_client = MagicMock()
    slack_client.notify = AsyncMock()

    daemon = MonitorDaemon(
        config=config,
        event_queue=event_queue,
        action_logger=action_logger,
        mute_store=mute_store,
        slack_client=slack_client,
    )
    return daemon, event_queue, action_logger, slack_client


@pytest.mark.asyncio
async def test_service_down_muted_stack_no_event_no_log(tmp_path) -> None:
    """First detection of down service for muted stack emits no queue event and no log."""
    from agent.agent.monitor import MuteStore

    mute_path = str(tmp_path / "mutes.json")
    store = MuteStore(mute_path)
    await store.mute("jellyfin", "GPU offline")

    daemon, event_queue, action_logger, _ = _make_monitor(store, tmp_path)

    poll_result = [{"name": "jellyfin_jellyfin", "running": 0, "desired": 1, "last_error": ""}]
    with patch.object(daemon, "_poll", return_value=poll_result):
        await daemon._check_once()

    assert event_queue.empty()
    action_logger.log.assert_not_called()


@pytest.mark.asyncio
async def test_service_down_non_muted_stack_emits_event_and_log(tmp_path) -> None:
    """First detection of down service for non-muted stack emits queue event and log."""
    from agent.agent.monitor import MuteStore

    mute_path = str(tmp_path / "mutes.json")
    store = MuteStore(mute_path)  # empty

    daemon, event_queue, action_logger, _ = _make_monitor(store, tmp_path)

    poll_result = [{"name": "jellyfin_jellyfin", "running": 0, "desired": 1, "last_error": "OOM"}]
    with patch.object(daemon, "_poll", return_value=poll_result):
        await daemon._check_once()

    assert not event_queue.empty()
    event = await event_queue.get()
    assert event["type"] == "services_down"
    action_logger.log.assert_called_once()
    call_kwargs = action_logger.log.call_args[0][0]
    assert call_kwargs["event"] == "monitor_alert"


@pytest.mark.asyncio
async def test_service_recovers_muted_stack_auto_unmute(tmp_path) -> None:
    """Recovery of service in muted stack: unmute called, stack_unmuted logged, Slack notified, no queue event."""
    from agent.agent.monitor import MuteStore
    from datetime import datetime, timezone

    mute_path = str(tmp_path / "mutes.json")
    store = MuteStore(mute_path)
    await store.mute("jellyfin", "GPU offline")

    daemon, event_queue, action_logger, slack_client = _make_monitor(store, tmp_path)

    # Pre-populate _down_since to simulate the service was already known as down
    daemon._down_since["jellyfin_jellyfin"] = datetime.now(timezone.utc)

    poll_result = [{"name": "jellyfin_jellyfin", "running": 1, "desired": 1, "last_error": ""}]
    with patch.object(daemon, "_poll", return_value=poll_result):
        await daemon._check_once()

    # unmute must have been called — store no longer has jellyfin
    assert store.is_muted("jellyfin") is False

    # stack_unmuted must be logged
    action_logger.log.assert_called_once()
    log_call = action_logger.log.call_args[0][0]
    assert log_call["event"] == "stack_unmuted"
    assert log_call["stack"] == "jellyfin"

    # Slack must have been notified
    slack_client.notify.assert_called_once()

    # No service_recovered event for previously-muted stacks
    assert event_queue.empty()


@pytest.mark.asyncio
async def test_service_recovers_non_muted_stack_normal_flow(tmp_path) -> None:
    """Recovery of service in non-muted stack: event queued, log written, Slack NOT called by monitor."""
    from agent.agent.monitor import MuteStore
    from datetime import datetime, timezone

    mute_path = str(tmp_path / "mutes.json")
    store = MuteStore(mute_path)  # empty

    daemon, event_queue, action_logger, slack_client = _make_monitor(store, tmp_path)
    daemon._down_since["traefik_traefik"] = datetime.now(timezone.utc)

    poll_result = [{"name": "traefik_traefik", "running": 1, "desired": 1, "last_error": ""}]
    with patch.object(daemon, "_poll", return_value=poll_result):
        await daemon._check_once()

    assert not event_queue.empty()
    event = await event_queue.get()
    assert event["type"] == "service_recovered"

    action_logger.log.assert_called_once()
    log_call = action_logger.log.call_args[0][0]
    assert log_call["event"] == "monitor_recovered"

    slack_client.notify.assert_not_called()
```

- [ ] **Run (expect failure):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "monitor_daemon or service_down or service_recovers"
  ```

### Implementation

- [ ] **Modify `agent/agent/monitor.py` — update `TYPE_CHECKING` block:**

  ```python
  if TYPE_CHECKING:
      from .agent import ActionLogger
      from .config_schema import AgentConfig
      from .slack import SlackClient
  ```

- [ ] **Modify `MonitorDaemon.__init__` signature** to accept `mute_store` and `slack_client`:

  ```python
  def __init__(
      self,
      config: AgentConfig,
      event_queue: asyncio.Queue,
      action_logger: ActionLogger,
      mute_store: "MuteStore",
      slack_client: "SlackClient",
  ) -> None:
      self._poll_interval: int = config.monitor.poll_interval
      self._docker_socket: str = config.docker.socket
      self._event_queue = event_queue
      self._logger = action_logger
      self._mute_store = mute_store
      self._slack = slack_client
      self._down_since: dict[str, datetime] = {}
  ```

- [ ] **Modify `_check_once` — degraded path** (replace the `if name not in self._down_since:` block):

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

- [ ] **Modify `_check_once` — recovery path** (replace the `if name in self._down_since:` block inside `else:`):

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

- [ ] **Run tests (expect pass):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "monitor_daemon or service_down or service_recovers"
  ```

- [ ] **Commit:**
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/monitor.py agent/tests/test_fix6_monitor_muting.py && git commit -m "feat: integrate MuteStore into MonitorDaemon._check_once"
  ```

---

## Task 5: Wire `MuteStore` into `ToolExecutor` and `HomelabAgent`

**Files:**
- Modify: `agent/agent/tools.py`
- Modify: `agent/agent/agent.py`

### Tests first

- [ ] **Add `ToolExecutor.mute_stack` test to `agent/tests/test_fix6_monitor_muting.py`:**

```python
@pytest.mark.asyncio
async def test_tool_mute_stack_calls_store_and_logs(tmp_path) -> None:
    """_tool_mute_stack writes to MuteStore and logs stack_muted."""
    from agent.agent.monitor import MuteStore
    from agent.agent.tools import ToolExecutor

    mute_path = str(tmp_path / "mutes.json")
    store = MuteStore(mute_path)

    config = MagicMock()
    config.docker.socket = "unix:///var/run/docker.sock"
    config.swarm.ssh_key = ""
    config.swarm.ssh_user = ""
    config.ansible.repo_path = "/tmp"
    config.ansible.inventory = "/tmp/inv"
    config.ansible.git_token = None
    config.ansible.git_author_name = "test"
    config.ansible.git_author_email = "test@test.com"
    config.rollback.state_path = str(tmp_path / "rollback.json")
    config.reports.path = "reports"
    config.action_log.path = str(tmp_path / "action.log")

    slack_client = MagicMock()
    action_logger = MagicMock()
    action_logger.log = AsyncMock()

    executor = ToolExecutor(config, slack_client, store, action_logger)
    result = await executor._tool_mute_stack({"stack": "jellyfin", "reason": "GPU offline"})

    assert store.is_muted("jellyfin") is True
    action_logger.log.assert_called_once()
    log_call = action_logger.log.call_args[0][0]
    assert log_call["event"] == "stack_muted"
    assert log_call["stack"] == "jellyfin"
    assert "jellyfin" in result
```

- [ ] **Run (expect failure):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "tool_mute_stack"
  ```

### Implementation

- [ ] **Modify `agent/agent/tools.py` — add `TYPE_CHECKING` imports:**

  The existing `TYPE_CHECKING` block only imports `AgentConfig`. Extend it:
  ```python
  if TYPE_CHECKING:
      from .agent import ActionLogger
      from .config_schema import AgentConfig
      from .monitor import MuteStore
  ```

- [ ] **Modify `ToolExecutor.__init__`** — add `mute_store` and `action_logger` parameters:

  Change signature from:
  ```python
  def __init__(self, config: "AgentConfig", slack_client: Any) -> None:
  ```
  To:
  ```python
  def __init__(
      self,
      config: "AgentConfig",
      slack_client: Any,
      mute_store: "MuteStore",
      action_logger: "ActionLogger",
  ) -> None:
  ```

  Add at the end of `__init__` body:
  ```python
  self._mute_store = mute_store
  self._logger = action_logger
  ```

- [ ] **Add `_tool_mute_stack` method to `ToolExecutor`** (after `_tool_slack_notify`):

  ```python
  async def _tool_mute_stack(self, inp: dict) -> str:
      stack = inp["stack"]
      reason = inp["reason"]
      await self._mute_store.mute(stack, reason)
      await self._logger.log({
          "event": "stack_muted",
          "stack": stack,
          "reason": reason,
      })
      return f"Stack '{stack}' muted. It will be skipped on future monitor polls until it recovers."
  ```

- [ ] **Modify `agent/agent/agent.py` — update `HomelabAgent.__init__`:**

  Change signature from:
  ```python
  def __init__(self, config: AgentConfig) -> None:
  ```
  To:
  ```python
  def __init__(self, config: AgentConfig, mute_store: "MuteStore") -> None:
  ```

  Add `TYPE_CHECKING` import for `MuteStore` (it's already in `monitor.py`, not yet imported in `agent.py`). Add at the top of `agent.py` under the existing imports:
  ```python
  from typing import TYPE_CHECKING
  if TYPE_CHECKING:
      from .monitor import MuteStore
  ```
  (Note: `TYPE_CHECKING` is not currently imported in `agent.py` — check and add if missing.)

  Change the `ToolExecutor` instantiation line from:
  ```python
  self._tools = ToolExecutor(config, self._slack)
  ```
  To:
  ```python
  self._tools = ToolExecutor(config, self._slack, mute_store, self._logger)
  ```

  Note: `self._logger` is already created on the line above (`self._logger = ActionLogger(config.action_log.path)`), so ordering is correct.

- [ ] **Run tests (expect pass):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "tool_mute_stack"
  ```

- [ ] **Commit:**
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/tools.py agent/agent/agent.py && git commit -m "feat: wire MuteStore and ActionLogger into ToolExecutor; add _tool_mute_stack"
  ```

---

## Task 6: Wire `MuteStore` through `amain` and `run_repl` in `cli.py`

**Files:**
- Modify: `agent/cli.py`

### Tests first

- [ ] **Add `/status` data test to `agent/tests/test_fix6_monitor_muting.py`:**

```python
@pytest.mark.asyncio
async def test_status_all_muted_returns_entries(tmp_path) -> None:
    """`all_muted()` returns the correct entries for the /status display."""
    from agent.agent.monitor import MuteStore

    mute_path = str(tmp_path / "mutes.json")
    store = MuteStore(mute_path)
    await store.mute("jellyfin", "GPU node offline")

    muted = store.all_muted()
    assert len(muted) == 1
    assert muted[0].stack == "jellyfin"
    assert muted[0].reason == "GPU node offline"
```

- [ ] **Run (expect pass — this tests MuteStore data only, not CLI):**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix6_monitor_muting.py -v -k "status_all_muted"
  ```

### Implementation

- [ ] **Modify `agent/cli.py` — update imports:**

  Change:
  ```python
  from agent.monitor import MonitorDaemon
  ```
  To:
  ```python
  from agent.monitor import MonitorDaemon, MuteStore
  ```

- [ ] **Modify `amain` in `agent/cli.py`:**

  After `config = load_config(args.config)` and before `agent = HomelabAgent(config)`, add:
  ```python
  mute_store = MuteStore(config.mute.path)
  ```

  Change `HomelabAgent` instantiation:
  ```python
  agent = HomelabAgent(config, mute_store=mute_store)
  ```

  Change `MonitorDaemon` instantiation:
  ```python
  monitor = MonitorDaemon(
      config,
      event_queue,
      action_logger,
      mute_store=mute_store,
      slack_client=agent._slack,
  )
  ```

  Change the `run_repl` call in the interactive REPL branch:
  ```python
  await run_repl(agent, config, event_queue, log_path, mute_store)
  ```

- [ ] **Modify `run_repl` signature** — add `mute_store` parameter:

  Change:
  ```python
  async def run_repl(agent: HomelabAgent, config: AgentConfig, event_queue: asyncio.Queue, log_path: str) -> None:
  ```
  To:
  ```python
  async def run_repl(agent: HomelabAgent, config: AgentConfig, event_queue: asyncio.Queue, log_path: str, mute_store: MuteStore) -> None:
  ```

- [ ] **Modify the `/status` branch in `run_repl`:**

  Change:
  ```python
  elif line == "/status":
      await run_check(config)
  ```
  To:
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

- [ ] **Run full test suite to confirm nothing is broken:**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
  ```

- [ ] **Commit:**
  ```bash
  cd /home/chris/src/homelab && git add agent/cli.py && git commit -m "feat: wire MuteStore through amain and run_repl; /status shows muted stacks"
  ```

---

## Task 7: Update `BEHAVIOUR_RULES` in `prompts.py`

**Files:**
- Modify: `agent/agent/prompts.py`

This is a prompt-only change; no tests needed.

- [ ] **Add `## Monitor Muting` subsection to `BEHAVIOUR_RULES` in `agent/agent/prompts.py`:**

  Append to the end of `BEHAVIOUR_RULES` (or at an appropriate location within the existing rules string):

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

- [ ] **Commit:**
  ```bash
  cd /home/chris/src/homelab && git add agent/agent/prompts.py && git commit -m "feat: add Monitor Muting behaviour rules to prompts"
  ```

---

## Final verification

- [ ] **Run the full test suite:**
  ```bash
  cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
  ```

- [ ] **Confirm all 12 tests pass:**

  | # | Test name | Covers |
  |---|-----------|--------|
  | 1 | `test_monitor_config_has_no_watched_stacks` | `watched_stacks` removed from `MonitorConfig` |
  | 2 | `test_agent_config_has_mute_field_with_default` | `MuteConfig` default path |
  | 3 | `test_mute_store_init_missing_file` | `MuteStore` handles missing file |
  | 4 | `test_mute_store_init_from_existing_file` | `MuteStore` loads from JSON |
  | 5 | `test_mute_persists_to_json` | `mute()` writes JSON |
  | 6 | `test_unmute_removes_from_json` | `unmute()` removes from JSON |
  | 7 | `test_is_muted_state_transitions` | `is_muted` state machine |
  | 8 | `test_mute_stack_tier_is_3` | `_DEFAULT_TIERS["mute_stack"] == 3` |
  | 9 | `test_mute_stack_tool_definition_exists` | `TOOL_DEFINITIONS` entry |
  | 10 | `test_stack_name_extraction` | `split("_", 1)[0]` logic |
  | 11 | `test_service_down_muted_stack_no_event_no_log` | Muted degraded path |
  | 12 | `test_service_down_non_muted_stack_emits_event_and_log` | Non-muted degraded path |
  | 13 | `test_service_recovers_muted_stack_auto_unmute` | Auto-unmute on recovery |
  | 14 | `test_service_recovers_non_muted_stack_normal_flow` | Normal recovery unchanged |
  | 15 | `test_tool_mute_stack_calls_store_and_logs` | `_tool_mute_stack` method |
  | 16 | `test_status_all_muted_returns_entries` | `/status` data layer |

---

## Key implementation notes

- **`MuteStore` lives in `monitor.py`**, not a new file. The spec places `MuteEntry` and `MuteStore` in `agent/agent/monitor.py` after imports and before `MonitorDaemon`.
- **Stack name extraction** uses `service_name.split("_", 1)[0]`. If there is no underscore, the full service name is used as the stack name.
- **The mute check only fires on first detection** (`name not in self._down_since`). Services already in `_down_since` (repeat polls while still degraded) never re-trigger an alert — muting has no additional effect on them.
- **Auto-unmute does not emit a `service_recovered` event**. The muted recovery path notifies Slack directly from `MonitorDaemon` and writes `stack_unmuted` to the action log. The normal `service_recovered` queue event is only emitted for non-muted stacks.
- **Dual `ActionLogger` instances** is a pre-existing pattern. `HomelabAgent` creates one; `amain` creates a second for `MonitorDaemon`. Both write to the same file. This is accepted as-is.
- **`session_id` in log entries**: The `stack_muted` and `stack_unmuted` log entries omit `session_id` in this implementation. Add it when Fix 4 lands, following whatever pattern Fix 4 establishes on `ActionLogger`.
- **`agent.py` `TYPE_CHECKING` import**: `agent.py` does not currently import `TYPE_CHECKING` (it uses plain imports). Check whether `TYPE_CHECKING` is already imported; if not, add it from `typing` and guard the `MuteStore` import. Alternatively, use a string annotation `"MuteStore"` on the parameter and add a runtime import inside the method — but the `TYPE_CHECKING` guard is cleaner and consistent with the rest of the codebase.
