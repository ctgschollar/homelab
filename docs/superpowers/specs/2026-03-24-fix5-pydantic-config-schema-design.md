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

A Pydantic v2 model tree using `pydantic-settings` `BaseSettings` at the top level. Covers all config sections from `config.yaml`:

- `AnthropicConfig` — `api_key`, `model`, `input_cost_per_mtok`, `output_cost_per_mtok`
- `SlackConfig` — `bot_token`, `signing_secret`, `channel`, `veto_window_seconds`
- `DockerConfig` — `socket`
- `SwarmConfig` — `nodes`, `ssh_key`, `ssh_user`
- `EdgeConfig` — `cloudflare_tunnel_node`, `ssh_key`, `ssh_user`
- `AnsibleConfig` — `repo_path`, `inventory`, `git_token`, `git_author_name`, `git_author_email`
- `MonitorConfig` — `poll_interval`, `watched_stacks`
- `SafeModeResourcesConfig` — `stacks`, `services`, `nodes`
- `SafetyConfig` — `global_safe_mode`, `safe_mode_resources`, `tool_tiers`, `log_agent_tier_reasoning`
- `ReportsConfig` — `path`, `tags`
- `ActionLogConfig` — `path`
- `ApprovalListenerConfig` — `host`, `port`
- `AgentConfig(BaseSettings)` — top-level, composes all of the above

**Type constraints:**
- `TierValue = Literal[1, 2, 3, "agent"]` — catches invalid tier values
- `veto_window_seconds: int = Field(gt=0, default=300)`
- `port: int = Field(ge=1024, le=65535, default=8765)`

**Field descriptions** replace the inline YAML comments as the documentation source of truth.

**Secrets via environment variables:**
Sensitive fields (`api_key`, `bot_token`, `signing_secret`, `git_token`) use `validation_alias` to read from the existing environment variable names (`ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `AGENT_GITHUB_TOKEN`). The `${...}` placeholder strings are removed from `config.yaml`. No deployment changes required — env var names are preserved.

**Config source:** A custom `YamlConfigSettingsSource` is registered on `AgentConfig` so `pydantic-settings` loads non-secret values from the YAML file, with env vars taking precedence.

**Startup warning:** A `@model_validator(mode="after")` on `AgentConfig` warns if `signing_secret` is unset.

### 2. `load_config` migration — `cli.py`

`load_config(path: str) -> dict` becomes `load_config(path: str) -> AgentConfig`. The env-var substitution regex (`${...}` → `os.environ`) is removed — `pydantic-settings` handles env vars natively. On `ValidationError`, each error is printed with a `[bold red]CONFIG ERROR:[/bold red]` prefix and the process exits with code 1.

### 3. Dict access migration

All `config["key"]["subkey"]` access across `agent.py`, `safety.py`, `tools.py`, `monitor.py`, `slack.py`, `cli.py`, `config_cli.py` is replaced with typed attribute access (`config.safety.global_safe_mode`, etc.).

### 4. Fix duplicate key in `config.yaml`

Remove the tier-2 `commit_config_updates` entry, leaving only the tier-3 entry with a comment explaining why.

Remove all `${...}` placeholder strings from secrets fields — those values now come from the environment via pydantic-settings.

### 5. `config_cli.py` simplification

Drop `ruamel.yaml`. Load config via `AgentConfig`, modify attributes, write back with `yaml.dump(config.model_dump(exclude=<secret_fields>))` — secrets are excluded since they live in the environment. Add a `validate` command: loads and validates the config, prints errors/warnings, exits 0 if clean or 1 if errors.

### 6. `pyproject.toml`

Add `pydantic>=2.0` and `pydantic-settings>=2.0` to the `[project] dependencies` list. No `requirements.txt` (project uses `pyproject.toml`).

---

## Files changed

| File | Change |
|------|--------|
| `agent/agent/config_schema.py` | New — full Pydantic model tree |
| `agent/config.yaml` | Fix duplicate key; remove `${...}` secret placeholders |
| `agent/cli.py` | `load_config` returns `AgentConfig`; migrate dict access |
| `agent/config_cli.py` | Drop ruamel; write via `model_dump`; add `validate` command |
| `agent/agent/safety.py` | Migrate `config: dict` → `config: AgentConfig` |
| `agent/agent/agent.py` | Migrate dict access throughout |
| `agent/agent/tools.py` | Migrate dict access throughout |
| `agent/agent/monitor.py` | Migrate dict access throughout |
| `agent/agent/slack.py` | Migrate dict access throughout |
| `agent/pyproject.toml` | Add `pydantic>=2.0`, `pydantic-settings>=2.0` |

---

## Out of scope

- No tests for the config schema (intentionally excluded)
- No changes to other fix areas (shell guards, Slack listener, concurrency, history trimming, mute store)
