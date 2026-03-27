# Fix 1: Shell Command Guards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pattern-based shell command guards to `SafetyPolicy` that auto-escalate `run_shell` tier based on matching command patterns.

**Architecture:** Two module-level lists of compiled `re.Pattern` objects (`_SHELL_FORCE_TIER3`, `_SHELL_FORCE_TIER2`) act as hardcoded denylists. `SafetyPolicy.__init__` merges these with config-provided patterns into instance-level lists, and a new `_check_shell_command` method is called from `_base_tier` (and safe-mode paths in `resolve_tier`) to enforce the guards. Guard match metadata is surfaced through three new fields on `ResolvedTier` and forwarded to `ActionLogger.log_tier_reasoning` for audit logging.

**Tech Stack:** Python 3.11, Pydantic v2, pytest, pytest-asyncio

---

## Task 1: Add `ShellCommandGuardsConfig` to config schema

**Files modified:**
- `/home/chris/src/homelab/agent/agent/config_schema.py`
- `/home/chris/src/homelab/agent/config.yaml`

**Test file:** `/home/chris/src/homelab/agent/tests/test_config_schema.py` (create)

### Steps

- [ ] **Write failing tests** in `agent/tests/test_config_schema.py`:

```python
"""Tests for ShellCommandGuardsConfig and its integration into SafetyConfig."""
import pytest
from agent.config_schema import (
    SafetyConfig,
    SafeModeResourcesConfig,
    ShellCommandGuardsConfig,
)


def make_safety(**kwargs) -> SafetyConfig:
    defaults = dict(
        global_safe_mode=False,
        safe_mode_resources=SafeModeResourcesConfig(),
        tool_tiers={"run_shell": "agent"},
        log_agent_tier_reasoning=False,
    )
    defaults.update(kwargs)
    return SafetyConfig(**defaults)


def test_shell_command_guards_defaults_to_empty() -> None:
    safety = make_safety()
    assert safety.shell_command_guards.force_tier3 == []
    assert safety.shell_command_guards.force_tier2 == []


def test_shell_command_guards_accepts_patterns() -> None:
    guards = ShellCommandGuardsConfig(
        force_tier3=[r"my-nuke\.sh"],
        force_tier2=[r"my-deploy\.sh"],
    )
    safety = make_safety(shell_command_guards=guards)
    assert safety.shell_command_guards.force_tier3 == [r"my-nuke\.sh"]
    assert safety.shell_command_guards.force_tier2 == [r"my-deploy\.sh"]


def test_shell_command_guards_independent_instances() -> None:
    """Default factory must not share a mutable instance across SafetyConfig instances."""
    s1 = make_safety()
    s2 = make_safety()
    assert s1.shell_command_guards is not s2.shell_command_guards
```

- [ ] **Run tests (expect failure):**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_config_schema.py -v
```

Expected: `ImportError` — `ShellCommandGuardsConfig` does not exist yet.

- [ ] **Implement** — modify `agent/agent/config_schema.py`:

  Add `ShellCommandGuardsConfig` **before** `SafetyConfig` (immediately after `SafeModeResourcesConfig`):

  ```python
  class ShellCommandGuardsConfig(BaseModel):
      force_tier3: list[str] = []
      force_tier2: list[str] = []
  ```

  Update `SafetyConfig` to add the new field:

  ```python
  class SafetyConfig(BaseModel):
      global_safe_mode: bool
      safe_mode_resources: SafeModeResourcesConfig
      tool_tiers: dict[str, TierValue]
      log_agent_tier_reasoning: bool
      shell_command_guards: ShellCommandGuardsConfig = Field(default_factory=ShellCommandGuardsConfig)
  ```

- [ ] **Update** `agent/config.yaml` — add `shell_command_guards` inside the `safety:` block, after `log_agent_tier_reasoning: true`:

  ```yaml
    # Optional additional shell command guard patterns. Each entry is a Python
    # regex string. Patterns here are ADDED to the hardcoded defaults; they
    # never replace them.
    shell_command_guards:
      force_tier3: []  # e.g. ['my-dangerous-script\.sh']
      force_tier2: []  # e.g. ['my-deploy\.sh']
  ```

- [ ] **Run tests (expect pass):**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_config_schema.py -v
```

- [ ] **Commit:**

