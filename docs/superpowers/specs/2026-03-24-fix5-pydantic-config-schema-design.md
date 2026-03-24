# Fix 5 — Pydantic Config Schema + Startup Validation

**Date:** 2026-03-24
**Scope:** `agent/` directory only
**PR:** standalone (one PR per fix)

---

## Problem

`config.yaml` contains a duplicate `commit_config_updates` key (tier 2 and tier 3). Python dict parsing silently takes the last value (tier 3). There is no startup check that catches this or any other config inconsistency. All config access throughout the codebase is untyped dict access (`config["key"]["subkey"]`), giving no validation, no IDE support, and no early failure on misconfiguration.

---

## Design

### 1. Schema — `agent/agent/config_schema.py`

A Pydantic v2 model tree using `pydantic-settings` `BaseSettings` at the top level.

**Sub-models** (plain `BaseModel`):

```python
TierValue = Literal[1, 2, 3, "agent"]

class AnthropicConfig(BaseModel):
    api_key: Optional[str] = None
    model: str
    input_cost_per_mtok: float
    output_cost_per_mtok: float

class SlackConfig(BaseModel):
    bot_token: Optional[str] = None
    signing_secret: Optional[str] = None
    channel: str
    veto_window_seconds: int = Field(gt=0, default=300)  # optional, defaults to 300

class DockerConfig(BaseModel):
    socket: str

class SwarmConfig(BaseModel):
    nodes: list[str]
    ssh_key: str
    ssh_user: str

class EdgeConfig(BaseModel):
    cloudflare_tunnel_node: str = ""
    ssh_key: str = ""
    ssh_user: str = ""
    # schema-only: no active code reads this section. All fields default to ""
    # so the section can be omitted from config.yaml without causing ValidationError.

class AnsibleConfig(BaseModel):
    repo_path: str
    inventory: str
    git_token: Optional[str] = None
    git_author_name: str
    git_author_email: str

class MonitorConfig(BaseModel):
    poll_interval: int
    watched_stacks: list[str] = []
    # An empty list is a valid value (Fix 6 removes this field entirely).
    # Existing config.yaml entries are preserved until Fix 6.

class SafeModeResourcesConfig(BaseModel):
    stacks: list[str] = []
    services: list[str] = []
    nodes: list[str] = []

class SafetyConfig(BaseModel):
    global_safe_mode: bool
    safe_mode_resources: SafeModeResourcesConfig
    tool_tiers: dict[str, TierValue]
    log_agent_tier_reasoning: bool

class ReportsConfig(BaseModel):
    path: str
    tags: list[str]

class ActionLogConfig(BaseModel):
    path: str

class ApprovalListenerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(ge=1024, le=65535, default=8765)  # optional, defaults to 8765

class HistoryConfig(BaseModel):
    path: str = "./agent_history.json"

class RollbackConfig(BaseModel):
    state_path: str = "./rollback_state.json"
```

**Top-level model:**

```python
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True)
    # populate_by_name=True: model_validate(raw_dict) resolves fields by Python
    # attribute name. AliasChoices on secret fields adds the env var name as an
    # additional alias — both the attribute name and the env var name work for lookup.

    anthropic: AnthropicConfig
    slack: SlackConfig
    docker: DockerConfig
    swarm: SwarmConfig
    edge: EdgeConfig = EdgeConfig()   # optional section; all fields default to ""
    ansible: AnsibleConfig
    monitor: MonitorConfig
    safety: SafetyConfig
    reports: ReportsConfig
    action_log: ActionLogConfig
    approval_listener: ApprovalListenerConfig = ApprovalListenerConfig()
    history: HistoryConfig = HistoryConfig()
    rollback: RollbackConfig = RollbackConfig()

    @model_validator(mode="after")
    def _warn_missing_signing_secret(self) -> "AgentConfig":
        if not self.slack.signing_secret:
            warnings.warn(
                "slack.signing_secret is not set — approval listener will be "
                "restricted to localhost"
            )
        return self
```

**Secret fields** use `AliasChoices` so both the Python attribute name and the env var name are valid lookup keys. This satisfies both `model_validate(raw_dict)` (uses attribute name) and env var resolution (uses env var name):

```python
# Inside AnthropicConfig:
api_key: Optional[str] = Field(
    default=None,
    validation_alias=AliasChoices("api_key", "ANTHROPIC_API_KEY"),
)

# Inside SlackConfig:
bot_token: Optional[str] = Field(
    default=None,
    validation_alias=AliasChoices("bot_token", "SLACK_BOT_TOKEN"),
)
signing_secret: Optional[str] = Field(
    default=None,
    validation_alias=AliasChoices("signing_secret", "SLACK_SIGNING_SECRET"),
)

# Inside AnsibleConfig:
git_token: Optional[str] = Field(
    default=None,
    validation_alias=AliasChoices("git_token", "AGENT_GITHUB_TOKEN"),
)
```

