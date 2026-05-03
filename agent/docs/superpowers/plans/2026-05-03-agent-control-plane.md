# Agent Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce an `AgentController` that centralises runtime state and routing, then build transient grace periods, Slack control commands, `run_shell` tier hardening, and a command whitelist on top of it.

**Architecture:** A new `AgentController` class sits between the event queue and `HomelabAgent`. It owns mode (`monitor`/`act`), the emergency stop flag, the grace period queue, and the command whitelist. `HomelabAgent` implements a new `AgentBase` protocol so the controller can be extended to route events to different agents later.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, FastAPI, pytest-asyncio, unittest.mock, PyYAML

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `agent_base.py` | Create | `AgentBase` Protocol |
| `controller.py` | Create | `AgentController`, `DeferredAlert` |
| `agent/agent/agent.py` | Modify | Add `cancel_all()`, `_active_task`; update `build_approval_app()` |
| `agent/agent/safety.py` | Modify | Tier-2 collapse for `run_shell`; whitelist check; `update_whitelist()` |
| `agent/agent/slack.py` | Modify | Add `notify_deferred_alert()`; add [Approve + Whitelist] button to `notify_plan()` |
| `agent/agent/config_schema.py` | Modify | Add `ControllerConfig`; add `grace_period_seconds` to `MonitorConfig` |
| `cli.py` | Modify | Wire controller; slim `event_consumer`; pass controller to approval listener |
| `config.yaml` | Modify | Add `controller.mode: "monitor"`; add `monitor.grace_period_seconds: 600` |
| `whitelist.json` | Create | Empty JSON array `[]` |
| `tests/test_safety.py` | Modify | Update two broken tier assertions; add tier-2 collapse + whitelist tests |
| `tests/test_controller.py` | Create | Controller routing, commands, grace period, whitelist |

All commands run from `agent/` directory. Run tests with: `hatch run -e test pytest`

---

## Task 1: `run_shell` tier-2 collapse

**Files:**
- Modify: `agent/agent/safety.py:118-131`
- Modify: `tests/test_safety.py`

- [ ] **Step 1: Run the two tests that will break, confirm they currently pass**

```bash
hatch run -e test pytest tests/test_safety.py::test_resolve_tier_git_push_forces_tier2 tests/test_safety.py::test_resolve_tier_git_push_tier2_stays_tier2 -v
```
Expected: both PASS (before our change)

- [ ] **Step 2: Write the new failing tests**

Add to the bottom of `tests/test_safety.py`:

```python
# --- run_shell tier-2 collapse ---

def test_run_shell_agent_tier2_collapsed_to_tier3() -> None:
    """Agent proposing tier 2 for run_shell must be escalated to tier 3."""
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=2,
        agent_reasoning="moderate risk",
        command="df -h",
    )
    assert resolved.tier == 3


def test_run_shell_agent_tier1_preserved() -> None:
    """Agent proposing tier 1 for a read-only command stays tier 1."""
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        agent_reasoning="read-only",
        command="df -h",
    )
    assert resolved.tier == 1


def test_run_shell_agent_no_tier_defaults_to_tier3() -> None:
    """When agent omits tier, default is 3 (was 2 before fix)."""
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=None,
        agent_reasoning=None,
        command="df -h",
    )
    assert resolved.tier == 3


def test_run_shell_tier2_pattern_match_becomes_tier3() -> None:
    """A command matching a tier-2 pattern (git push) resolves to tier 3, not tier 2."""
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="git push origin main",
    )
    assert resolved.tier == 3
```

- [ ] **Step 3: Run new tests to confirm they fail**

```bash
hatch run -e test pytest tests/test_safety.py::test_run_shell_agent_tier2_collapsed_to_tier3 tests/test_safety.py::test_run_shell_agent_no_tier_defaults_to_tier3 tests/test_safety.py::test_run_shell_tier2_pattern_match_becomes_tier3 -v
```
Expected: all FAIL

- [ ] **Step 4: Apply the fix to `agent/agent/safety.py`**

Replace the `_base_tier` method (lines 118–131):

```python
def _base_tier(self, tool_name: str, agent_proposed_tier: int | None, command: str | None = None) -> int:
    """Return the raw tier before safe-mode overrides."""
    configured = self.tool_tiers.get(tool_name)

    if configured is not None:
        if configured in (1, 2, 3):
            return int(configured)
        if configured == "agent":
            if tool_name == "run_shell" and agent_proposed_tier is not None and command is not None:
                tier = self._check_shell_command(command, agent_proposed_tier)
                # Only tier 1 (read-only) or tier 3 (explicit approval).
                # Tier 2 veto windows are never appropriate for shell commands.
                return 1 if tier == 1 else 3
            return agent_proposed_tier if agent_proposed_tier is not None else 3

    return _DEFAULT_TIERS.get(tool_name, 2)
```

- [ ] **Step 5: Update the two now-broken existing tests**

In `tests/test_safety.py`, change:

```python
def test_resolve_tier_git_push_forces_tier2() -> None:
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="git push origin main",
    )
    assert resolved.tier == 3  # was 2 — tier-2 collapsed to tier-3 for run_shell


def test_resolve_tier_git_push_tier2_stays_tier2() -> None:
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=2,
        command="git push origin main",
    )
    assert resolved.tier == 3  # was 2 — tier-2 collapsed to tier-3 for run_shell
```

- [ ] **Step 6: Run the full safety test suite**