```bash
cd /home/chris/src/homelab && git add agent/agent/config_schema.py agent/config.yaml agent/tests/test_config_schema.py && git commit -m "$(cat <<'EOF'
feat: add ShellCommandGuardsConfig to SafetyConfig schema

Adds ShellCommandGuardsConfig sub-model with force_tier3 and force_tier2
pattern lists. Nests it in SafetyConfig with a default_factory default.
Adds shell_command_guards section to config.yaml (empty by default).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `_check_shell_command` and `_last_guard_match` to `SafetyPolicy`

**Files modified:**
- `/home/chris/src/homelab/agent/agent/safety.py`

**Test file:** `/home/chris/src/homelab/agent/tests/test_safety.py` (create)

### Steps

- [ ] **Write failing tests** in `agent/tests/test_safety.py`:

```python
"""Tests for SafetyPolicy shell command guards (_check_shell_command)."""
import pytest
from agent.config_schema import (
    AgentConfig,
    SafetyConfig,
    SafeModeResourcesConfig,
    ShellCommandGuardsConfig,
)
from agent.safety import SafetyPolicy


def make_policy(
    extra_tier3: list[str] | None = None,
    extra_tier2: list[str] | None = None,
    global_safe_mode: bool = False,
) -> SafetyPolicy:
    guards = ShellCommandGuardsConfig(
        force_tier3=extra_tier3 or [],
        force_tier2=extra_tier2 or [],
    )
    safety = SafetyConfig(
        global_safe_mode=global_safe_mode,
        safe_mode_resources=SafeModeResourcesConfig(),
        tool_tiers={"run_shell": "agent"},
        log_agent_tier_reasoning=False,
        shell_command_guards=guards,
    )
    config = AgentConfig.model_construct(safety=safety)
    return SafetyPolicy(config)


# --- _check_shell_command unit tests ---

def test_check_rm_rf_returns_3() -> None:
    policy = make_policy()
    result = policy._check_shell_command("rm -rf /tmp/test", agent_proposed_tier=1)
    assert result == 3


def test_check_mkfs_returns_3() -> None:
    policy = make_policy()
    result = policy._check_shell_command("mkfs.ext4 /dev/sdb", agent_proposed_tier=1)
    assert result == 3


def test_check_dd_returns_3() -> None:
    policy = make_policy()
    result = policy._check_shell_command("dd if=/dev/zero of=/dev/sda", agent_proposed_tier=1)
    assert result == 3


def test_check_git_push_returns_min_tier2() -> None:
    policy = make_policy()
    result = policy._check_shell_command("git push origin main", agent_proposed_tier=1)
    assert result == 2


def test_check_git_push_does_not_lower_tier3() -> None:
    policy = make_policy()
    result = policy._check_shell_command("git push origin main", agent_proposed_tier=3)
    assert result == 3


def test_check_df_passthrough() -> None:
    policy = make_policy()
    result = policy._check_shell_command("df -h", agent_proposed_tier=1)
    assert result == 1


def test_check_systemctl_restart_returns_min_tier2() -> None:
    policy = make_policy()
    result = policy._check_shell_command("systemctl restart nginx", agent_proposed_tier=1)
    assert result == 2


def test_check_sed_i_returns_min_tier2() -> None:
    policy = make_policy()
    result = policy._check_shell_command("sed -i 's/foo/bar/' file.conf", agent_proposed_tier=1)
    assert result == 2


# --- _last_guard_match side-channel ---

def test_last_guard_match_set_on_tier3_match() -> None:
    policy = make_policy()
    policy._check_shell_command("rm -rf /tmp/test", agent_proposed_tier=1)
    assert policy._last_guard_match is not None
    list_name, pattern_str = policy._last_guard_match
    assert list_name == "force_tier3"
    assert isinstance(pattern_str, str)


def test_last_guard_match_set_on_tier2_match() -> None:
    policy = make_policy()
    policy._check_shell_command("git push origin main", agent_proposed_tier=1)
    assert policy._last_guard_match is not None
    list_name, pattern_str = policy._last_guard_match
    assert list_name == "force_tier2"
    assert isinstance(pattern_str, str)


def test_last_guard_match_cleared_on_no_match() -> None:
    policy = make_policy()
    # First call sets it
    policy._check_shell_command("rm -rf /tmp", agent_proposed_tier=1)
    assert policy._last_guard_match is not None
    # Second call should clear it
    policy._check_shell_command("df -h", agent_proposed_tier=1)
    assert policy._last_guard_match is None


