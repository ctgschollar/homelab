# Fix 3: Concurrent Shell Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize concurrent tier-1 `run_shell` tool calls while keeping non-shell tier-1 tools concurrent.

**Architecture:** Split `tier1_blocks` in `_handle_tool_calls` into two lists — `tier1_shell_blocks` (name == `"run_shell"`) and `tier1_safe_blocks` (all other tier-1 tools) — then execute them in two phases: a plain sequential `for` loop for shell blocks (Phase 1), followed by `asyncio.gather` for safe blocks (Phase 2). A new `max_concurrent_shell` field is added to `SafetyConfig` and `SafetyPolicy` to document the operator's intent, with the default value of `1` (sequential); the field is wired and validated but semaphore-based parallelism is deferred to a future release.

**Tech Stack:** Python 3.11, asyncio, pytest, pytest-asyncio

---

## Task 1: Add `max_concurrent_shell` to config schema and YAML

**Files modified:**
- `/home/chris/src/homelab/agent/agent/config_schema.py`
- `/home/chris/src/homelab/agent/agent/safety.py`
- `/home/chris/src/homelab/agent/config.yaml`

**New test file:** `/home/chris/src/homelab/agent/tests/test_fix3_shell_gate.py`

### Step 1.1 — Write tests first

- [ ] Create `/home/chris/src/homelab/agent/tests/test_fix3_shell_gate.py` with tests for the config schema and SafetyPolicy changes (Tests 5 and 6 from the spec).

```python
"""Tests for Fix 3: Concurrent Shell Gate — config schema and SafetyPolicy."""
import pytest
from pydantic import ValidationError

from agent.agent.config_schema import SafetyConfig, SafeModeResourcesConfig
from agent.agent.safety import SafetyPolicy


def _make_safety_config(**kwargs) -> SafetyConfig:
    defaults = {
        "global_safe_mode": False,
        "safe_mode_resources": SafeModeResourcesConfig(),
        "tool_tiers": {"run_shell": "agent", "read_logs": 1},
        "log_agent_tier_reasoning": False,
    }
    defaults.update(kwargs)
    return SafetyConfig(**defaults)


def _make_agent_config(safety_config: SafetyConfig):
    """Build a minimal AgentConfig stub with only the safety field populated."""
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.safety = safety_config
    return cfg


class TestMaxConcurrentShellConfig:
    def test_default_is_one(self) -> None:
        """SafetyConfig.max_concurrent_shell defaults to 1."""
        cfg = _make_safety_config()
        assert cfg.max_concurrent_shell == 1

    def test_accepts_value_greater_than_one(self) -> None:
        """SafetyConfig accepts max_concurrent_shell=3 without error."""
        cfg = _make_safety_config(max_concurrent_shell=3)
        assert cfg.max_concurrent_shell == 3

    def test_rejects_zero(self) -> None:
        """SafetyConfig rejects max_concurrent_shell=0 (ge=1 constraint)."""
        with pytest.raises(ValidationError):
            _make_safety_config(max_concurrent_shell=0)

    def test_rejects_negative(self) -> None:
        """SafetyConfig rejects negative max_concurrent_shell."""
        with pytest.raises(ValidationError):
            _make_safety_config(max_concurrent_shell=-1)


class TestSafetyPolicyMaxConcurrentShell:
    def test_attribute_assigned_from_config(self) -> None:
        """SafetyPolicy exposes max_concurrent_shell from config."""
        safety_cfg = _make_safety_config(max_concurrent_shell=3)
        agent_cfg = _make_agent_config(safety_cfg)
        policy = SafetyPolicy(agent_cfg)
        assert policy.max_concurrent_shell == 3

    def test_default_value_propagates(self) -> None:
        """SafetyPolicy.max_concurrent_shell is 1 when config uses default."""
        safety_cfg = _make_safety_config()
        agent_cfg = _make_agent_config(safety_cfg)
        policy = SafetyPolicy(agent_cfg)
        assert policy.max_concurrent_shell == 1
```

- [ ] Run tests (expect failures — production code not yet changed):

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix3_shell_gate.py -v -k "TestMaxConcurrentShellConfig or TestSafetyPolicyMaxConcurrentShell"
```

### Step 1.2 — Add field to `SafetyConfig`

- [ ] Edit `/home/chris/src/homelab/agent/agent/config_schema.py`: add `max_concurrent_shell` to `SafetyConfig` after `log_agent_tier_reasoning`.

```python
class SafetyConfig(BaseModel):
    global_safe_mode: bool
    safe_mode_resources: SafeModeResourcesConfig
    tool_tiers: dict[str, TierValue]
    log_agent_tier_reasoning: bool
    max_concurrent_shell: int = Field(ge=1, default=1)