```bash
hatch run -e test pytest tests/test_safety.py -v
```
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add agent/agent/safety.py tests/test_safety.py
git commit -m "fix: collapse run_shell tier-2 to tier-3 for agent-discretion tools"
```

---

## Task 2: AgentBase Protocol

**Files:**
- Create: `agent_base.py`

- [ ] **Step 1: Create `agent_base.py`**

```python
# agent_base.py
from __future__ import annotations
from typing import Protocol, runtime_checkable


@runtime_checkable
class AgentBase(Protocol):
    async def chat(self, message: str, trigger: str) -> tuple[str, float]: ...
    async def handle_event(self, event: dict) -> tuple[str, float]: ...
    async def cancel_all(self) -> None: ...
```

- [ ] **Step 2: Verify HomelabAgent satisfies the protocol (no code change needed)**

```bash
cd /home/chris/src/homelab/agent && python -c "
from agent_base import AgentBase
from agent.agent import HomelabAgent
# HomelabAgent.cancel_all() does not exist yet — this will fail after Task 3 adds it
print('AgentBase defined OK')
"
```
Expected: `AgentBase defined OK` (cancel_all check deferred to Task 3)

- [ ] **Step 3: Commit**

```bash
git add agent_base.py
git commit -m "feat: add AgentBase protocol for agent implementations"
```

---

## Task 3: HomelabAgent.cancel_all()

**Files:**
- Modify: `agent/agent/agent.py`
- Modify: `tests/test_agent_logging.py` (verify no breakage)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_logging.py` (or create a focused file `tests/test_agent_cancel.py`):

```python
# tests/test_agent_cancel.py
import asyncio
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def minimal_agent():
    """Build a HomelabAgent with the minimum viable config using model_construct."""
    from agent.agent import HomelabAgent
    from agent.config_schema import (
        AgentConfig, AnthropicConfig, SlackConfig, DockerConfig,
        SwarmConfig, AnsibleConfig, MonitorConfig, SafetyConfig,
        SafeModeResourcesConfig, ShellCommandGuardsConfig,
        ActionLogConfig, ControllerConfig,
    )
    config = AgentConfig.model_construct(
        anthropic=AnthropicConfig(model="claude-sonnet-4-20250514", input_cost_per_mtok=3.0, output_cost_per_mtok=15.0),
        slack=SlackConfig(channel="#test"),
        docker=DockerConfig(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig(nodes=[], ssh_key="/tmp/key", ssh_user="root"),
        ansible=AnsibleConfig(repo_path="/tmp", inventory="/tmp/inv.yml", git_author_name="Test", git_author_email="test@test.com"),
        monitor=MonitorConfig(poll_interval=30, grace_period_seconds=600),
        controller=ControllerConfig(mode="act"),
        safety=SafetyConfig(
            global_safe_mode=False,
            safe_mode_resources=SafeModeResourcesConfig(),
            tool_tiers={"run_shell": "agent"},
            log_agent_tier_reasoning=False,
            shell_command_guards=ShellCommandGuardsConfig(),
        ),
        action_log=ActionLogConfig(path="/tmp/action.log"),
    )
    with patch("agent.agent.anthropic.AsyncAnthropic"):
        agent = HomelabAgent(config)
    return agent


@pytest.mark.asyncio
async def test_cancel_all_cancels_pending(minimal_agent) -> None:
    """cancel_all() must cancel all pending approvals."""
    fut = minimal_agent._pending.register("plan-test", "run_shell", "plan text", tier=3)
    await minimal_agent.cancel_all()
    result = await fut
    approved, reason = result
    assert approved is False
    assert "emergency stop" in reason


@pytest.mark.asyncio
async def test_cancel_all_cancels_active_task(minimal_agent) -> None:
    """cancel_all() must cancel _active_task if set."""
    async def _long() -> None:
        await asyncio.sleep(100)

    task = asyncio.create_task(_long())
    minimal_agent._active_task = task
    await minimal_agent.cancel_all()
    await asyncio.sleep(0)
    assert task.cancelled()
```

- [ ] **Step 2: Run to confirm they fail**

```bash
hatch run -e test pytest tests/test_agent_cancel.py -v
```
Expected: FAIL — `ControllerConfig` not in schema yet, and `cancel_all` not defined

- [ ] **Step 3: Add `_active_task` and `cancel_all()` to `HomelabAgent`**

In `agent/agent/agent.py`, in `HomelabAgent.__init__` add after `self._active_execution`:

```python
self._active_task: asyncio.Task | None = None
```

Add as a new method after `aclose()`:

```python
async def cancel_all(self) -> None:
    self._pending.cancel_all("emergency stop")
    if self._active_task is not None:
        self._active_task.cancel()
```

- [ ] **Step 4: Commit (tests will pass after Task 4 adds ControllerConfig)**

```bash
git add agent/agent/agent.py tests/test_agent_cancel.py
git commit -m "feat: add HomelabAgent.cancel_all() and _active_task tracking"
```

---

## Task 4: Config schema additions

**Files:**
- Modify: `agent/agent/config_schema.py`
- Modify: `agent/config.yaml`

- [ ] **Step 1: Add `ControllerConfig` and `grace_period_seconds` to `config_schema.py`**

After the `MonitorConfig` class, add:

```python
class ControllerConfig(BaseModel):
    mode: Literal["monitor", "act"] = "monitor"
    whitelist_path: str = "./whitelist.json"
```

Modify `MonitorConfig`:

```python
class MonitorConfig(BaseModel):
    poll_interval: int
    grace_period_seconds: int = 600
```

In `AgentConfig`, add after the `monitor` field:

```python
controller: ControllerConfig = Field(default_factory=ControllerConfig)
```

Add `Literal` to the imports at the top of the file (it's used by `TierValue` already — confirm it's there):

```python
from typing import Literal, Optional
```

- [ ] **Step 2: Add to `config.yaml`**

After the `monitor:` block, add:

```yaml
controller:
  mode: "monitor"
  whitelist_path: "./whitelist.json"
```

Update the `monitor:` block to add `grace_period_seconds`:

```yaml
monitor:
  poll_interval: 30
  grace_period_seconds: 600
```

- [ ] **Step 3: Validate config loads correctly**

```bash
hatch run config validate
```
Expected: `Config is valid.`

- [ ] **Step 4: Run the agent cancel tests (should now pass)**

```bash
hatch run -e test pytest tests/test_agent_cancel.py -v
```
Expected: all PASS

- [ ] **Step 5: Run full test suite to check nothing broke**

```bash
hatch run -e test pytest -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add agent/agent/config_schema.py agent/config.yaml
git commit -m "feat: add ControllerConfig and monitor.grace_period_seconds to config schema"
```

---

## Task 5: AgentController skeleton

**Files:**
- Create: `controller.py`
- Create: `tests/test_controller.py`

- [ ] **Step 1: Write failing tests for basic routing**

Create `tests/test_controller.py`:

```python
# tests/test_controller.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


def make_controller(mode: str = "act", grace_period: int = 10, tmp_path: Path | None = None):
    """Build an AgentController with mocked dependencies."""
    from controller import AgentController
    from agent.config_schema import (
        AgentConfig, ControllerConfig, MonitorConfig, AnthropicConfig,
        SlackConfig, DockerConfig, SwarmConfig, AnsibleConfig,
        SafetyConfig, SafeModeResourcesConfig, ShellCommandGuardsConfig,
        ActionLogConfig,
    )
    config = AgentConfig.model_construct(
        controller=ControllerConfig(
            mode=mode,
            whitelist_path=str(tmp_path / "whitelist.json") if tmp_path else "/tmp/whitelist_test.json",
        ),
        monitor=MonitorConfig(poll_interval=30, grace_period_seconds=grace_period),
        anthropic=AnthropicConfig(model="claude-sonnet-4-20250514", input_cost_per_mtok=3.0, output_cost_per_mtok=15.0),
        slack=SlackConfig(channel="#test"),
        docker=DockerConfig(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig(nodes=[], ssh_key="/tmp/key", ssh_user="root"),
        ansible=AnsibleConfig(repo_path="/tmp", inventory="/tmp/inv.yml", git_author_name="Test", git_author_email="test@test.com"),
        safety=SafetyConfig(
            global_safe_mode=False,
            safe_mode_resources=SafeModeResourcesConfig(),
            tool_tiers={"run_shell": "agent"},
            log_agent_tier_reasoning=False,
            shell_command_guards=ShellCommandGuardsConfig(),
        ),
        action_log=ActionLogConfig(path="/tmp/action.log"),
    )
    agent = AsyncMock()
    agent.handle_event = AsyncMock(return_value=("", 0.0))
    agent.chat = AsyncMock(return_value=("", 0.0))
    agent.cancel_all = AsyncMock()
    agent._slack = AsyncMock()

    slack = AsyncMock()
    slack.configured = True
    slack.notify = AsyncMock(return_value={"ok": True})
    slack.notify_deferred_alert = AsyncMock(return_value=("#ch", "12345.0"))

    return AgentController(
        config=config,
        agents={"default": agent},
        slack=slack,
        config_path="/tmp/test_config.yaml",
    ), agent, slack


# --- mode routing ---

@pytest.mark.asyncio
async def test_monitor_mode_does_not_call_agent(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="monitor")
    event = {"source": "monitor", "type": "services_down", "data": {"services": [{"service": "foo", "running": 0, "desired": 1, "last_error": ""}]}}
    await controller.handle_event(event)
    agent.handle_event.assert_not_called()
    slack.notify.assert_called_once()


@pytest.mark.asyncio
async def test_act_mode_defers_monitor_event(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    event = {"source": "monitor", "type": "services_down", "data": {"services": [{"service": "foo", "running": 0, "desired": 1, "last_error": ""}]}}
    await controller.handle_event(event)
    # Should be deferred, not sent to agent immediately
    agent.handle_event.assert_not_called()
    assert len(controller.deferred) == 1


@pytest.mark.asyncio
async def test_stopped_drops_event(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    controller.stopped = True
    event = {"source": "monitor", "type": "services_down", "data": {"services": [{"service": "foo", "running": 0, "desired": 1, "last_error": ""}]}}
    await controller.handle_event(event)
    agent.handle_event.assert_not_called()
    assert len(controller.deferred) == 0


# --- control commands ---

@pytest.mark.asyncio
async def test_cmd_stop_sets_stopped_flag(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    result = await controller.handle_command("stop")
    assert controller.stopped is True
    assert "Stopped" in result


@pytest.mark.asyncio
async def test_cmd_start_clears_stopped_flag(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    controller.stopped = True
    result = await controller.handle_command("start")
    assert controller.stopped is False
    assert "Resumed" in result


@pytest.mark.asyncio
async def test_cmd_stop_calls_agent_cancel_all(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    await controller.handle_command("stop")
    agent.cancel_all.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_queue_empty(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    result = await controller.handle_command("queue")
    assert "No pending" in result


@pytest.mark.asyncio
async def test_cmd_mode_monitor(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    with patch.object(controller, "_persist_mode"):
        result = await controller.handle_command("mode monitor")
    assert controller.mode == "monitor"
    assert "monitor" in result.lower()


@pytest.mark.asyncio
async def test_cmd_mode_act(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="monitor")
    with patch.object(controller, "_persist_mode"):
        result = await controller.handle_command("mode act")
    assert controller.mode == "act"
    assert "act" in result.lower()


@pytest.mark.asyncio
async def test_cmd_case_insensitive(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    result = await controller.handle_command("STOP")
    assert controller.stopped is True
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
hatch run -e test pytest tests/test_controller.py -v
```
Expected: FAIL — `controller` module not found

