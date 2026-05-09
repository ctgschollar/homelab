# CLAUDE.md — openai-agent

Same functionality as `agent/` but uses the OpenAI Python SDK targeting Ollama's
`/v1/chat/completions` endpoint, which has better tool-calling support than the
Anthropic-compatible endpoint.

## Commands

```bash
# Run the agent (interactive REPL + monitor)
hatch run agent

# Run in daemon mode (monitor + Slack approval listener, no stdin)
hatch run agent --daemon

# Run a single question and exit
hatch run agent "why is sonarr down?"

# Check service health and exit
hatch run check

# Config management
hatch run config show
hatch run config safemode on|off
hatch run config set safety.global_safe_mode true
hatch run config set safety.tool_tiers.run_shell 2
hatch run config log-reasoning on|off
hatch run config validate

# Run tests
hatch run -e test pytest
```

## Key differences from `agent/`

| | `agent/` | `openai-agent/` |
|---|---|---|
| SDK | `anthropic` | `openai` |
| Endpoint | `/v1/messages` (Anthropic) | `/v1/chat/completions` (OpenAI) |
| Tool format | `input_schema` | `function.parameters` |
| Tool results | `role: user`, `type: tool_result` | `role: tool`, `tool_call_id` |
| Config model key | `anthropic.model` | `model.name` |
| Prompt caching | Yes (Anthropic-specific) | No |

## Config

`config.yaml` has a single `model:` section:

```yaml
model:
  name: "qwen3.6:27b"           # active model
  base_url: "http://192.168.88.144:11434/v1"
  api_key: "ollama"             # dummy key for Ollama
  input_cost_per_mtok: 0.0      # 0 for local models
  output_cost_per_mtok: 0.0
  available_models:
    - qwen3.6:27b
    - qwen2.5-coder:32b
```

Set a real API key via `AGENT_LLM_API_KEY` env var (e.g. for cloud OpenAI).

## Architecture

Identical to `agent/` — see that CLAUDE.md for the full architecture description.
The only modified files are:

- `agent/agent.py` — OpenAI SDK, different message/tool format
- `agent/tools.py` — OpenAI function-calling schema
- `agent/config_schema.py` — `ModelConfig` (merged anthropic+llm sections)
- `controller.py` — uses `config.model.name` / `config.model.available_models`
- `cli.py` — imports updated for new schema
- `config_cli.py` — `model.*` keys instead of `anthropic.*`