# --- config-provided extra patterns ---

def test_config_extra_tier3_pattern() -> None:
    policy = make_policy(extra_tier3=[r"my-nuke\.sh"])
    result = policy._check_shell_command("my-nuke.sh", agent_proposed_tier=1)
    assert result == 3


def test_config_extra_tier2_pattern() -> None:
    policy = make_policy(extra_tier2=[r"my-deploy\.sh"])
    result = policy._check_shell_command("my-deploy.sh", agent_proposed_tier=1)
    assert result == 2


def test_hardcoded_defaults_not_replaced_by_config() -> None:
    """Config-extra patterns are additive; hardcoded defaults still apply."""
    policy = make_policy(extra_tier2=[r"my-deploy\.sh"])
    result = policy._check_shell_command("git push origin main", agent_proposed_tier=1)
    assert result == 2
```

- [ ] **Run tests (expect failure):**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_safety.py -v
```

Expected: `AttributeError` — `_check_shell_command` and `_last_guard_match` do not exist yet.

- [ ] **Implement** — modify `agent/agent/safety.py`:

  1. Add `import re` at the top of the file.

  2. Add the two module-level pattern lists immediately after `_DEFAULT_TIERS`:

  ```python
  _SHELL_FORCE_TIER3: list[re.Pattern] = [
      re.compile(r'\brm\s+-rf?\b'),
      re.compile(r'\bmkfs\b'),
      re.compile(r'\bdd\b.*\bof='),
      re.compile(r'\bparted\b'),
      re.compile(r'\bfdisk\b'),
      re.compile(r'\bwipefs\b'),
      re.compile(r'\bshred\b'),
      re.compile(r'\btruncate\b'),
      re.compile(r'>\s*/dev/'),
  ]

  _SHELL_FORCE_TIER2: list[re.Pattern] = [
      re.compile(r'\bsystemctl\b.*(restart|stop|start|disable|enable)'),
      re.compile(r'\bdocker\b.*(rm|rmi|prune|kill)'),
      re.compile(r'\breboot\b'),
      re.compile(r'\bpoweroff\b'),
      re.compile(r'\bshutdown\b'),
      re.compile(r'\biptables\b'),
      re.compile(r'\bufw\b.*(delete|disable|reset)'),
      re.compile(r'\bpasswd\b'),
      re.compile(r'\busermod\b'),
      re.compile(r'\bchmod\b\s+[0-7]*7'),
      re.compile(r'\bchown\b'),
      re.compile(r'\bgit\s+push\b'),
      re.compile(r'\bgit\s+reset\b'),
      re.compile(r'\bgit\s+config\b'),
      re.compile(r'\bcrontab\b'),
      re.compile(r'\bsed\b.*-i'),
      re.compile(r'\bawk\b.*>'),
      re.compile(r'\bwget\b.*-O\b'),
      re.compile(r'\bcurl\b.*(-o\b|-O\b|--output)'),
  ]
  ```

  3. In `SafetyPolicy.__init__`, after the existing field assignments, add:

  ```python
  guards = config.safety.shell_command_guards
  self._shell_force_tier3_patterns: list[re.Pattern] = list(_SHELL_FORCE_TIER3) + [
      re.compile(p) for p in guards.force_tier3
  ]
  self._shell_force_tier2_patterns: list[re.Pattern] = list(_SHELL_FORCE_TIER2) + [
      re.compile(p) for p in guards.force_tier2
  ]
  self._last_guard_match: tuple[str, str] | None = None
  ```

  4. Add the `_check_shell_command` method to `SafetyPolicy`:

  ```python
  def _check_shell_command(self, command: str, agent_proposed_tier: int) -> int:
      """Apply pattern guards to a shell command and return the effective tier.

      Sets self._last_guard_match to (list_name, pattern_string) when a guard
      fires, or None when no guard matches.
      """
      for pattern in self._shell_force_tier3_patterns:
          if pattern.search(command):
              self._last_guard_match = ("force_tier3", pattern.pattern)
              return 3
      for pattern in self._shell_force_tier2_patterns:
          if pattern.search(command):
              self._last_guard_match = ("force_tier2", pattern.pattern)
              return max(2, agent_proposed_tier)
      self._last_guard_match = None
      return agent_proposed_tier
  ```