- [ ] **Step 3: Create `controller.py`**

```python
# controller.py
from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from rich.console import Console

from agent_base import AgentBase

if TYPE_CHECKING:
    from agent.agent.config_schema import AgentConfig
    from agent.agent.rag import IncidentRAG
    from agent.agent.slack import SlackClient

console = Console()

_COMMANDS = frozenset(["stop", "start", "queue", "mode monitor", "mode act"])


@dataclass
class DeferredAlert:
    alert_id: str
    event: dict
    services: list[str]
    timer_task: asyncio.Task
    slack_message_ref: tuple[str, str] | None
    deferred_at: datetime


class AgentController:
    def __init__(
        self,
        config: AgentConfig,
        agents: dict[str, AgentBase],
        slack: SlackClient,
        config_path: str = "config.yaml",
        rag: IncidentRAG | None = None,
    ) -> None:
        self._config = config
        self.agents = agents
        self._slack = slack
        self._config_path = config_path
        self._rag = rag
        self._whitelist_path = Path(config.controller.whitelist_path)
        self._grace_period = config.monitor.grace_period_seconds

        self.mode: Literal["monitor", "act"] = config.controller.mode
        self.whitelist: set[str] = self._load_whitelist()
        self.stopped: bool = False
        self.deferred: dict[str, DeferredAlert] = {}
        self._active_agent_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Whitelist persistence
    # ------------------------------------------------------------------

    def _load_whitelist(self) -> set[str]:
        if self._whitelist_path.exists():
            try:
                return set(json.loads(self._whitelist_path.read_text()))
            except Exception:
                return set()
        return set()

    def _save_whitelist(self) -> None:
        self._whitelist_path.write_text(json.dumps(sorted(self.whitelist), indent=2))

    # ------------------------------------------------------------------
    # Mode persistence
    # ------------------------------------------------------------------

    def _persist_mode(self, mode: str) -> None:
        try:
            with open(self._config_path) as f:
                data = yaml.safe_load(f) or {}
            data.setdefault("controller", {})["mode"] = mode
            with open(self._config_path, "w") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        except Exception as exc:
            console.print(f"[yellow]Warning: could not persist mode to config: {exc}[/yellow]")

    # ------------------------------------------------------------------
    # Command detection
    # ------------------------------------------------------------------

    def is_command(self, text: str) -> bool:
        return text.lower().strip() in _COMMANDS

    # ------------------------------------------------------------------
    # Event routing
    # ------------------------------------------------------------------

    async def handle_event(self, event: dict) -> None:
        etype = event.get("type", "")
        source = event.get("source", "")

        if self.stopped and etype != "user_message":
            return

        if etype == "user_message":
            await self._run_agent_chat(event)
            return

        if self.mode == "monitor":
            await self._notify_only(event)
            return

        if source == "monitor":
            if etype == "services_down":
                await self._defer(event)
                return
            if etype == "service_recovered":
                await self._handle_recovery(event)
                return

        await self._run_agent(event)

    async def _notify_only(self, event: dict) -> None:
        etype = event.get("type", "")
        data = event.get("data", {})
        if etype == "services_down":
            services = [s["service"] for s in data.get("services", [])]
            await self._slack.notify(
                f"👁 Monitor alert: {', '.join(f'`{s}`' for s in services)} degraded. "
                f"(monitor-only mode — not acting)"
            )
        elif etype == "service_recovered":
            svc = data.get("service", "unknown")
            await self._slack.notify(f"✅ `{svc}` recovered.")

    async def _run_agent(self, event: dict) -> None:
        agent = self.agents["default"]
        task = asyncio.create_task(agent.handle_event(event))
        agent._active_task = task  # type: ignore[attr-defined]
        self._active_agent_task = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._active_agent_task = None
            agent._active_task = None  # type: ignore[attr-defined]

    async def _run_agent_chat(self, event: dict) -> None:
        source = event.get("source", "cli")
        message = event["data"]["message"]
        agent = self.agents["default"]
        task = asyncio.create_task(
            agent.chat(message, trigger=f"{source}:user_message")
        )
        agent._active_task = task  # type: ignore[attr-defined]
        self._active_agent_task = task
        try:
            response, _ = await task
            if source != "cli" and response:
                await self._slack.notify(response)
        except asyncio.CancelledError:
            pass
        finally:
            self._active_agent_task = None
            agent._active_task = None  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Control commands
    # ------------------------------------------------------------------

    async def handle_command(self, text: str) -> str:
        lower = text.lower().strip()
        if lower == "stop":
            return await self._cmd_stop()
        if lower == "start":
            return await self._cmd_start()
        if lower == "queue":
            return self._cmd_queue()
        if lower == "mode monitor":
            return await self._cmd_mode("monitor")
        if lower == "mode act":
            return await self._cmd_mode("act")
        return f"Unknown command: {text!r}"

    async def _cmd_stop(self) -> str:
        self.stopped = True
        for alert in list(self.deferred.values()):
            alert.timer_task.cancel()
        self.deferred.clear()
        if self._active_agent_task:
            self._active_agent_task.cancel()
        await self.agents["default"].cancel_all()
        return "🛑 Stopped. All pending work cancelled. Type `start` to resume."

    async def _cmd_start(self) -> str:
        self.stopped = False
        return "✅ Resumed."

    def _cmd_queue(self) -> str:
        if not self.deferred:
            return "No pending investigations in queue."
        now = datetime.now(timezone.utc)
        lines = [f"*{len(self.deferred)} pending investigation(s):*"]
        for alert in self.deferred.values():
            age = int((now - alert.deferred_at).total_seconds())
            remaining = max(0, self._grace_period - age)
            lines.append(
                f"• `{alert.alert_id}` — {', '.join(alert.services)} "
                f"— waiting {age}s, {remaining}s remaining"
            )
        return "\n".join(lines)

    async def _cmd_mode(self, mode: Literal["monitor", "act"]) -> str:
        self.mode = mode
        self._persist_mode(mode)
        if mode == "monitor":
            return "👁 Monitor-only mode. I'll notify but not act."
        return "⚡ Act mode. I'll investigate and propose actions."

    # ------------------------------------------------------------------
    # Grace period (stubs — completed in Task 7)
    # ------------------------------------------------------------------

    async def _defer(self, event: dict) -> None:
        raise NotImplementedError

    async def _handle_recovery(self, event: dict) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Whitelist management (completed in Task 9)
    # ------------------------------------------------------------------

    async def add_to_whitelist(self, command: str) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Alert resolution (completed in Task 7)
    # ------------------------------------------------------------------

    async def start_alert(self, alert_id: str) -> bool:
        raise NotImplementedError

    async def ignore_alert(self, alert_id: str) -> bool:
        raise NotImplementedError
```