Note: `AliasChoices` is on the sub-model fields (not on `AgentConfig`). Sub-models are plain `BaseModel` so pydantic-settings env var resolution doesn't apply to them directly — only `AgentConfig`-level fields are read from env vars by pydantic-settings. The `AliasChoices` on sub-model fields allows `model_validate(raw_dict)` to work by attribute name, and the env var names are used by `YamlConfigSettingsSource` returning the raw YAML dict combined with `AgentConfig`'s `env_settings` layer resolving the top-level env vars. **For env vars on nested models to work, `AgentConfig`'s env var source must use a prefix or the env vars must be at top level.** Since the secret fields live in sub-models (`anthropic.api_key`, not `api_key`), pydantic-settings will not find `ANTHROPIC_API_KEY` as a nested field by default. To resolve this, the `YamlConfigSettingsSource.__call__` must inject env var values into the returned dict:

```python
def __call__(self) -> dict:
    with open(self._path) as f:
        data = yaml.safe_load(f) or {}
    # Inject env var secrets into the appropriate sub-dicts
    import os
    _env_map = {
        ("anthropic", "api_key"): "ANTHROPIC_API_KEY",
        ("slack", "bot_token"): "SLACK_BOT_TOKEN",
        ("slack", "signing_secret"): "SLACK_SIGNING_SECRET",
        ("ansible", "git_token"): "AGENT_GITHUB_TOKEN",
    }
    for (section, field), env_var in _env_map.items():
        val = os.environ.get(env_var)
        if val is not None:
            data.setdefault(section, {})[field] = val
    return data
```

This approach keeps all config loading in a single source (the YAML dict enriched with env vars), removes dependency on `BaseSettings`'s env var layer for nested fields, and means the `env_settings` source in `settings_customise_sources` can be dropped in favour of explicit injection. The factory becomes:

```python
def load_agent_config(yaml_path: str) -> AgentConfig:
    class _Config(AgentConfig):
        @classmethod
        def settings_customise_sources(cls, settings_cls, **kwargs):
            # Only YAML source (with env vars injected) + init_settings.
            # dotenv_settings and secrets_settings are intentionally omitted:
            # this agent runs in a Docker container where env vars are the
            # canonical secrets mechanism.
            return (
                YamlConfigSettingsSource(settings_cls, yaml_path),
                kwargs["init_settings"],
            )
    return _Config()
```

**`YamlConfigSettingsSource`:**

```python
class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls, yaml_path: str) -> None:
        super().__init__(settings_cls)
        self._path = yaml_path

    def get_field_value(self, field: FieldInfo, field_name: str):
        # Not called — __call__ returns the full dict.
        # pydantic-settings v2 expects return type Any (single value, not a tuple).
        return None

    def field_is_complex(self, field: FieldInfo) -> bool:
        # Required: tells pydantic-settings not to flatten nested models into
        # env-var-style dotted keys. All fields are complex (nested dicts).
        return True

    def __call__(self) -> dict:
        """Return raw YAML dict with env var secrets injected."""
        with open(self._path) as f:
            data = yaml.safe_load(f) or {}
        import os
        _env_map = {
            ("anthropic", "api_key"): "ANTHROPIC_API_KEY",
            ("slack", "bot_token"): "SLACK_BOT_TOKEN",
            ("slack", "signing_secret"): "SLACK_SIGNING_SECRET",
            ("ansible", "git_token"): "AGENT_GITHUB_TOKEN",
        }
        for (section, field), env_var in _env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                data.setdefault(section, {})[field] = val
        return data
```

**`load_agent_config` factory:**

```python
def load_agent_config(yaml_path: str) -> AgentConfig:
    class _Config(AgentConfig):
        @classmethod
        def settings_customise_sources(cls, settings_cls, **kwargs):
            return (
                YamlConfigSettingsSource(settings_cls, yaml_path),
                kwargs["init_settings"],
            )
    return _Config()
```

### 2. `load_config` — `cli.py`

```python
def load_config(path: str) -> AgentConfig:
```

Calls `load_agent_config(path)`. Removes the old env-var substitution regex. On `ValidationError`, prints each error with `[bold red]CONFIG ERROR:[/bold red]` prefix, exits with code 1.