- [ ] **Run tests (expect pass):**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_safety.py -v
```

- [ ] **Commit:**

```bash
cd /home/chris/src/homelab && git add agent/agent/safety.py agent/tests/test_safety.py && git commit -m "$(cat <<'EOF'
feat: add _check_shell_command and pattern lists to SafetyPolicy

Adds _SHELL_FORCE_TIER3 and _SHELL_FORCE_TIER2 module-level compiled
pattern lists. SafetyPolicy.__init__ merges these with config-provided
patterns into instance-level lists. _check_shell_command applies guards
and records match metadata in _last_guard_match.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update `resolve_tier` to call guard and populate `ResolvedTier`

**Files modified:**
- `/home/chris/src/homelab/agent/agent/safety.py`

**Test file:** `/home/chris/src/homelab/agent/tests/test_safety.py` (extend)

### Steps

- [ ] **Write failing tests** — append to `agent/tests/test_safety.py`:

```python
# --- resolve_tier integration: ResolvedTier fields ---

def test_resolve_tier_rm_rf_forces_tier3() -> None:
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="rm -rf /tmp/test",
    )
    assert resolved.tier == 3


def test_resolve_tier_git_push_forces_tier2() -> None:
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="git push origin main",
    )
    assert resolved.tier == 2


def test_resolve_tier_read_only_passthrough() -> None:
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="df -h",
    )
    assert resolved.tier == 1


def test_resolve_tier_git_push_tier2_stays_tier2() -> None:
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=2,
        command="git push origin main",
    )
    assert resolved.tier == 2


def test_resolve_tier_config_pattern_force_tier3() -> None:
    policy = make_policy(extra_tier3=[r"my-nuke\.sh"])
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="my-nuke.sh",
    )
    assert resolved.tier == 3


def test_resolve_tier_config_pattern_force_tier2() -> None:
    policy = make_policy(extra_tier2=[r"my-deploy\.sh"])
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="my-deploy.sh",
    )
    assert resolved.tier == 2


def test_resolve_tier_override_reason_logged_on_guard_fire() -> None:
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="git push origin main",
    )
    assert resolved.override_reason == "shell_pattern_guard"
    assert resolved.guard_matched_list == "force_tier2"
    assert resolved.guard_matched_pattern is not None


def test_resolve_tier_no_override_reason_when_no_guard() -> None:
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="df -h",
    )
    assert resolved.override_reason is None
    assert resolved.guard_matched_list is None
    assert resolved.guard_matched_pattern is None


def test_resolve_tier_original_tier_set_when_guard_fires() -> None:
    """original_tier must be agent_proposed_tier when a pattern guard overrides it."""
    policy = make_policy()
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="rm -rf /tmp/test",
    )
    assert resolved.original_tier == 1
    assert resolved.safe_mode_active is False


def test_resolve_tier_safe_mode_sets_guard_fields() -> None:
    """Under safe mode, guard still runs and guard fields are populated on ResolvedTier."""
    policy = make_policy(global_safe_mode=True)
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="git push origin main",
    )
    assert resolved.tier == 3
    assert resolved.safe_mode_active is True
    assert resolved.guard_matched_list == "force_tier2"
    assert resolved.guard_matched_pattern is not None


def test_resolve_tier_safe_mode_no_guard_match_guard_fields_none() -> None:
    """Under safe mode with no guard match, guard fields are still None."""
    policy = make_policy(global_safe_mode=True)
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="df -h",
    )
    assert resolved.tier == 3
    assert resolved.safe_mode_active is True
    assert resolved.guard_matched_list is None
    assert resolved.guard_matched_pattern is None


def test_resolve_tier_stale_guard_match_not_leaked() -> None:
    """_last_guard_match must be reset at start of each resolve_tier call."""
    policy = make_policy()
    # First call fires a guard
    policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="git push origin main",
    )
    # Second call must not inherit the previous guard match
    resolved = policy.resolve_tier(
        tool_name="run_shell",
        agent_proposed_tier=1,
        command="df -h",
    )
    assert resolved.guard_matched_list is None
    assert resolved.guard_matched_pattern is None
    assert resolved.override_reason is None


def test_resolve_tier_non_shell_tool_unaffected() -> None:
    """Guard logic must not run for non-run_shell tools."""
    policy = make_policy()
    resolved = policy.resolve_tier(tool_name="read_file")
    assert resolved.tier == 1
    assert resolved.guard_matched_list is None
```

