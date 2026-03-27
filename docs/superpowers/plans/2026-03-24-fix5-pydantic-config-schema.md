# Fix 5 — Pydantic Config Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all untyped `config["key"]` dict access with a validated Pydantic v2 + pydantic-settings model, fix the duplicate `commit_config_updates` config key, and add a `config_cli.py validate` command.

**Architecture:** A new `config_schema.py` defines the full Pydantic model tree. A `YamlConfigSettingsSource` loads the YAML file and injects env var secrets into the dict before validation. `load_agent_config(yaml_path)` is the single factory used everywhere. All files that accept `config: dict` are migrated to `config: AgentConfig` and their dict accesses replaced with attribute access.

**Tech Stack:** Python 3.12, Pydantic v2, pydantic-settings v2, PyYAML (already a dep), pytest (test env)

**Spec:** `docs/superpowers/specs/2026-03-24-fix5-pydantic-config-schema-design.md`

---

## File Map

| File | Role |
|------|------|
| `agent/agent/config_schema.py` | **NEW** — all Pydantic sub-models, `AgentConfig(BaseSettings)`, `YamlConfigSettingsSource`, `load_agent_config` factory |
| `agent/pyproject.toml` | Add `pydantic>=2.0`, `pydantic-settings>=2.0`; remove `ruamel.yaml` |
| `agent/config.yaml` | Remove duplicate key and secret placeholders; add `history`/`rollback` sections |
| `agent/agent/slack.py` | Accept `Optional[str]` for secrets; guard `verify_signature` |
| `agent/agent/safety.py` | Accept `AgentConfig`; attribute access throughout |
| `agent/agent/monitor.py` | Accept `AgentConfig`; attribute access throughout |
| `agent/agent/tools.py` | Accept `AgentConfig`; attribute access throughout |
| `agent/agent/agent.py` | Accept `AgentConfig`; attribute access throughout |
| `agent/cli.py` | `load_config` returns `AgentConfig`; migrate all dict access |
| `agent/config_cli.py` | Drop `ruamel.yaml`; raw-dict write-back with Pydantic validation; add `validate` command |

---

## Task 1: Add dependencies to pyproject.toml

**Files:**
- Modify: `agent/pyproject.toml`

- [ ] **Open `agent/pyproject.toml` and update `[project] dependencies`:**
  - Add `"pydantic>=2.0"` and `"pydantic-settings>=2.0"` to the list
  - Remove `"ruamel.yaml>=0.18.0"` from the list

- [ ] **Commit:**
  ```bash
  cd agent
  git add pyproject.toml
  git commit -m "chore: add pydantic/pydantic-settings deps, remove ruamel.yaml"
  ```

---

## Task 2: Create `config_schema.py`

**Files:**
- Create: `agent/agent/config_schema.py`

This is the core of the fix. Implement the full model tree exactly as specified.

- [ ] **Create `agent/agent/config_schema.py` with this content:**