- [ ] **Step 4: Run controller tests**

```bash
hatch run -e test pytest tests/test_controller.py -v
```
Expected: all routing and command tests PASS; grace period / whitelist tests not yet written

- [ ] **Step 5: Commit**

```bash
git add controller.py tests/test_controller.py
git commit -m "feat: add AgentController skeleton with mode, stop/start, queue, mode commands"
```

---

## Task 6: Slack additions

**Files:**
- Modify: `agent/agent/slack.py`

- [ ] **Step 1: Add `notify_deferred_alert()` to `SlackClient`**

Add after `notify_plan()`:

```python
async def notify_deferred_alert(
    self,
    alert_id: str,
    services: list[str],
    grace_seconds: int,
) -> tuple[str, str] | None:
    """Post a grace-period alert with [Start Now] / [Ignore] buttons."""
    minutes = grace_seconds // 60
    service_list = ", ".join(f"`{s}`" for s in services)
    text = (
        f"⚠️ *{len(services)} service(s) degraded:* {service_list}\n"
        f"Starting investigation in {minutes} minute(s). Act now or ignore?"
    )
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "▶ Start Now"},
                    "style": "primary",
                    "action_id": "alert_start",
                    "value": alert_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✖ Ignore"},
                    "style": "danger",
                    "action_id": "alert_ignore",
                    "value": alert_id,
                },
            ],
        },
    ]
    result = await self._post_message(blocks, text=f"Service alert: {alert_id}")
    channel = result.get("channel")
    ts = result.get("ts")
    if channel and ts:
        return channel, ts
    return None
```

- [ ] **Step 2: Update `_plan_blocks()` and `notify_plan()` to support [Approve + Whitelist]**

Replace the existing `_plan_blocks` static method:

```python
@staticmethod
def _plan_blocks(
    plan_id: str,
    plan_text: str,
    veto_seconds: int | None,
    tool_name: str = "",
    command: str = "",
) -> list:
    timeout_note = (
        f"\n_Auto-cancels in {veto_seconds}s if no response._"
        if veto_seconds is not None
        else "\n_Waiting indefinitely for explicit approval._"
    )
    approve_elements: list[dict] = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Approve"},
            "style": "primary",
            "action_id": "plan_approve",
            "value": plan_id,
        },
    ]
    if tool_name == "run_shell" and command:
        import json as _json
        approve_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Approve + Whitelist"},
            "style": "primary",
            "action_id": "plan_approve_whitelist",
            "value": _json.dumps({"plan_id": plan_id, "command": command}),
        })
    approve_elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "❌ Deny"},
        "style": "danger",
        "action_id": "plan_deny",
        "value": plan_id,
    })
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⏳ Plan proposed"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Plan ID:* `{plan_id}`\n{plan_text}{timeout_note}"},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": approve_elements,
        },
    ]
```

Replace the existing `notify_plan()`:

```python
async def notify_plan(
    self,
    plan_id: str,
    plan_text: str,
    veto_seconds: int | None,
    tool_name: str = "",
    command: str = "",
) -> tuple[str, str] | None:
    """Post the plan message. Returns (channel_id, ts) for later updates."""
    blocks = self._plan_blocks(plan_id, plan_text, veto_seconds, tool_name, command)
    result = await self._post_message(blocks, text=f"Plan proposed: {plan_id}")
    channel = result.get("channel")
    ts = result.get("ts")
    if channel and ts:
        return channel, ts
    return None
```

- [ ] **Step 3: Update the `notify_plan` call in `agent/agent/agent.py`**

In `_handle_approval_flow`, find the `notify_plan` call and update it:

```python
message_ref = await self._slack.notify_plan(
    plan_id,
    plan_text,
    veto_seconds,
    tool_name=block.name,
    command=tool_input.get("command", ""),
)
```

- [ ] **Step 4: Run full test suite**

```bash
hatch run -e test pytest -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agent/agent/slack.py agent/agent/agent.py
git commit -m "feat: add notify_deferred_alert and Approve+Whitelist button to Slack client"
```

---

## Task 7: Grace period + self-healed RAG

**Files:**
- Modify: `controller.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_controller.py`:

```python
# --- grace period ---

@pytest.mark.asyncio
async def test_defer_creates_deferred_alert(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=10, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "sonarr_sonarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    assert len(controller.deferred) == 1
    alert = list(controller.deferred.values())[0]
    assert "sonarr_sonarr" in alert.services
    alert.timer_task.cancel()  # cleanup


@pytest.mark.asyncio
async def test_grace_period_timer_fires_agent(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=0, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "sonarr_sonarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    await asyncio.sleep(0.05)  # let timer fire
    agent.handle_event.assert_called_once()
    assert len(controller.deferred) == 0


@pytest.mark.asyncio
async def test_start_alert_fires_agent_immediately(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "radarr_radarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    alert_id = list(controller.deferred.keys())[0]
    result = await controller.start_alert(alert_id)
    assert result is True
    await asyncio.sleep(0.05)
    agent.handle_event.assert_called_once()
    assert len(controller.deferred) == 0


@pytest.mark.asyncio
async def test_ignore_alert_cancels_without_agent(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "radarr_radarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    alert_id = list(controller.deferred.keys())[0]
    result = await controller.ignore_alert(alert_id)
    assert result is True
    agent.handle_event.assert_not_called()
    assert len(controller.deferred) == 0


@pytest.mark.asyncio
async def test_self_healed_writes_rag_incident(tmp_path) -> None:
    rag = AsyncMock()
    rag.count_incidents = AsyncMock(return_value=5)
    rag.store_incident = AsyncMock()
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    controller._rag = rag

    # First defer an alert
    down_event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "jellyfin_jellyfin", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(down_event)

    # Then recover
    recovery_event = {
        "source": "monitor", "type": "service_recovered",
        "data": {"service": "jellyfin_jellyfin", "down_duration_seconds": 45},
    }
    await controller.handle_event(recovery_event)

    rag.store_incident.assert_called_once()
    call_args = rag.store_incident.call_args[0][0]
    assert "self-healed" in call_args["tags"]
    assert "jellyfin_jellyfin" in call_args["title"]
    agent.handle_event.assert_not_called()
    slack.notify.assert_called()


@pytest.mark.asyncio
async def test_recovery_with_no_deferred_alert_goes_to_agent(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    # No deferred alert — recovery goes straight to agent
    recovery_event = {
        "source": "monitor", "type": "service_recovered",
        "data": {"service": "jellyfin_jellyfin", "down_duration_seconds": 300},
    }
    await controller.handle_event(recovery_event)
    agent.handle_event.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_queue_shows_pending_alert(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "sonarr_sonarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    result = await controller.handle_command("queue")
    assert "sonarr_sonarr" in result
    # cleanup
    for alert in controller.deferred.values():
        alert.timer_task.cancel()
```

- [ ] **Step 2: Run to confirm failures**

```bash
hatch run -e test pytest tests/test_controller.py -k "grace or defer or start_alert or ignore_alert or healed or recovery or queue_shows" -v
```
Expected: FAIL — `NotImplementedError`

- [ ] **Step 3: Implement `_defer`, `_grace_period_timer`, `_handle_recovery`, `_write_self_healed_incident`, `start_alert`, `ignore_alert` in `controller.py`**

Replace the stub methods:

```python
async def _defer(self, event: dict) -> None:
    alert_id = f"alert-{secrets.token_hex(4)}"
    services = [s["service"] for s in event["data"].get("services", [])]
    message_ref = await self._slack.notify_deferred_alert(
        alert_id, services, self._grace_period
    )
    timer = asyncio.create_task(self._grace_period_timer(alert_id))
    self.deferred[alert_id] = DeferredAlert(
        alert_id=alert_id,
        event=event,
        services=services,
        timer_task=timer,
        slack_message_ref=message_ref,
        deferred_at=datetime.now(timezone.utc),
    )

async def _grace_period_timer(self, alert_id: str) -> None:
    await asyncio.sleep(self._grace_period)
    alert = self.deferred.pop(alert_id, None)
    if alert is not None:
        await self._run_agent(alert.event)

async def _handle_recovery(self, event: dict) -> None:
    service = event["data"]["service"]
    duration = event["data"].get("down_duration_seconds", 0)
    for alert_id, alert in list(self.deferred.items()):
        if service in alert.services:
            alert.timer_task.cancel()
            del self.deferred[alert_id]
            await self._slack.notify(
                f"✅ `{service}` recovered on its own after {duration}s — no investigation needed."
            )
            if self._rag is not None:
                await self._write_self_healed_incident(service, duration)
            return
    # No deferred alert — agent was already working on it
    await self._run_agent(event)

async def _write_self_healed_incident(self, service: str, duration: int) -> None:
    count = await self._rag.count_incidents()
    inc_id = f"INC-{count + 1:04d}"
    now = datetime.now(timezone.utc)
    await self._rag.store_incident({
        "id": inc_id,
        "title": f"{service}-self-healed",
        "date": now,
        "tags": ["recovery", "self-healed"],
        "inciting_incident": (
            f"`{service}` degraded and recovered without agent intervention after {duration}s."
        ),
        "resolution": "Cluster software self-healed the service. No agent action taken.",
        "tools_used": [],
    })

async def start_alert(self, alert_id: str) -> bool:
    alert = self.deferred.pop(alert_id, None)
    if alert is None:
        return False
    alert.timer_task.cancel()
    asyncio.create_task(self._run_agent(alert.event))
    return True

async def ignore_alert(self, alert_id: str) -> bool:
    alert = self.deferred.pop(alert_id, None)
    if alert is None:
        return False
    alert.timer_task.cancel()
    return True
```

