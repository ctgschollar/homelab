# Fix 1 — `run_shell` Command Pattern Guards

**Date:** 2026-03-24
**Scope:** `agent/` directory only
**PR:** standalone

---

## Problem

`run_shell` is tagged `"agent"` in `tool_tiers`, so the model proposes its own safety tier via `agent_proposed_tier`. Nothing in `SafetyPolicy` validates that proposal against the actual command string. The incident log shows the agent classifying `git push` and credential config reads as tier 1 ("read-only-equivalent"). Any sufficiently plausible-sounding `agent_reasoning` string passes through unchanged.

The gap is in `_base_tier` in `safety.py`: when `configured == "agent"`, the method returns `agent_proposed_tier` directly with no further checks. The command string is not consulted at all.

---

## Design

### 1. Pattern lists (`safety.py`)

Add two module-level lists of compiled `re.Pattern` objects immediately after the existing `_DEFAULT_TIERS` dict. Import `re` at the top of the file.

**`_SHELL_FORCE_TIER3`** — matches commands that are irreversible or destructive at the storage/filesystem level. Any match forces tier 3 regardless of the agent's proposal.

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
```

**`_SHELL_FORCE_TIER2`** — matches commands that modify system state, running services, users, network rules, or tracked repository state. Any match raises the effective tier to at least 2 (never lowers it).

```python
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

These are the **hardcoded defaults**. They are always present and cannot be removed or replaced by config. The config-provided patterns (see section 4) are compiled and appended to these lists at `SafetyPolicy.__init__` time.

---

### 2. `_check_shell_command` method

Add the following method to `SafetyPolicy`. It takes the raw command string and the agent's proposed tier, and returns the effective tier after applying pattern guards.

**Signature:**

```python
def _check_shell_command(self, command: str, agent_proposed_tier: int) -> int:
```

**Logic (in order):**

1. Iterate over `self._shell_force_tier3_patterns` (the merged list — see section 4). If any pattern matches `command` (using `pattern.search(command)`), set `self._last_guard_match = ("force_tier3", pattern.pattern)` and return `3`.
2. Iterate over `self._shell_force_tier2_patterns`. If any pattern matches, set `self._last_guard_match = ("force_tier2", pattern.pattern)` and return `max(2, agent_proposed_tier)`.
3. If no pattern matched, set `self._last_guard_match = None` and return `agent_proposed_tier` unchanged.

The method also needs to record which list matched and which pattern, for logging (see section 5). The approach: capture the matching pattern in a local variable during the loop, then pass it back via a dedicated return path or a side-channel instance variable. The simplest design is to store the match result as a pair of instance attributes set during the check:

- `self._last_guard_match: tuple[str, str] | None` — set to `(tier_list_name, pattern_string)` when a guard fires, or `None` when no guard fires.

`_check_shell_command` sets `self._last_guard_match` before returning (including clearing it to `None` on the fall-through path). The caller reads it immediately after. This avoids changing the method signature and keeps the logging concern isolated to the call site in `resolve_tier`.

---

### 3. `_base_tier` integration

In `_base_tier`, the current branch for `configured == "agent"` is:

```python
if configured == "agent":
    return agent_proposed_tier if agent_proposed_tier is not None else 2
```

Replace this branch with:

```python
if configured == "agent":
    if tool_name == "run_shell" and agent_proposed_tier is not None and command is not None:
        return self._check_shell_command(command, agent_proposed_tier)
    return agent_proposed_tier if agent_proposed_tier is not None else 2
```

This requires `_base_tier` to accept a `command` parameter. The full updated method signature and body is:

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

The `command` parameter defaults to `None`. Inside the guarded branch, if `command is None`, the guard is not applied and the method falls back to `agent_proposed_tier` as before. This preserves backward compatibility for callers that do not supply a command.

`resolve_tier` must also be updated to accept and forward `command`, and must reset `self._last_guard_match = None` at the very start of each call — before any early-return paths — so that stale values from a prior call can never leak into the current call's result:

```python
def resolve_tier(
    self,
    tool_name: str,
    target_resource: str | None = None,
    agent_proposed_tier: int | None = None,
    agent_reasoning: str | None = None,
    command: str | None = None,
) -> ResolvedTier:
    self._last_guard_match = None  # reset before any early-return

    original = self._base_tier(tool_name, agent_proposed_tier, command)
    # ... (safe-mode early-return paths — see note below)
```

After the `original = self._base_tier(...)` call, `resolve_tier` reads `self._last_guard_match` to populate the new `ResolvedTier` fields:

```python
    guard_list, guard_pattern = (self._last_guard_match or (None, None))
    override_reason = "shell_pattern_guard" if guard_list is not None else None
```

These values are then passed to the returned `ResolvedTier` (see section 5).

`self._last_guard_match` must be initialised to `None` in `SafetyPolicy.__init__` before any method accesses it.

**Safe mode + guard interaction:** When `global_safe_mode` is active, safe mode takes precedence and the action becomes tier 3 regardless of what any guard pattern returns. However, `_check_shell_command` still runs and `self._last_guard_match` is still set (or cleared) normally before safe mode's tier-3 override is applied. This means guard match information is preserved in `ResolvedTier` for logging and audit purposes even when safe mode is the operative reason for tier 3. The tier escalation from the guard is irrelevant in this case — `safe_mode_active` will be `True` on the returned `ResolvedTier`, which is the authoritative signal that safe mode, not a guard, drove the final tier.

**Safe-mode early-return paths — NOT unchanged for shell commands:** `resolve_tier` contains early-return paths that construct and return a `ResolvedTier` directly under safe mode (e.g. when `global_safe_mode` is `True`). These paths are **not** unchanged when a shell command is being evaluated. For any call where `tool_name == "run_shell"` and `command` is not `None`, the safe-mode early-return path must first call `self._check_shell_command(command, agent_proposed_tier)` (to set `self._last_guard_match`), then read `self._last_guard_match` and populate `guard_matched_list`, `guard_matched_pattern`, and `override_reason` on the returned `ResolvedTier` before returning. Without this, the guard fields would always be `None` under safe mode, contrary to the audit-logging guarantee stated above.

---

### 4. Config schema (`SafetyConfig` + `config.yaml`)

#### `config_schema.py`

Add a new sub-model `ShellCommandGuardsConfig` and add it as an optional field on `SafetyConfig`.

`ShellCommandGuardsConfig` must be defined **before** `SafetyConfig` in the file (it currently sits just after `SafeModeResourcesConfig`). Defining it after `SafetyConfig` causes a forward-reference error because Pydantic v2 resolves field types at class-creation time for concrete `BaseModel` subclasses.

```python
class ShellCommandGuardsConfig(BaseModel):
    force_tier3: list[str] = []
    force_tier2: list[str] = []
```

Update `SafetyConfig`:

```python
class SafetyConfig(BaseModel):
    global_safe_mode: bool
    safe_mode_resources: SafeModeResourcesConfig
    tool_tiers: dict[str, TierValue]
    log_agent_tier_reasoning: bool
    shell_command_guards: ShellCommandGuardsConfig = Field(default_factory=ShellCommandGuardsConfig)
```

Use `Field(default_factory=ShellCommandGuardsConfig)` rather than a bare `= ShellCommandGuardsConfig()` default for consistency with how other optional sub-models are defaulted elsewhere in the file (e.g. `ApprovalListenerConfig`, `HistoryConfig`, `RollbackConfig` all use `Field(..., default=...)`; using `default_factory` avoids sharing a single mutable instance across configs). The field defaults to an empty `ShellCommandGuardsConfig` (both lists empty) so existing configs without the section continue to work.

#### `SafetyPolicy.__init__`

After the existing field assignments, compile the config-provided patterns and merge them with the hardcoded defaults:

```python
import re  # at module top

# In __init__:
guards = config.safety.shell_command_guards
self._shell_force_tier3_patterns: list[re.Pattern] = list(_SHELL_FORCE_TIER3) + [
    re.compile(p) for p in guards.force_tier3
]
self._shell_force_tier2_patterns: list[re.Pattern] = list(_SHELL_FORCE_TIER2) + [
    re.compile(p) for p in guards.force_tier2
]
self._last_guard_match: tuple[str, str] | None = None
```

The hardcoded defaults are copied (not mutated) so that the module-level lists remain unchanged across instances.

#### `config.yaml`

Add a `shell_command_guards` subsection inside the `safety` block, after `log_agent_tier_reasoning`. It ships empty in the default config — operators add their own patterns here without touching the hardcoded defaults.

```yaml
  # Optional additional shell command guard patterns. Each entry is a Python
  # regex string. Patterns here are ADDED to the hardcoded defaults; they
  # never replace them.
  shell_command_guards:
    force_tier3: []  # e.g. ['my-dangerous-script\.sh']
    force_tier2: []  # e.g. ['my-deploy\.sh']
```

This section must appear inside the `safety:` block at the same indentation level as `log_agent_tier_reasoning`.

---

### 5. Logging

The existing `log_agent_tier_reasoning` pathway logs a `tier_reasoning` entry to the action log when `run_shell` is classified. The guard override must annotate that entry.

When `_check_shell_command` fires (i.e. the returned tier differs from `agent_proposed_tier` because a pattern matched), `self._last_guard_match` is set to a tuple of:

- `tier_list_name`: the string `"force_tier3"` or `"force_tier2"`
- `pattern_string`: the `.pattern` attribute of the matching `re.Pattern`

The caller in `resolve_tier` reads `self._last_guard_match` immediately after calling `_base_tier`. The guard metadata is then surfaced through `ResolvedTier` so the logging layer can act on it.

Add two new optional fields to `ResolvedTier`:

```python
@dataclass
class ResolvedTier:
    tier: int
    safe_mode_active: bool
    original_tier: int | None
    agent_reasoning: str | None
    override_reason: str | None = None         # new
    guard_matched_list: str | None = None      # new: "force_tier2" or "force_tier3"
    guard_matched_pattern: str | None = None   # new: the regex string that matched
```

In `resolve_tier`, after calling `_base_tier` to obtain `original`, check `self._last_guard_match`:

- If it is not `None`, set `override_reason = "shell_pattern_guard"` and populate `guard_matched_list` and `guard_matched_pattern` on the returned `ResolvedTier`.
- If it is `None`, leave all three fields as `None`.

**`original_tier` when a pattern guard fires (safe mode NOT active):** When `_check_shell_command` overrides the agent's proposal (i.e. `_last_guard_match` is not `None`) and safe mode is not active, `original_tier` on the returned `ResolvedTier` must be set to `agent_proposed_tier` (the value the agent proposed, before the guard raised it). `safe_mode_active` stays `False`. This distinguishes a pattern-guard override from a safe-mode override: safe-mode overrides set `safe_mode_active=True`, while pattern-guard overrides set `safe_mode_active=False` and `override_reason="shell_pattern_guard"`. When no guard fires and safe mode is not active, `original_tier` remains `None` as today.

The logging code that consumes `ResolvedTier` must be updated in two places in `agent.py`:

1. The `log_tier_reasoning` **call site** in `_handle_tool_calls` must be updated to read `resolved.override_reason`, `resolved.guard_matched_list`, and `resolved.guard_matched_pattern` from the returned `ResolvedTier` and pass them as arguments to `ActionLogger.log_tier_reasoning`.
2. `ActionLogger.log_tier_reasoning`'s **method signature** must be updated to accept `override_reason: str | None`, `guard_matched_list: str | None`, and `guard_matched_pattern: str | None` as optional parameters, and include them in the log entry when they are not `None`.

When `override_reason` is `None`, all three fields must be omitted from the log entry entirely (not written as `null`).