```python
from __future__ import annotations

import os
import warnings
from typing import Literal, Optional

import yaml
from pydantic import AliasChoices, BaseModel, Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

TierValue = Literal[1, 2, 3, "agent"]


# ---------------------------------------------------------------------------
# Sub-models (plain BaseModel — no env var resolution)
# ---------------------------------------------------------------------------

class AnthropicConfig(BaseModel):
    """Anthropic API connection and pricing."""
    api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("api_key", "ANTHROPIC_API_KEY"),
        description="Anthropic API key — injected from ANTHROPIC_API_KEY env var",
    )
    model: str = Field(description="Model ID, e.g. claude-sonnet-4-20250514")
    input_cost_per_mtok: float = Field(description="USD cost per million input tokens")
    output_cost_per_mtok: float = Field(description="USD cost per million output tokens")


class SlackConfig(BaseModel):
    """Slack bot credentials and channel config."""
    bot_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("bot_token", "SLACK_BOT_TOKEN"),
        description="Slack bot token — injected from SLACK_BOT_TOKEN env var",
    )
    signing_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("signing_secret", "SLACK_SIGNING_SECRET"),
        description="Slack signing secret — injected from SLACK_SIGNING_SECRET env var",
    )
    channel: str = Field(description="Slack channel to post notifications, e.g. #homelab")
    veto_window_seconds: int = Field(
        gt=0,
        default=300,
        description="Seconds to wait for Approve/Deny before a tier-2 plan times out",
    )


class DockerConfig(BaseModel):
    """Docker daemon connection."""
    socket: str = Field(description="Docker socket URL, e.g. unix:///var/run/docker.sock")


class SwarmConfig(BaseModel):
    """Docker Swarm node access."""
    nodes: list[str] = Field(description="Hostnames of all swarm nodes")
    ssh_key: str = Field(description="Path to SSH private key for node access")
    ssh_user: str = Field(description="SSH user for node access")


class EdgeConfig(BaseModel):
    """Edge/tunnel node access. Schema-only — no active code reads this section."""
    cloudflare_tunnel_node: str = Field(default="", description="IP or hostname of the Cloudflare tunnel node")
    ssh_key: str = Field(default="", description="Path to SSH private key")
    ssh_user: str = Field(default="", description="SSH user")


class AnsibleConfig(BaseModel):
    """Ansible repo and git author config."""
    repo_path: str = Field(description="Absolute path to the homelab ansible repo on disk")
    inventory: str = Field(description="Absolute path to the Ansible inventory file")
    git_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("git_token", "AGENT_GITHUB_TOKEN"),
        description="GitHub PAT for git push — injected from AGENT_GITHUB_TOKEN env var",
    )
    git_author_name: str = Field(description="Git commit author name")
    git_author_email: str = Field(description="Git commit author email")


class MonitorConfig(BaseModel):
    """Service health monitor config."""
    poll_interval: int = Field(description="Seconds between health check polls")
    watched_stacks: list[str] = Field(
        default=[],
        description="Retained for backwards-compatibility. Removed in Fix 6.",
    )


class SafeModeResourcesConfig(BaseModel):
    """Per-resource safe mode lists. Any action whose primary target matches a prefix here is forced to tier 3."""
    stacks: list[str] = Field(default=[], description="Stack name prefixes to force tier 3")
    services: list[str] = Field(default=[], description="Service name prefixes to force tier 3")
    nodes: list[str] = Field(default=[], description="Node hostname prefixes to force tier 3")


class SafetyConfig(BaseModel):
    """Autonomy tier and safe mode policy."""
    global_safe_mode: bool = Field(
        description="When true, ALL actions behave like tier 3 — agent proposes, operator approves",
    )
    safe_mode_resources: SafeModeResourcesConfig = Field(
        default_factory=SafeModeResourcesConfig,
        description="Per-resource tier-3 overrides",
    )
    tool_tiers: dict[str, TierValue] = Field(
        default_factory=dict,
        description="Per-tool tier overrides. Valid values: 1, 2, 3, 'agent'",
    )
    log_agent_tier_reasoning: bool = Field(
        description="When a tool is tagged 'agent', log its tier reasoning to the action log",
    )


class ReportsConfig(BaseModel):
    """Incident report storage."""
    path: str = Field(description="Path relative to ansible.repo_path where reports are saved")
    tags: list[str] = Field(description="Predefined tag list agents must choose from")


class ActionLogConfig(BaseModel):
    """JSONL action log file location."""
    path: str = Field(description="Path to the action log file, e.g. ./action.log")


class ApprovalListenerConfig(BaseModel):
    """Slack approval webhook listener."""
    host: str = Field(default="0.0.0.0", description="Host to bind the approval listener")
    port: int = Field(ge=1024, le=65535, default=8765, description="Port to bind the approval listener")


class HistoryConfig(BaseModel):
    """Conversation history persistence."""
    path: str = Field(default="./agent_history.json", description="Path to the agent history JSON file")


class RollbackConfig(BaseModel):
    """Docker stack rollback state."""
    state_path: str = Field(default="./rollback_state.json", description="Path to the rollback state JSON file")


# ---------------------------------------------------------------------------
# YAML + env-var settings source
# ---------------------------------------------------------------------------

class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    """Loads config from a YAML file and injects env var secrets into the dict."""

    def __init__(self, settings_cls: type, yaml_path: str) -> None:
        super().__init__(settings_cls)
        self._path = yaml_path

    def get_field_value(self, field: FieldInfo, field_name: str):
        # Not called — __call__ returns the full dict.
        # pydantic-settings v2 uses __call__ when it returns a dict.
        return None

    def field_is_complex(self, field: FieldInfo) -> bool:
        # Tell pydantic-settings not to flatten nested models into dotted env-var keys.
        return True

    def __call__(self) -> dict:
        """Return raw YAML dict with env var secrets injected at their sub-model paths."""
        with open(self._path) as f:
            data = yaml.safe_load(f) or {}

        _env_map: dict[tuple[str, str], str] = {
            ("anthropic", "api_key"): "ANTHROPIC_API_KEY",
            ("slack", "bot_token"): "SLACK_BOT_TOKEN",
            ("slack", "signing_secret"): "SLACK_SIGNING_SECRET",
            ("ansible", "git_token"): "AGENT_GITHUB_TOKEN",
        }
        for (section, field_name), env_var in _env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                data.setdefault(section, {})[field_name] = val

        return data


# ---------------------------------------------------------------------------
# Top-level config model
# ---------------------------------------------------------------------------

class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True)
    # populate_by_name=True: model_validate(raw_dict) resolves by Python attribute
    # name even when AliasChoices is set on a field.

    anthropic: AnthropicConfig
    slack: SlackConfig
    docker: DockerConfig
    swarm: SwarmConfig
    edge: EdgeConfig = Field(default_factory=EdgeConfig)
    ansible: AnsibleConfig
    monitor: MonitorConfig
    safety: SafetyConfig
    reports: ReportsConfig
    action_log: ActionLogConfig
    approval_listener: ApprovalListenerConfig = Field(default_factory=ApprovalListenerConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    rollback: RollbackConfig = Field(default_factory=RollbackConfig)

    @model_validator(mode="after")
    def _warn_missing_signing_secret(self) -> "AgentConfig":
        if not self.slack.signing_secret:
            warnings.warn(
                "slack.signing_secret is not set — approval listener will be "
                "restricted to localhost",
                stacklevel=2,
            )
        return self


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def load_agent_config(yaml_path: str) -> AgentConfig:
    """Load and validate config from yaml_path with env var secrets injected."""
    source = YamlConfigSettingsSource(AgentConfig, yaml_path)

    class _Config(AgentConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            **kwargs: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            # Only YAML source (with env vars injected) + init_settings.
            # dotenv_settings and secrets_settings intentionally omitted —
            # this agent runs in a Docker container where env vars are canonical.
            return (source, kwargs["init_settings"])

    return _Config()
```