- [ ] **Step 4: Run all controller tests**

```bash
hatch run -e test pytest tests/test_controller.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add controller.py tests/test_controller.py
git commit -m "feat: implement grace period, self-healed RAG incidents, alert start/ignore"
```

---

## Task 8: build_approval_app() controller wiring

**Files:**
- Modify: `agent/agent/agent.py`

- [ ] **Step 1: Update `build_approval_app` signature and handler**

In `agent/agent/agent.py`, update the function signature:

```python
def build_approval_app(
    pending: PendingApprovals,
    slack: "SlackClient",
    event_queue: asyncio.Queue | None = None,
    controller: Any = None,
) -> FastAPI:
```

Add `from typing import Any` to the imports at the top of `agent.py` if not already present.

Inside `build_approval_app`, in the `block_actions` handler, add handling for the new action IDs. Find the existing `for action in payload.get("actions", []):` loop and extend it:

```python
for action in payload.get("actions", []):
    action_id = action.get("action_id")
    value = action.get("value", "")

    # --- Deferred alert buttons ---
    if action_id == "alert_start":
        if controller is not None:
            await controller.start_alert(value)
        return Response(content="", status_code=200)

    if action_id == "alert_ignore":
        if controller is not None:
            await controller.ignore_alert(value)
        return Response(content="", status_code=200)

    # --- Plan approval buttons ---
    if action_id not in ("plan_approve", "plan_deny", "plan_approve_whitelist"):
        continue

    if action_id == "plan_approve_whitelist":
        import json as _json
        data = _json.loads(value) if value else {}
        plan_id = data.get("plan_id", "")
        command = data.get("command", "")
        if command and controller is not None:
            await controller.add_to_whitelist(command)
        channel = payload.get("channel", {}).get("id", "")
        ts = payload.get("message", {}).get("ts", "")
        user = payload.get("user", {}).get("name", "slack")
        if channel and ts:
            plan_text = ""
            for block in payload.get("message", {}).get("blocks", []):
                if block.get("type") == "section":
                    plan_text = block.get("text", {}).get("text", "")
                    break
            await slack.resolve_plan_message(channel, ts, plan_id, plan_text, True, "", user)
        pending.resolve(plan_id, True, reason="")
        return Response(content="", status_code=200)

    plan_id = value
    # ... rest of existing plan_approve / plan_deny logic (unchanged)
```

- [ ] **Step 2: Update `start_approval_listener` to accept and pass controller**

In `HomelabAgent.start_approval_listener`:

```python
async def start_approval_listener(
    self,
    host: str,
    port: int,
    event_queue: asyncio.Queue | None = None,
    controller: Any = None,
) -> tuple[asyncio.Task, uvicorn.Server]:
    host = _resolve_listener_host(host, self._slack.signature_verification_enabled)
    app = build_approval_app(self._pending, self._slack, event_queue, controller)
    server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(server_config)

    async def _serve() -> None:
        await server.serve()

    task = asyncio.create_task(_serve())
    return task, server
```

- [ ] **Step 3: Run full test suite**

```bash
hatch run -e test pytest -v
```
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add agent/agent/agent.py
git commit -m "feat: wire controller into build_approval_app for alert and whitelist interactions"
```

---

## Task 9: SafetyPolicy whitelist

**Files:**
- Modify: `agent/agent/safety.py`
- Create: `whitelist.json`
- Modify: `tests/test_safety.py`
- Modify: `controller.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_safety.py`:

```python
# --- whitelist ---

def make_policy_with_whitelist(commands: list[str]) -> SafetyPolicy:
    policy = make_policy()
    policy.whitelist = set(commands)
    return policy


def test_whitelisted_command_is_tier1() -> None:
    policy = make_policy_with_whitelist(["df -h"])
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=3,
        agent_reasoning="should be overridden by whitelist",
        command="df -h",
    )
    assert resolved.tier == 1


def test_non_whitelisted_command_unaffected() -> None:
    policy = make_policy_with_whitelist(["df -h"])
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        agent_reasoning="read-only",
        command="free -h",
    )
    assert resolved.tier == 1  # passes through normally


def test_whitelist_does_not_apply_to_non_shell_tools() -> None:
    policy = make_policy_with_whitelist(["df -h"])
    resolved = policy.resolve_tier(tool_name="read_file")
    assert resolved.tier == 1  # read_file is tier 1 by default — unrelated to whitelist


def test_safe_mode_overrides_whitelist() -> None:
    policy = make_policy(global_safe_mode=True)
    policy.whitelist = {"df -h"}
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        agent_reasoning="read-only",
        command="df -h",
    )
    assert resolved.tier == 3
    assert resolved.safe_mode_active is True


def test_update_whitelist_replaces_set() -> None:
    policy = make_policy_with_whitelist(["df -h"])
    policy.update_whitelist({"free -h", "uptime"})
    assert policy.whitelist == {"free -h", "uptime"}
    assert "df -h" not in policy.whitelist
