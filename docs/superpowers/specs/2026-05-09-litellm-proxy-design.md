# LiteLLM Proxy + Model Switching Design

**Date:** 2026-05-09
**Status:** Approved

## Overview

Deploy LiteLLM as a Swarm stack to proxy requests from the homelab agent to either local Ollama models (running on the LLM laptop at `192.168.3.200`) or Anthropic's Claude API. Also deploy Open WebUI as a Swarm stack for interactive chat. Add a `model` Slack command to the agent for listing and switching models at runtime.

## Components

### 1. `litellm/` Swarm Stack

- Image: `ghcr.io/berriai/litellm:main-latest`
- Config file (`litellm_config.yaml`) stored on a Linstor volume and mounted into the container
- Wildcard passthrough: `model_list: [{model_name: "*", litellm_params: {model: "ollama_chat/*", api_base: "http://192.168.3.200:11434"}}]`
- Claude models pass through to Anthropic via `ANTHROPIC_API_KEY` env var (Docker Swarm secret)
- Traefik at `litellm.schollar.dev`, port 4000
- No authentication (internal network only)

### 2. `open-webui/` Swarm Stack

- Image: `ghcr.io/open-webui/open-webui:main`
- `OLLAMA_BASE_URL` set to `http://192.168.3.200:11434`
- Linstor volume for persistent data (`pool_hdd`, 20G)
- Traefik at `chat.schollar.dev`, port 8080
- The existing systemd-based Open WebUI on the laptop is left untouched

### 3. Agent Config Changes

New `llm` section in `config.yaml`:

```yaml
llm:
  base_url: "https://litellm.schollar.dev"
  available_models:
    - claude-sonnet-4-20250514
    - ollama/gemma2:2b
    - ollama/qwen2.5-coder:14b
    - ollama/qwen2.5-coder:32b
```

The `anthropic.model` field remains the active model. Cost fields (`input_cost_per_mtok`, `output_cost_per_mtok`) stay on `AnthropicConfig` and are set to `0.0` when using a local model — updated manually when switching.

### 4. Agent Code Changes

**`config_schema.py`**
- Add `LlmConfig` with `base_url: str` and `available_models: list[str]`
- Add optional `llm: LlmConfig | None = None` to `AgentConfig`

**`agent.py`**
- When `config.llm` is set, pass `base_url=config.llm.base_url` to `anthropic.AsyncAnthropic()`
- No other changes — LiteLLM accepts the same Anthropic message format

**`controller.py`**
- Add `model` commands to `_COMMANDS`: `model`, `model list`, `model use <name>`, `model add <name>`, `model remove <name>`
- Implement `_cmd_model_*` handlers using the existing `_persist_mode` pattern for config persistence

## Slack Commands

| Command | Effect |
|---------|--------|
| `model` | Show current active model |
| `model list` | Show all available models |
| `model use <name>` | Switch active model, persist to `config.yaml` |
| `model add <name>` | Add model to available list, persist to `config.yaml` |
| `model remove <name>` | Remove model from available list, persist to `config.yaml` |

Switching to a local model does not automatically update cost fields — the user is responsible for setting these to `0.0` via `config_cli.py` if desired.

## Error Handling

No special handling for LiteLLM/Ollama unreachability. API errors surface as normal in logs and Slack, same as existing Anthropic API errors. No automatic fallback to Claude.

## Out of Scope

- Per-tool model routing (specced for future iteration)
- LiteLLM authentication
- Automatic cost tracking per model
- Any changes to Claude Code, claude-runner, or `ansible/roles/runner/`