- [ ] **Verify syntax (no runtime errors):**
  ```bash
  cd agent
  python -c "from agent.config_schema import load_agent_config; print('OK')"
  ```
  Expected: `OK`

- [ ] **Commit:**
  ```bash
  git add agent/config_schema.py
  git commit -m "feat: add Pydantic config schema and YamlConfigSettingsSource"
  ```

---

## Task 3: Fix `config.yaml`

**Files:**
- Modify: `agent/config.yaml`

- [ ] **Remove the duplicate `commit_config_updates` tier-2 entry** (line 89 in the current file). Keep only the tier-3 entry at the bottom of `tool_tiers`. Add a comment:
  ```yaml
  commit_config_updates:   3  # tier 3: always requires explicit approval — modifies persistent config
  ```

- [ ] **Remove these four keys entirely** (they are now injected from env vars):
  - `anthropic.api_key`
  - `slack.bot_token`
  - `slack.signing_secret`
  - `ansible.git_token`

- [ ] **Add `history` and `rollback` sections** at the end of the file:
  ```yaml
  history:
    path: ./agent_history.json

  rollback:
    state_path: ./rollback_state.json
  ```

- [ ] **Verify the config loads cleanly** (set dummy env vars so required secrets don't block):
  ```bash
  cd agent
  ANTHROPIC_API_KEY=test SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test AGENT_GITHUB_TOKEN=test \
    python -c "
  from agent.config_schema import load_agent_config
  cfg = load_agent_config('config.yaml')
  print('model:', cfg.anthropic.model)
  print('channel:', cfg.slack.channel)
  print('nodes:', cfg.swarm.nodes)
  print('history:', cfg.history.path)
  print('rollback:', cfg.rollback.state_path)
  "
  ```
  Expected: all values print correctly, no errors.

- [ ] **Commit:**
  ```bash
  git add config.yaml
  git commit -m "fix: remove duplicate config key and secret placeholders, add history/rollback sections"
  ```

---

## Task 4: Migrate `slack.py`

**Files:**
- Modify: `agent/agent/slack.py`

The only changes here are: `__init__` signature accepts `Optional[str]`, and `verify_signature` guards against `None` secret.

- [ ] **Update `SlackClient.__init__` signature** (line 26):
  ```python
  from typing import Optional

  def __init__(self, bot_token: Optional[str], signing_secret: Optional[str], channel: str) -> None:
  ```

- [ ] **Guard `verify_signature`** — add an early return before the `hmac` call (around line 42):
  ```python
  def verify_signature(self, timestamp: str, raw_body: bytes, signature: str) -> bool:
      if self._secret is None:
          return False
      if abs(time.time() - float(timestamp)) > 300:
          return False
      base = f"v0:{timestamp}:{raw_body.decode()}"
      expected = "v0=" + hmac.new(
          self._secret.encode(), base.encode(), digestmod=hashlib.sha256
      ).hexdigest()
      return hmac.compare_digest(expected, signature)
  ```

- [ ] **Commit:**
  ```bash
  git add agent/slack.py
  git commit -m "fix: SlackClient accepts Optional secrets, guard verify_signature against None"
  ```

---

## Task 5: Migrate `safety.py`

**Files:**
- Modify: `agent/agent/safety.py`

- [ ] **Update import and `__init__` signature:**
  ```python
  from __future__ import annotations
  from typing import TYPE_CHECKING
  if TYPE_CHECKING:
      from .config_schema import AgentConfig
  ```

  Change `__init__`:
  ```python
  def __init__(self, config: "AgentConfig") -> None:
      safety = config.safety
      self.global_safe_mode: bool = safety.global_safe_mode

      safe_resources = safety.safe_mode_resources
      self._safe_stacks: list[str] = safe_resources.stacks
      self._safe_services: list[str] = safe_resources.services
      self._safe_nodes: list[str] = safe_resources.nodes

      self.tool_tiers: dict[str, int | str] = dict(safety.tool_tiers)
      self.log_agent_tier_reasoning: bool = safety.log_agent_tier_reasoning
  ```

- [ ] **Verify syntax:**
  ```bash
  cd agent
  python -c "from agent.safety import SafetyPolicy; print('OK')"
  ```
  Expected: `OK`

- [ ] **Commit:**
  ```bash
  git add agent/safety.py
  git commit -m "refactor: migrate SafetyPolicy to accept AgentConfig"
  ```

---

## Task 6: Migrate `monitor.py`

**Files:**
- Modify: `agent/agent/monitor.py`

- [ ] **Update import and `__init__` signature:**
  ```python
  from __future__ import annotations
  from typing import TYPE_CHECKING
  if TYPE_CHECKING:
      from .config_schema import AgentConfig
  ```

  Change `__init__`:
  ```python
  def __init__(
      self,
      config: "AgentConfig",
      event_queue: asyncio.Queue,
      action_logger: ActionLogger,
  ) -> None:
      self._poll_interval: int = config.monitor.poll_interval
      self._docker_socket: str = config.docker.socket
      self._event_queue = event_queue
      self._logger = action_logger
      self._down_since: dict[str, datetime] = {}
  ```

- [ ] **Verify syntax:**
  ```bash
  cd agent
  python -c "from agent.monitor import MonitorDaemon; print('OK')"
  ```
  Expected: `OK`

- [ ] **Commit:**
  ```bash
  git add agent/monitor.py
  git commit -m "refactor: migrate MonitorDaemon to accept AgentConfig"
  ```

---

## Task 7: Migrate `tools.py`

**Files:**
- Modify: `agent/agent/tools.py`

`ToolExecutor.__init__` has the most dict accesses. Replace all of them.

- [ ] **Update import and `ToolExecutor.__init__`** (around line 355). Replace the `config.get(...)` calls with attribute access:

  ```python
  from __future__ import annotations
  from typing import TYPE_CHECKING
  if TYPE_CHECKING:
      from .config_schema import AgentConfig
  ```

  ```python
  def __init__(self, config: "AgentConfig", slack_client: SlackClient) -> None:
      self._config = config
      self._slack = slack_client
      self._docker_socket = config.docker.socket
      self._ssh_key = config.swarm.ssh_key
      self._ssh_user = config.swarm.ssh_user
      self._repo_path = config.ansible.repo_path
      self._inventory = config.ansible.inventory
      self._git_token = config.ansible.git_token or ""
      self._git_author_name = config.ansible.git_author_name
      self._git_author_email = config.ansible.git_author_email
      self._rollback_state_path = Path(config.rollback.state_path)
      self._reports_path = Path(self._repo_path) / config.reports.path
      self._action_log_path = Path(config.action_log.path)
      self._secrets: list[str] = [s for s in [self._git_token] if s]
  ```

- [ ] **Verify syntax:**
  ```bash
  cd agent
  python -c "from agent.tools import ToolExecutor; print('OK')"
  ```
  Expected: `OK`

- [ ] **Commit:**
  ```bash
  git add agent/tools.py
  git commit -m "refactor: migrate ToolExecutor to accept AgentConfig"
  ```

---

## Task 8: Migrate `agent.py`

**Files:**
- Modify: `agent/agent/agent.py`

`HomelabAgent.__init__` has many dict accesses. Replace them all.

- [ ] **Add import at top of `agent.py`:**
  ```python
  from .config_schema import AgentConfig
  ```

- [ ] **Update `HomelabAgent.__init__` signature and body** (around line 324):

  ```python
  def __init__(self, config: AgentConfig) -> None:
      self._config = config
      self._model: str = config.anthropic.model
      self._client = anthropic.AsyncAnthropic(api_key=config.anthropic.api_key or "")
      self._input_cost_per_mtok: float = config.anthropic.input_cost_per_mtok
      self._output_cost_per_mtok: float = config.anthropic.output_cost_per_mtok

      self._slack = SlackClient(
          bot_token=config.slack.bot_token,
          signing_secret=config.slack.signing_secret,
          channel=config.slack.channel,
      )
      self._veto_window: int = config.slack.veto_window_seconds

      log_path = config.action_log.path
      self._logger = ActionLogger(log_path)
      self._safety = SafetyPolicy(config)
      self._tools = ToolExecutor(config, self._slack)
      self._pending = PendingApprovals()

      self._history_path = Path(config.history.path)
      self._history: list[dict] = self._load_history()
      self._last_cost_breakdown: str = ""
      self._zar_rate: float | None = None
      self._zar_rate_fetched_at: datetime | None = None
      self._system_prompt = build_system_prompt()
      self._active_execution: dict | None = None
  ```

- [ ] **Verify syntax:**
  ```bash
  cd agent
  python -c "from agent.agent import HomelabAgent; print('OK')"
  ```
  Expected: `OK`

- [ ] **Commit:**
  ```bash
  git add agent/agent.py
  git commit -m "refactor: migrate HomelabAgent to accept AgentConfig"
  ```

---

## Task 9: Migrate `cli.py`

**Files:**
- Modify: `agent/cli.py`

- [ ] **Replace `load_config`** — remove the env-var substitution regex and return `AgentConfig`:

  ```python
  from pydantic import ValidationError
  from agent.config_schema import AgentConfig, load_agent_config

  def load_config(path: str) -> AgentConfig:
      try:
          return load_agent_config(path)
      except ValidationError as e:
          for err in e.errors():
              loc = " → ".join(str(x) for x in err["loc"])
              console.print(f"[bold red]CONFIG ERROR:[/bold red] {loc}: {err['msg']}")
          sys.exit(1)
  ```

  Remove the old `import re`, `import os`, `import yaml` imports if they're no longer used elsewhere in the file. (Check before removing — `re` is used in `_parse_log_range`.)

- [ ] **Update `run_check` signature and body** (line 62):
  ```python
  async def run_check(config: AgentConfig) -> None:
      import docker
      socket = config.docker.socket
      # rest unchanged
  ```

- [ ] **Update `amain`** — replace the two dict accesses:
  ```python
  log_path = config.action_log.path
  # ...
  listener_host = config.approval_listener.host
  listener_port = config.approval_listener.port
  ```
  Remove the `listener_cfg = config.get(...)` lines.

- [ ] **Update `run_repl` signature:**
  ```python
  async def run_repl(agent: HomelabAgent, config: AgentConfig, event_queue: asyncio.Queue, log_path: str) -> None:
  ```
  The body only passes `config` to `run_check(config)` — that call already works once `run_check` is updated.

- [ ] **Verify the full CLI imports cleanly:**
  ```bash
  cd agent
  python -c "import cli; print('OK')"
  ```
  Expected: `OK`

- [ ] **Commit:**
  ```bash
  git add cli.py
  git commit -m "refactor: cli.py load_config returns AgentConfig, migrate all dict access"
  ```

---

## Task 10: Migrate `config_cli.py`

**Files:**
- Modify: `agent/config_cli.py`

This file changes the most in terms of structure: drop `ruamel.yaml`, switch to `yaml.safe_load`/`yaml.dump`, add `validate` command, add write-back validation.

- [ ] **Replace the import block.** Remove `from ruamel.yaml import YAML` and the `yaml = YAML()` / `yaml.preserve_quotes = True` lines. Add:
  ```python
  import yaml
  from pydantic import ValidationError
  from agent.config_schema import AgentConfig, load_agent_config
  ```

- [ ] **Replace `_load` and `_save`:**
  ```python
  def _load() -> tuple[dict, Path]:
      path = CONFIG_PATH
      with open(path) as f:
          return yaml.safe_load(f) or {}, path

  def _save(data: dict, path: Path) -> None:
      try:
          AgentConfig.model_validate(data)
      except ValidationError as e:
          for err in e.errors():
              loc = " → ".join(str(x) for x in err["loc"])
              print(f"CONFIG ERROR: {loc}: {err['msg']}")
          print("Aborting — config not written.")
          sys.exit(1)
      with open(path, "w") as f:
          yaml.dump(data, f, sort_keys=False, default_flow_style=False)
  ```

- [ ] **Add `cmd_validate` and register it in `COMMANDS`:**
  ```python
  def cmd_validate(_args: list[str]) -> None:
      import warnings
      with warnings.catch_warnings(record=True) as caught:
          warnings.simplefilter("always")
          try:
              load_agent_config(str(CONFIG_PATH))
          except ValidationError as e:
              for err in e.errors():
                  loc = " → ".join(str(x) for x in err["loc"])
                  print(f"CONFIG ERROR: {loc}: {err['msg']}")
              sys.exit(1)
      for w in caught:
          print(f"CONFIG WARNING: {w.message}")
      print("Config is valid.")
  ```

  In `COMMANDS`:
  ```python
  "validate": cmd_validate,
  ```

  Update the docstring at the top of the file to add:
  ```
  python config_cli.py validate
  ```

- [ ] **Verify syntax and that existing commands still work:**
  ```bash
  cd agent
  python config_cli.py show
  ```
  Expected: config printed without errors.

  ```bash
  ANTHROPIC_API_KEY=test SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test AGENT_GITHUB_TOKEN=test \
    python config_cli.py validate
  ```
  Expected: `Config is valid.`

- [ ] **Commit:**
  ```bash
  git add config_cli.py
  git commit -m "refactor: config_cli.py drop ruamel.yaml, add validate command, validate on write"
  ```

---

## Task 11: Smoke test end-to-end

- [ ] **Run a full import check** to confirm no broken imports across the agent package:
  ```bash
  cd agent
  ANTHROPIC_API_KEY=test SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test AGENT_GITHUB_TOKEN=test \
    python -c "
  from agent.config_schema import load_agent_config
  from agent.agent import HomelabAgent
  from agent.monitor import MonitorDaemon
  from agent.safety import SafetyPolicy
  from agent.tools import ToolExecutor
  import cli
  cfg = load_agent_config('config.yaml')
  print('anthropic model:', cfg.anthropic.model)
  print('safety global_safe_mode:', cfg.safety.global_safe_mode)
  print('history path:', cfg.history.path)
  print('rollback path:', cfg.rollback.state_path)
  print('All imports OK')
  "
  ```
  Expected: all values print, `All imports OK`.

- [ ] **Verify the duplicate key is gone** — run validate:
  ```bash
  ANTHROPIC_API_KEY=test SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test AGENT_GITHUB_TOKEN=test \
    python config_cli.py validate
  ```
  Expected: `CONFIG WARNING: slack.signing_secret is not set...` (because we passed a dummy value — actually will not warn since we set it). Or if not set: warning prints, exits 0.

- [ ] **Final commit:**
  ```bash
  git add -A
  git commit -m "fix: Fix 5 complete — Pydantic config schema, startup validation, remove duplicate key"
  ```

---

## Task 12: Open PR

- [ ] **Push branch and open PR:**
  ```bash
  git push -u origin HEAD
  gh pr create \
    --title "fix: Pydantic config schema + startup validation (Fix 5)" \
    --body "$(cat <<'EOF'
  ## Summary
  - Introduces Pydantic v2 + pydantic-settings config model, replacing all untyped dict access
  - Fixes duplicate \`commit_config_updates\` key in config.yaml (was silently taking tier 3 over tier 2)
  - Secrets (\`ANTHROPIC_API_KEY\`, \`SLACK_BOT_TOKEN\`, \`SLACK_SIGNING_SECRET\`, \`AGENT_GITHUB_TOKEN\`) are now injected from env vars — removed from config.yaml
  - Adds \`python config_cli.py validate\` command for startup/CI config validation
  - Guards \`SlackClient.verify_signature\` against None secret

  ## Test plan
  - [ ] \`python config_cli.py validate\` exits 0 with valid config + env vars set
  - [ ] \`python config_cli.py validate\` exits 1 with an invalid tier value (e.g. edit \`run_shell: 5\` manually)
  - [ ] \`python config_cli.py show\` prints clean YAML (no comments, secrets absent)
  - [ ] \`python config_cli.py safemode on\` then \`off\` — verify write-back works
  - [ ] Agent starts without error: \`python cli.py --check\`

  Spec: \`docs/superpowers/specs/2026-03-24-fix5-pydantic-config-schema-design.md\`

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```