- [ ] **Run tests (expect failure):**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_safety.py -v
```

Expected: `TypeError` — `resolve_tier` does not accept `command` yet; `ResolvedTier` does not have the new fields yet.

- [ ] **Implement** — modify `agent/agent/safety.py`:

  1. Add three new optional fields to `ResolvedTier`:

  ```python
  @dataclass
  class ResolvedTier:
      tier: int
      safe_mode_active: bool
      original_tier: int | None
      agent_reasoning: str | None
      override_reason: str | None = None
      guard_matched_list: str | None = None
      guard_matched_pattern: str | None = None
  ```

  2. Update `_base_tier` signature to accept `command`:

  ```python
  def _base_tier(self, tool_name: str, agent_proposed_tier: int | None, command: str | None = None) -> int:
      """Return the raw tier before safe-mode overrides."""
      configured = self.tool_tiers.get(tool_name)

      if configured is not None:
          if configured in (1, 2, 3):
              return int(configured)
          if configured == "agent":
              # Agent discretion — apply pattern guards for run_shell
              if tool_name == "run_shell" and agent_proposed_tier is not None and command is not None:
                  return self._check_shell_command(command, agent_proposed_tier)
              return agent_proposed_tier if agent_proposed_tier is not None else 2

      return _DEFAULT_TIERS.get(tool_name, 2)
  ```

  3. Replace `resolve_tier` entirely:

  ```python
  def resolve_tier(
      self,
      tool_name: str,
      target_resource: str | None = None,
      agent_proposed_tier: int | None = None,
      agent_reasoning: str | None = None,
      command: str | None = None,
  ) -> ResolvedTier:
      """Resolve the effective execution tier for a tool call.

      Resolution order (highest priority first):
      1. global_safe_mode → tier 3, log original
      2. target_resource in safe_mode_resources → tier 3, log original
      3. explicit numeric value in tool_tiers config → use it
      4. tool_tiers value is "agent" → use agent_proposed_tier (with shell guards)
      5. hardcoded default → use _DEFAULT_TIERS
      """
      self._last_guard_match = None  # reset before any early-return

      original = self._base_tier(tool_name, agent_proposed_tier, command)
      guard_list, guard_pattern = (self._last_guard_match or (None, None))
      override_reason = "shell_pattern_guard" if guard_list is not None else None

      # Priority 1: global safe mode
      if self.global_safe_mode:
          return ResolvedTier(
              tier=3,
              safe_mode_active=True,
              original_tier=original,
              agent_reasoning=agent_reasoning,
              override_reason=override_reason,
              guard_matched_list=guard_list,
              guard_matched_pattern=guard_pattern,
          )

      # Priority 2: per-resource safe mode
      if self._resource_in_safe_mode(target_resource):
          return ResolvedTier(
              tier=3,
              safe_mode_active=True,
              original_tier=original,
              agent_reasoning=agent_reasoning,
              override_reason=override_reason,
              guard_matched_list=guard_list,
              guard_matched_pattern=guard_pattern,
          )

      # No safe-mode override — use original tier
      # When a guard fired, original_tier = agent_proposed_tier (pre-guard value)
      guard_fired = guard_list is not None
      return ResolvedTier(
          tier=original,
          safe_mode_active=False,
          original_tier=agent_proposed_tier if guard_fired else None,
          agent_reasoning=agent_reasoning,
          override_reason=override_reason,
          guard_matched_list=guard_list,
          guard_matched_pattern=guard_pattern,
      )
  ```

- [ ] **Run tests (expect pass):**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_safety.py -v
```

- [ ] **Commit:**