---

### 6. `prompts.py` update

Update the `### For \`run_shell\`` subsection of `TIER_RULES` to inform the agent that its tier proposals are subject to automatic escalation by pattern guards. Replace the current content of that subsection with:

```
### For `run_shell` (agent-discretion tool)
You must include `agent_proposed_tier` (1, 2, or 3) and `agent_reasoning` in every `run_shell` call. Use these guidelines:
- **Tier 1:** Purely diagnostic/read-only (e.g., `df -h`, `docker ps`, `journalctl -n 50`)
- **Tier 2:** Involves SSH to multiple nodes, service restarts, or config changes
- **Tier 3:** Irreversible actions (data deletion, partition changes)

**Pattern guards:** The system enforces a hardcoded denylist of dangerous command patterns. If your proposed command matches a tier-3 pattern (e.g. `rm -rf`, `mkfs`, `dd of=`, `parted`), the tier is automatically escalated to 3 regardless of your proposal. If it matches a tier-2 pattern (e.g. `systemctl restart`, `git push`, `git reset`, `sed -i`), the tier is raised to at least 2. You will see the enforced tier in the action log under `override_reason: shell_pattern_guard`. Propose the tier you genuinely believe is correct — the guard is a safety net, not a substitute for your own reasoning.

**SSH to nodes:** Always use the `node` parameter in `run_shell` rather than constructing raw `ssh` commands. The tool automatically uses `/root/.ssh/ansible_ssh_key` as the identity. This applies to all nodes including the edge node at `192.168.3.91`.
```

---

## Files changed

| File | Change |
|------|--------|
| `agent/agent/safety.py` | Add `import re`; add `_SHELL_FORCE_TIER3` and `_SHELL_FORCE_TIER2` module-level lists; add `_last_guard_match` and `_shell_force_tier3_patterns` / `_shell_force_tier2_patterns` instance attributes to `SafetyPolicy.__init__`; add `_check_shell_command` method; update `_base_tier` signature (add `command: str | None = None`) and logic; update `resolve_tier` signature (add `command: str | None = None`) and logic; add `override_reason`, `guard_matched_list`, `guard_matched_pattern` fields to `ResolvedTier` |
| `agent/agent/config_schema.py` | Add `ShellCommandGuardsConfig` model (before `SafetyConfig`); add `shell_command_guards` field to `SafetyConfig` |
| `agent/agent/agent.py` | In `_handle_tool_calls`, for `run_shell` tool calls, pass `command=tool_input.get("command")` when calling `self._safety.resolve_tier(...)`. Update the `log_tier_reasoning` call site to forward three new `ResolvedTier` fields — `override_reason`, `guard_matched_list`, and `guard_matched_pattern` — to `ActionLogger.log_tier_reasoning`. Update `ActionLogger.log_tier_reasoning`'s method signature to accept those three new optional fields and include them in the log entry when they are not `None`. |
| `agent/config.yaml` | Add `shell_command_guards` subsection inside `safety:` block |
| `agent/agent/prompts.py` | Update `TIER_RULES` `run_shell` subsection to document pattern guards |

No other files are in scope for this fix. The changes to `agent.py` are: adding one keyword argument to the `resolve_tier` call site, and updating the `log_tier_reasoning` call site and `ActionLogger.log_tier_reasoning` signature to forward the three new guard metadata fields.

---

## Tests

File: `agent/tests/test_fix1_shell_guards.py`

All tests are unit tests. All external dependencies (config loading, file I/O, logging sinks) are mocked or bypassed by constructing `SafetyPolicy` directly with a minimal fake `AgentConfig`. Do not write integration tests.

**Test helper:** Define a `make_policy(extra_tier3=None, extra_tier2=None)` fixture or factory function that builds a `SafetyPolicy` with a minimal `AgentConfig`. Do **not** construct `AgentConfig(...)` directly — that goes through `BaseSettings` validation and requires a YAML source, env vars, and all mandatory fields. Instead, bypass validation using Pydantic's `model_construct`:

```python
from agent.config_schema import (
    AgentConfig, SafetyConfig, SafeModeResourcesConfig, ShellCommandGuardsConfig
)
from agent.safety import SafetyPolicy

def make_policy(
    extra_tier3: list[str] | None = None,
    extra_tier2: list[str] | None = None,
) -> SafetyPolicy:
    guards = ShellCommandGuardsConfig(
        force_tier3=extra_tier3 or [],
        force_tier2=extra_tier2 or [],
    )
    safety = SafetyConfig(
        global_safe_mode=False,
        safe_mode_resources=SafeModeResourcesConfig(),
        tool_tiers={"run_shell": "agent"},
        log_agent_tier_reasoning=False,
        shell_command_guards=guards,
    )
    config = AgentConfig.model_construct(safety=safety)
    return SafetyPolicy(config)
```

`AgentConfig.model_construct(...)` creates the instance without running validators or requiring the fields `SafetyPolicy` does not read (e.g. `anthropic`, `slack`, `docker`). Only pass the fields that `SafetyPolicy.__init__` actually accesses (`safety`). `SafetyConfig` itself can be constructed normally (it is a plain `BaseModel`, not a `BaseSettings`, so no YAML source is needed).

### Test cases

| Test name | Scenario | Expected result |
|-----------|----------|-----------------|
| `test_rm_rf_forces_tier3` | `command="rm -rf /tmp/test"`, `agent_proposed_tier=1` | `resolved.tier == 3` |
| `test_git_push_forces_tier2` | `command="git push origin main"`, `agent_proposed_tier=1` | `resolved.tier == 2` |
| `test_read_only_passthrough` | `command="df -h"`, `agent_proposed_tier=1` | `resolved.tier == 1` |
| `test_git_push_tier2_stays_tier2` | `command="git push origin main"`, `agent_proposed_tier=2` | `resolved.tier == 2` (max() does not lower) |
| `test_config_pattern_force_tier3` | `extra_tier3=["my-nuke\\.sh"]`, `command="my-nuke.sh"`, `agent_proposed_tier=1` | `resolved.tier == 3` |
| `test_config_pattern_force_tier2` | `extra_tier2=["my-deploy\\.sh"]`, `command="my-deploy.sh"`, `agent_proposed_tier=1` | `resolved.tier == 2` |
| `test_override_reason_logged_on_guard_fire` | `command="git push"`, `agent_proposed_tier=1` | `resolved.override_reason == "shell_pattern_guard"` and `resolved.guard_matched_list == "force_tier2"` and `resolved.guard_matched_pattern` is not `None` |
| `test_no_override_reason_when_no_guard` | `command="df -h"`, `agent_proposed_tier=1` | `resolved.override_reason is None` and `resolved.guard_matched_list is None` and `resolved.guard_matched_pattern is None` |

Each test calls `policy.resolve_tier(tool_name="run_shell", agent_proposed_tier=<n>, command=<cmd>)` and asserts on the returned `ResolvedTier`.

---

## Out of scope

- Changes to `agent.py` or `tools.py` beyond: passing `command=` to `resolve_tier`, and updating the `log_tier_reasoning` call site and `ActionLogger.log_tier_reasoning` signature to forward `override_reason`, `guard_matched_list`, and `guard_matched_pattern`.
- Changes to the action log writer or Slack notifier to surface `override_reason` — that is handled wherever tier-reasoning log entries are currently written (Fix 1 only adds the fields to `ResolvedTier`; consuming them is the responsibility of the existing logging code).
- Any change to how tier-1 and tier-2 execution gates work (that is Fix 3).
- Validation of `shell_command_guards` regex strings for correctness — if a pattern fails to compile, the `re.compile()` call will raise at startup, which is sufficient.
- Adding new commands to the hardcoded denylist beyond the set defined in section 1 — that is an ongoing operational concern, not a code change.