```

### Step 1.3 — Expose attribute in `SafetyPolicy`

- [ ] Edit `/home/chris/src/homelab/agent/agent/safety.py`: add `self.max_concurrent_shell` to `SafetyPolicy.__init__` after `self.log_agent_tier_reasoning`, following the existing pattern.

```python
def __init__(self, config: AgentConfig) -> None:
    self.global_safe_mode: bool = config.safety.global_safe_mode
    # ... existing lines ...
    self.log_agent_tier_reasoning: bool = config.safety.log_agent_tier_reasoning
    self.max_concurrent_shell: int = config.safety.max_concurrent_shell
```

### Step 1.4 — Add field to `config.yaml`

- [ ] Edit `/home/chris/src/homelab/agent/config.yaml`: add `max_concurrent_shell` under the `safety:` block, immediately after `log_agent_tier_reasoning`.

```yaml
  # Maximum number of tier-1 run_shell calls that may execute concurrently.
  # Default 1 (sequential). Values > 1 are accepted but currently still run
  # sequentially — semaphore-based parallelism is reserved for a future release.
  max_concurrent_shell: 1
```

### Step 1.5 — Run tests (expect pass)

- [ ] Run the full test suite:

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
```

### Step 1.6 — Commit

```
cd /home/chris/src/homelab/agent && git add agent/config_schema.py agent/safety.py config.yaml tests/test_fix3_shell_gate.py && git commit -m "feat: add max_concurrent_shell to SafetyConfig and SafetyPolicy"
```

---

## Task 2: Separate `run_shell` blocks from other tier-1 blocks in `_handle_tool_calls`

**Files modified:**
- `/home/chris/src/homelab/agent/agent/agent.py`

**Test file updated:** `/home/chris/src/homelab/agent/tests/test_fix3_shell_gate.py`

### Step 2.1 — Write tests first

- [ ] Add the following test classes to `/home/chris/src/homelab/agent/tests/test_fix3_shell_gate.py`.

The tests exercise `_handle_tool_calls` directly by constructing a minimal `HomelabAgent` with mocked dependencies. The `_tools.execute` async mock records call order. `asyncio.gather` is patched at `"agent.agent.agent.asyncio.gather"` (where it is used), not at `"asyncio.gather"`.

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import pytest_asyncio

from agent.agent.agent import HomelabAgent
from agent.agent.config_schema import AgentConfig


def _fake_tool_use_block(block_id: str, name: str, input_data: dict | None = None):
    block = MagicMock()
    block.type = "tool_use"
    block.id = block_id
    block.name = name
    block.input = input_data or {}
    return block


def _make_agent() -> HomelabAgent:
    """Build a HomelabAgent with fully mocked I/O dependencies."""
    cfg = MagicMock(spec=AgentConfig)
    cfg.anthropic.model = "claude-test"
    cfg.anthropic.api_key = "fake"
    cfg.anthropic.input_cost_per_mtok = 3.0
    cfg.anthropic.output_cost_per_mtok = 15.0
    cfg.slack.bot_token = None
    cfg.slack.signing_secret = None
    cfg.slack.channel = "#test"
    cfg.slack.veto_window_seconds = 300
    cfg.action_log.path = "/tmp/test_action.log"
    cfg.history.path = "/tmp/test_history.json"
    cfg.safety.global_safe_mode = False
    cfg.safety.safe_mode_resources.stacks = []
    cfg.safety.safe_mode_resources.services = []
    cfg.safety.safe_mode_resources.nodes = []
    cfg.safety.tool_tiers = {
        "run_shell": "agent",
        "read_logs": 1,
    }
    cfg.safety.log_agent_tier_reasoning = False
    cfg.safety.max_concurrent_shell = 1
    agent = HomelabAgent.__new__(HomelabAgent)
    agent._config = cfg
    agent._model = "claude-test"
    from agent.agent.safety import SafetyPolicy
    agent._safety = SafetyPolicy(cfg)
    agent._tools = MagicMock()
    agent._logger = MagicMock()
    agent._logger.log_action_taken = AsyncMock()
    agent._logger.log_tier_reasoning = AsyncMock()
    agent._pending = MagicMock()
    agent._history = []
    return agent