```

- [ ] **Step 2: Run to confirm failures**

```bash
hatch run -e test pytest tests/test_safety.py -k "whitelist" -v
```
Expected: FAIL

- [ ] **Step 3: Add whitelist to `SafetyPolicy`**

In `agent/agent/safety.py`, in `SafetyPolicy.__init__`, add after `self._shell_force_tier2_patterns`:

```python
self.whitelist: set[str] = set()
```

Add a new method after `__init__`:

```python
def update_whitelist(self, commands: set[str]) -> None:
    self.whitelist = commands
```

In `resolve_tier`, add the whitelist check immediately after the safe-mode blocks (before `_base_tier` is called). Find the section after the `_resource_in_safe_mode` block and add:

```python
# Whitelisted commands are always tier 1 (safe mode already checked above)
if tool_name == "run_shell" and command and command in self.whitelist:
    return ResolvedTier(
        tier=1,
        safe_mode_active=False,
        original_tier=None,
        agent_reasoning=agent_reasoning,
    )
```

- [ ] **Step 4: Implement `add_to_whitelist` in `controller.py`**

Replace the stub:

```python
async def add_to_whitelist(self, command: str) -> None:
    self.whitelist.add(command)
    self._save_whitelist()
    agent = self.agents["default"]
    if hasattr(agent, "_safety"):
        agent._safety.update_whitelist(self.whitelist)  # type: ignore[attr-defined]
    await self._slack.notify(f"✅ Added to whitelist: `{command}`")
```

- [ ] **Step 5: Create `whitelist.json`**

```bash
echo '[]' > /home/chris/src/homelab/agent/whitelist.json
```

- [ ] **Step 6: Run all safety tests**

```bash
hatch run -e test pytest tests/test_safety.py -v
```
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add agent/agent/safety.py controller.py whitelist.json tests/test_safety.py
git commit -m "feat: add command whitelist to SafetyPolicy and AgentController"
```

---

## Task 10: Wire up cli.py

**Files:**
- Modify: `cli.py`

- [ ] **Step 1: Update `event_consumer` to use the controller**

Replace the existing `event_consumer` function:

```python
async def event_consumer(controller: "AgentController", event_queue: asyncio.Queue) -> None:
    while True:
        event = await event_queue.get()
        try:
            await controller.handle_event(event)
        except Exception as exc:
            console.print(f"\n[bold red]Event consumer error:[/bold red] {exc}")
        finally:
            event_queue.task_done()
```

Add to the imports at the top of `cli.py`:

```python
from controller import AgentController
```

- [ ] **Step 2: Update `amain` to create the controller and pass it through**

In `amain`, after `agent = HomelabAgent(config)` and the RAG init block, add:

```python
controller = AgentController(
    config=config,
    agents={"default": agent},
    slack=agent._slack,
    config_path=args.config,
    rag=agent._rag,
)
# Push whitelist into SafetyPolicy on startup
agent._safety.update_whitelist(controller.whitelist)
```

Change the `consumer_task` line from:

```python
consumer_task = asyncio.create_task(event_consumer(agent, event_queue))
```

to:

```python
consumer_task = asyncio.create_task(event_consumer(controller, event_queue))
```

- [ ] **Step 3: Update the Slack events handler to detect control commands**

In `amain`, update the `start_approval_listener` calls to pass the controller:

```python
listener_task, listener_server = await agent.start_approval_listener(
    listener_host, listener_port, event_queue, controller=controller
)
```

In `agent/agent/agent.py`'s `build_approval_app`, in the `/slack/events` handler, update the message routing to detect control commands:

```python
if body.get("type") == "event_callback":
    event = body.get("event", {})
    if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "").strip()
        if text and event_queue is not None:
            if controller is not None and controller.is_command(text):
                response = await controller.handle_command(text)
                await slack.notify(response)
            else:
                await event_queue.put({
                    "source": "slack",
                    "type": "user_message",
                    "data": {"message": text},
                    "timestamp": datetime.now(timezone.utc),
                })
```

- [ ] **Step 4: Update `run_repl` to remove the old event_consumer signature reference**

The REPL still interacts with `agent._pending` for approvals (unchanged). The only change: the `event_queue.put` calls in `run_repl` are already correct — they just enqueue `user_message` events which the controller routes to `_run_agent_chat`. No change needed to REPL logic.

- [ ] **Step 5: Update the `--check` path in `main()` (no controller needed there)**

Verify the `--check` path still works — it calls `asyncio.run(run_check(config))` directly and does not use the controller.

```bash
hatch run check
```
Expected: service list printed, no errors

- [ ] **Step 6: Run the full test suite**

```bash
hatch run -e test pytest -v
```
Expected: all PASS

- [ ] **Step 7: Start the agent briefly to verify it boots without error**

```bash
timeout 3 hatch run agent --config config.yaml 2>&1 || true
```
Expected: agent starts, prints `Homelab Agent — type /quit to exit`, exits cleanly after 3s

- [ ] **Step 8: Commit**

```bash
git add cli.py agent/agent/agent.py
git commit -m "feat: wire AgentController into cli.py — event consumer, Slack commands, approval listener"
```

---

## Task 11: Final integration commit

- [ ] **Step 1: Run full test suite one last time**

```bash
hatch run -e test pytest -v
```
Expected: all PASS

- [ ] **Step 2: Validate config**

```bash
hatch run config validate
```
Expected: `Config is valid.`

- [ ] **Step 3: Push**

```bash
git push
```