**All dict accesses in `cli.py` to migrate:**

- `run_check(config: dict)` → `run_check(config: AgentConfig)`:
  - `config.get("docker", {}).get("socket", ...)` → `config.docker.socket`
- `amain`:
  - `config.get("action_log", {}).get("path", "./action.log")` → `config.action_log.path`
  - `config.get("approval_listener", {})` host/port → `config.approval_listener.host` / `.port`
- `run_repl(agent, config: dict, ...)` → `run_repl(agent, config: AgentConfig, ...)`

### 3. Dict access migration — all files

**`agent.py` (`HomelabAgent.__init__` and elsewhere):**
- All `anthropic`, `slack`, `action_log`, `history` section accesses → attribute access
- `config.get("history", {}).get("path", ...)` → `config.history.path`

**`tools.py`:**
- `config.get("rollback", {}).get("state_path", ...)` → `config.rollback.state_path`
- All other config dict accesses → attribute access

**`safety.py`:**
- `SafetyPolicy.__init__(self, config: dict)` → `SafetyPolicy.__init__(self, config: AgentConfig)`
- All dict accesses → attribute access

**`monitor.py`:**
- `MonitorDaemon.__init__` and all dict accesses → attribute access

**`slack.py` (`SlackClient`):**
- `SlackClient.__init__` signature updated to accept `Optional[str]` for `bot_token` and `signing_secret`
- `verify_signature` currently calls `self._secret.encode()` unconditionally — guard it: if `self._secret` is `None`, return `False` (treat as unverified)
- All dict accesses → attribute access

### 4. Fix `config.yaml`

- Remove the tier-2 `commit_config_updates` entry (keep tier-3, add explanatory comment)
- Remove `anthropic.api_key`, `slack.bot_token`, `slack.signing_secret`, `ansible.git_token` keys entirely — `YamlConfigSettingsSource.__call__` now injects these from env vars. They must not appear in the YAML file so they are never written to disk by `config_cli.py`.
- Add `history` section: `path: ./agent_history.json`
- Add `rollback` section: `state_path: ./rollback_state.json`

### 5. `config_cli.py` simplification

Drop `ruamel.yaml`. **Inline YAML comments are intentionally removed** — field descriptions in the Pydantic models are the documentation source of truth going forward.

Write-back pattern for all mutating commands:
1. Load raw YAML dict with `yaml.safe_load`
2. Mutate the raw dict
3. Validate via `AgentConfig.model_validate(raw_dict)` — `populate_by_name=True` ensures attribute names resolve from the dict; secret fields are `Optional` and absent from the raw dict, so validation passes without env vars present
4. If valid, write back with `yaml.dump(raw_dict, sort_keys=False, default_flow_style=False)` — `sort_keys=False` preserves section order; `default_flow_style=False` ensures lists (e.g. `reports.tags`) render as block-style YAML, not inline. Secrets are absent from the raw YAML dict (never in the YAML file per Section 4), so they cannot be written to disk.
5. If invalid, abort and print errors

`cmd_set` and all other commands use this raw-dict pattern.

**`validate` command:**
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
Exits 0 if no `ValidationError` (warnings do not affect exit code).

### 6. `pyproject.toml`

In `[project] dependencies`:
- Add `pydantic>=2.0`
- Add `pydantic-settings>=2.0`
- Remove `ruamel.yaml>=0.18.0`

---

## Files changed

| File | Change |
|------|--------|
| `agent/agent/config_schema.py` | New — full Pydantic model tree, `YamlConfigSettingsSource`, `load_agent_config` factory |
| `agent/config.yaml` | Fix duplicate key; remove secret keys; add `history` + `rollback` sections |
| `agent/cli.py` | `load_config` returns `AgentConfig`; migrate all dict access |
| `agent/config_cli.py` | Drop ruamel; raw-dict write-back with Pydantic validation; add `validate` command |
| `agent/agent/safety.py` | `config: dict` → `config: AgentConfig`; migrate all dict access |
| `agent/agent/agent.py` | All dict access → attribute access |
| `agent/agent/tools.py` | All dict access → attribute access |
| `agent/agent/monitor.py` | All dict access → attribute access |
| `agent/agent/slack.py` | `SlackClient.__init__` accepts `Optional[str]`; all dict access → attribute access |
| `agent/pyproject.toml` | Add pydantic deps; remove `ruamel.yaml` |

---

## Out of scope

- No tests (intentionally excluded)
- No changes to other fix areas (shell guards, Slack listener, concurrency, history trimming, mute store)
- `watched_stacks` kept in schema; removal is part of Fix 6