```bash
cd /home/chris/src/homelab && git add agent/agent/safety.py agent/tests/test_safety.py && git commit -m "$(cat <<'EOF'
feat: wire _check_shell_command into resolve_tier and extend ResolvedTier

resolve_tier now accepts command= and passes it through _base_tier to
_check_shell_command for run_shell calls. ResolvedTier gains three new
fields: override_reason, guard_matched_list, guard_matched_pattern.
Safe-mode early-return paths also populate guard fields for audit logging.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Update `log_tier_reasoning` call sites

**Files modified:**
- `/home/chris/src/homelab/agent/agent/agent.py`

**Test file:** `/home/chris/src/homelab/agent/tests/test_agent_logging.py` (create)

### Steps

- [ ] **Write failing tests** in `agent/tests/test_agent_logging.py`:

```python
"""Tests for ActionLogger.log_tier_reasoning with guard metadata fields."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, call
import pytest
from agent.agent import ActionLogger


@pytest.fixture
def logger() -> ActionLogger:
    mock_writer = AsyncMock()
    return ActionLogger(writer=mock_writer)


async def test_log_tier_reasoning_includes_guard_fields_when_set(logger: ActionLogger) -> None:
    await logger.log_tier_reasoning(
        tool="run_shell",
        agent_proposed_tier=1,
        reasoning="looks safe",
        safe_mode_active=False,
        effective_tier=2,
        override_reason="shell_pattern_guard",
        guard_matched_list="force_tier2",
        guard_matched_pattern=r"\bgit\s+push\b",
    )
    logger._writer.assert_awaited_once()
    logged = logger._writer.call_args[0][0]
    assert logged["override_reason"] == "shell_pattern_guard"
    assert logged["guard_matched_list"] == "force_tier2"
    assert logged["guard_matched_pattern"] == r"\bgit\s+push\b"


async def test_log_tier_reasoning_omits_guard_fields_when_none(logger: ActionLogger) -> None:
    await logger.log_tier_reasoning(
        tool="run_shell",
        agent_proposed_tier=1,
        reasoning="looks safe",
        safe_mode_active=False,
        effective_tier=1,
        override_reason=None,
        guard_matched_list=None,
        guard_matched_pattern=None,
    )
    logger._writer.assert_awaited_once()
    logged = logger._writer.call_args[0][0]
    assert "override_reason" not in logged
    assert "guard_matched_list" not in logged
    assert "guard_matched_pattern" not in logged


async def test_log_tier_reasoning_backward_compatible_no_guard_args(logger: ActionLogger) -> None:
    """Guard fields are optional and default to None — old call sites still work."""
    await logger.log_tier_reasoning(
        tool="run_shell",
        agent_proposed_tier=1,
        reasoning="looks safe",
        safe_mode_active=False,
        effective_tier=1,
    )
    logger._writer.assert_awaited_once()
    logged = logger._writer.call_args[0][0]
    assert "override_reason" not in logged
```

Note: these tests depend on knowing the internal structure of `ActionLogger`. Before writing them, verify the exact signature and `_writer` attribute name in `agent/agent/agent.py`. Adjust the fixture and attribute access accordingly if the actual implementation differs.

- [ ] **Run tests (expect failure):**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_agent_logging.py -v
```

Expected: `TypeError` — `log_tier_reasoning` does not accept the new keyword arguments yet.

- [ ] **Implement** — modify `agent/agent/agent.py`:

  1. Update `ActionLogger.log_tier_reasoning` method signature and body (currently at line 99):

  ```python
  async def log_tier_reasoning(
      self,
      tool: str,
      agent_proposed_tier: int,
      reasoning: str,
      safe_mode_active: bool,
      effective_tier: int,
      override_reason: str | None = None,
      guard_matched_list: str | None = None,
      guard_matched_pattern: str | None = None,
  ) -> None:
      entry: dict = {
          "event": "tier_reasoning",
          "tool": tool,
          "agent_proposed_tier": agent_proposed_tier,
          "reasoning": reasoning,
          "safe_mode_active": safe_mode_active,
          "effective_tier": effective_tier,
      }
      if override_reason is not None:
          entry["override_reason"] = override_reason
          entry["guard_matched_list"] = guard_matched_list
          entry["guard_matched_pattern"] = guard_matched_pattern
      await self.log(entry)
  ```

  2. Update the `resolve_tier` call site in `_handle_tool_calls` (currently around line 543) to pass `command`:

  ```python
  resolved = self._safety.resolve_tier(
      block.name,
      target,
      agent_tier,
      agent_reason,
      command=inp.get("command") if block.name == "run_shell" else None,
  )
  ```

  3. Update the `log_tier_reasoning` call site (currently around line 553) to forward the three new fields:

  ```python
  await self._logger.log_tier_reasoning(
      tool=block.name,
      agent_proposed_tier=agent_tier,
      reasoning=agent_reason or "",
      safe_mode_active=resolved.safe_mode_active,
      effective_tier=resolved.tier,
      override_reason=resolved.override_reason,
      guard_matched_list=resolved.guard_matched_list,
      guard_matched_pattern=resolved.guard_matched_pattern,
  )
  ```

- [ ] **Run tests (expect pass):**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_agent_logging.py -v
```

- [ ] **Run full test suite to confirm no regressions:**

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
```

- [ ] **Commit:**

```bash
cd /home/chris/src/homelab && git add agent/agent/agent.py agent/tests/test_agent_logging.py && git commit -m "$(cat <<'EOF'
feat: forward guard metadata through log_tier_reasoning call sites

Updates ActionLogger.log_tier_reasoning to accept override_reason,
guard_matched_list, and guard_matched_pattern as optional fields, writing
them to the log entry only when not None. Updates the resolve_tier and
log_tier_reasoning call sites in _handle_tool_calls to pass command= and
forward the new ResolvedTier fields.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Update `prompts.py` TIER_RULES

**Files modified:**
- `/home/chris/src/homelab/agent/agent/prompts.py`

**No tests required** — this is a static string update with no logic.

### Steps

- [ ] **Locate** the `### For \`run_shell\`` subsection inside the `TIER_RULES` constant in `agent/agent/prompts.py`.

- [ ] **Replace** the current content of that subsection with:

  ```
  ### For `run_shell` (agent-discretion tool)
  You must include `agent_proposed_tier` (1, 2, or 3) and `agent_reasoning` in every `run_shell` call. Use these guidelines:
  - **Tier 1:** Purely diagnostic/read-only (e.g., `df -h`, `docker ps`, `journalctl -n 50`)
  - **Tier 2:** Involves SSH to multiple nodes, service restarts, or config changes
  - **Tier 3:** Irreversible actions (data deletion, partition changes)

  **Pattern guards:** The system enforces a hardcoded denylist of dangerous command patterns. If your proposed command matches a tier-3 pattern (e.g. `rm -rf`, `mkfs`, `dd of=`, `parted`), the tier is automatically escalated to 3 regardless of your proposal. If it matches a tier-2 pattern (e.g. `systemctl restart`, `git push`, `git reset`, `sed -i`), the tier is raised to at least 2. You will see the enforced tier in the action log under `override_reason: shell_pattern_guard`. Propose the tier you genuinely believe is correct — the guard is a safety net, not a substitute for your own reasoning.

  **SSH to nodes:** Always use the `node` parameter in `run_shell` rather than constructing raw `ssh` commands. The tool automatically uses `/root/.ssh/ansible_ssh_key` as the identity. This applies to all nodes including the edge node at `192.168.3.91`.
  ```

- [ ] **Commit:**

```bash
cd /home/chris/src/homelab && git add agent/agent/prompts.py && git commit -m "$(cat <<'EOF'
docs: update TIER_RULES to document run_shell pattern guards

Informs the agent that its tier proposals are subject to automatic
escalation by pattern guards, and lists example patterns for each tier.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Summary of files changed

| File | Change |
|------|--------|
| `agent/agent/config_schema.py` | Add `ShellCommandGuardsConfig` model before `SafetyConfig`; add `shell_command_guards` field to `SafetyConfig` |
| `agent/config.yaml` | Add `shell_command_guards` subsection inside `safety:` block |
| `agent/agent/safety.py` | Add `import re`; add `_SHELL_FORCE_TIER3` and `_SHELL_FORCE_TIER2` module-level lists; add `_last_guard_match`, `_shell_force_tier3_patterns`, `_shell_force_tier2_patterns` to `SafetyPolicy.__init__`; add `_check_shell_command`; update `_base_tier` and `resolve_tier` signatures; add `override_reason`, `guard_matched_list`, `guard_matched_pattern` to `ResolvedTier` |
| `agent/agent/agent.py` | Update `ActionLogger.log_tier_reasoning` signature and body; update `resolve_tier` call site to pass `command=`; update `log_tier_reasoning` call site to forward three new `ResolvedTier` fields |
| `agent/agent/prompts.py` | Update `TIER_RULES` `run_shell` subsection to document pattern guards |
| `agent/tests/test_config_schema.py` | New — config schema unit tests |
| `agent/tests/test_safety.py` | New — `SafetyPolicy` unit tests (guard logic, `resolve_tier` integration) |
| `agent/tests/test_agent_logging.py` | New — `ActionLogger.log_tier_reasoning` unit tests |

## Test commands (full suite)

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/ -v
```

Individual suites:

```bash
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_config_schema.py -v
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_safety.py -v
cd /home/chris/src/homelab/agent && hatch run pytest tests/test_agent_logging.py -v
```