class TestShellBlocksSequential:
    @pytest.mark.asyncio
    async def test_two_shell_blocks_execute_sequentially(self) -> None:
        """Two tier-1 run_shell calls must execute one after the other."""
        agent = _make_agent()
        call_order = []

        async def fake_execute(tool_name: str, tool_input: dict) -> str:
            call_order.append(tool_input.get("command", tool_name))
            return f"result:{tool_input.get('command', tool_name)}"

        agent._tools.execute = fake_execute

        blocks = [
            _fake_tool_use_block("b1", "run_shell", {"command": "cmd1", "agent_proposed_tier": 1}),
            _fake_tool_use_block("b2", "run_shell", {"command": "cmd2", "agent_proposed_tier": 1}),
        ]

        results = await agent._handle_tool_calls(blocks, trigger="test")

        assert len(results) == 2
        assert call_order == ["cmd1", "cmd2"]
        assert results[0]["tool_use_id"] == "b1"
        assert results[1]["tool_use_id"] == "b2"
        assert "cmd1" in results[0]["content"]
        assert "cmd2" in results[1]["content"]

    @pytest.mark.asyncio
    async def test_shell_executes_before_gather_phase(self) -> None:
        """run_shell (Phase 1) completes before read_logs gather (Phase 2)."""
        agent = _make_agent()
        phase_log = []

        async def fake_execute(tool_name: str, tool_input: dict) -> str:
            phase_log.append(tool_name)
            return f"result:{tool_name}"

        agent._tools.execute = fake_execute

        blocks = [
            _fake_tool_use_block("b1", "run_shell", {"command": "ls", "agent_proposed_tier": 1}),
            _fake_tool_use_block("b2", "read_logs", {"service_name": "traefik"}),
        ]

        results = await agent._handle_tool_calls(blocks, trigger="test")

        assert len(results) == 2
        # run_shell must appear before read_logs in execution order
        assert phase_log.index("run_shell") < phase_log.index("read_logs")
        assert results[0]["tool_use_id"] == "b1"
        assert results[1]["tool_use_id"] == "b2"


class TestNonShellBlocksConcurrent:
    @pytest.mark.asyncio
    async def test_two_safe_blocks_use_gather(self) -> None:
        """Two tier-1 non-shell blocks must be dispatched via asyncio.gather."""
        agent = _make_agent()

        async def fake_execute(tool_name: str, tool_input: dict) -> str:
            return f"result:{tool_name}"

        agent._tools.execute = fake_execute

        blocks = [
            _fake_tool_use_block("b1", "read_logs", {"service_name": "traefik"}),
            _fake_tool_use_block("b2", "read_logs", {"service_name": "postgres"}),
        ]

        with patch("agent.agent.agent.asyncio.gather", wraps=asyncio.gather) as mock_gather:
            results = await agent._handle_tool_calls(blocks, trigger="test")

        assert len(results) == 2
        mock_gather.assert_called_once()
        # gather must have received two coroutine arguments
        gather_args = mock_gather.call_args[0]
        assert len(gather_args) == 2


class TestResultOrdering:
    @pytest.mark.asyncio
    async def test_results_in_original_block_order(self) -> None:
        """Results must appear in tool_use_block order: shell, read_logs, shell."""
        agent = _make_agent()

        async def fake_execute(tool_name: str, tool_input: dict) -> str:
            cmd = tool_input.get("command") or tool_input.get("service_name") or tool_name
            return f"result:{cmd}"

        agent._tools.execute = fake_execute

        blocks = [
            _fake_tool_use_block("b1", "run_shell", {"command": "cmd1", "agent_proposed_tier": 1}),
            _fake_tool_use_block("b2", "read_logs", {"service_name": "traefik"}),
            _fake_tool_use_block("b3", "run_shell", {"command": "cmd3", "agent_proposed_tier": 1}),
        ]

        results = await agent._handle_tool_calls(blocks, trigger="test")

        assert len(results) == 3
        assert results[0]["tool_use_id"] == "b1"
        assert results[1]["tool_use_id"] == "b2"
        assert results[2]["tool_use_id"] == "b3"
        assert "cmd1" in results[0]["content"]
        assert "traefik" in results[1]["content"]
        assert "cmd3" in results[2]["content"]
```

- [ ] Run tests (expect failures — agent.py not yet modified):

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_fix3_shell_gate.py -v -k "TestShellBlocksSequential or TestNonShellBlocksConcurrent or TestResultOrdering"
```

### Step 2.2 — Modify `_handle_tool_calls` in `agent.py`

The existing `if tier1_blocks:` block (lines 569–585 in the current file) must be replaced. The `_exec_tier1` inner function is moved outside the guard. The new structure implements three execution phases as described in the spec.

- [ ] Edit `/home/chris/src/homelab/agent/agent/agent.py`: replace the `if tier1_blocks:` block with the three-phase structure.

The current code to replace (lines 566–585):

```python
        results: dict[str, str] = {}

        # Gather tier-1 calls concurrently
        if tier1_blocks:
            async def _exec_tier1(b: Any) -> tuple[str, str]:
                self._print_tool_call(b, resolved_map[b.id])
                res = await self._tools.execute(b.name, b.input or {})
                await self._logger.log_action_taken(
                    tool=b.name,
                    tool_input=b.input or {},
                    outcome=res,
                    tier=resolved_map[b.id].tier,
                    safe_mode_active=resolved_map[b.id].safe_mode_active,
                    trigger=trigger,
                )
                return b.id, res

            gathered = await asyncio.gather(*[_exec_tier1(b) for b in tier1_blocks])
            for bid, res in gathered:
                results[bid] = res
```

Replace with:

```python
        results: dict[str, str] = {}

        async def _exec_tier1(b: Any) -> tuple[str, str]:
            self._print_tool_call(b, resolved_map[b.id])
            res = await self._tools.execute(b.name, b.input or {})
            await self._logger.log_action_taken(
                tool=b.name,
                tool_input=b.input or {},
                outcome=res,
                tier=resolved_map[b.id].tier,
                safe_mode_active=resolved_map[b.id].safe_mode_active,
                trigger=trigger,
            )
            return b.id, res

        # Split tier-1 blocks: shell runs sequentially, safe tools run concurrently
        tier1_shell_blocks = [b for b in tier1_blocks if b.name == "run_shell"]
        tier1_safe_blocks  = [b for b in tier1_blocks if b.name != "run_shell"]

        # Phase 1: tier-1 shell blocks — sequential regardless of max_concurrent_shell
        if self._safety.max_concurrent_shell == 1:
            # sequential loop (default)
            for b in tier1_shell_blocks:
                bid, res = await _exec_tier1(b)
                results[bid] = res
        else:
            # max_concurrent_shell > 1: still sequential for now
            # Future: replace with asyncio.Semaphore(self._safety.max_concurrent_shell)
            for b in tier1_shell_blocks:
                bid, res = await _exec_tier1(b)
                results[bid] = res

        # Phase 2: tier-1 safe blocks — concurrent via gather (unchanged behaviour)
        if tier1_safe_blocks:
            gathered = await asyncio.gather(*[_exec_tier1(b) for b in tier1_safe_blocks])
            for bid, res in gathered:
                results[bid] = res
```

### Step 2.3 — Run full test suite (expect all pass)

- [ ] Run all tests:

```
cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
```

### Step 2.4 — Commit

```
cd /home/chris/src/homelab/agent && git add agent/agent.py tests/test_fix3_shell_gate.py && git commit -m "feat: serialize tier-1 run_shell calls in _handle_tool_calls (Fix 3)"
```

---

## Summary of all files changed

| File | Change |
|---|---|
| `agent/agent/config_schema.py` | Add `max_concurrent_shell: int = Field(ge=1, default=1)` to `SafetyConfig` |
| `agent/agent/safety.py` | Add `self.max_concurrent_shell = config.safety.max_concurrent_shell` to `SafetyPolicy.__init__` |
| `agent/config.yaml` | Add `max_concurrent_shell: 1` under `safety:` after `log_agent_tier_reasoning` |
| `agent/agent/agent.py` | Move `_exec_tier1` outside guard; split `tier1_blocks` into shell/safe lists; add Phase 1 sequential loop; keep Phase 2 gather for safe tools |
| `agent/tests/test_fix3_shell_gate.py` | New file — 6 test classes covering config schema, SafetyPolicy attribute, sequential shell execution, gather path for safe tools, result ordering |

No other files are touched. Tier classification logic in `SafetyPolicy.resolve_tier` is unchanged. The mutating-block approval flow is unchanged.
