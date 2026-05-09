# CLAUDE.md — openai-agent

Same functionality as `agent/` but uses the Ollama Python SDK targeting Ollama's
native `/api/chat` endpoint, which supports structured tool calling and `think: false`
to disable qwen3 extended thinking.

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
| SDK | `anthropic` | `ollama` |
| Endpoint | `/v1/messages` (Anthropic) | `/api/chat` (Ollama native) |
| Tool format | `input_schema` | `function.parameters` |
| Tool results | `role: user`, `type: tool_result` | `role: tool`, `content` |
| Config model key | `anthropic.model` | `model.name` |
| Prompt caching | Yes (Anthropic-specific) | No |
| Extended thinking | No | Disabled via `think: false` |

## Config

`config.yaml` has a single `model:` section:

```yaml
model:
  name: "qwen3.6:27b"           # active model
  base_url: "http://192.168.88.144:11434"  # Ollama host (no /v1 suffix)
  input_cost_per_mtok: 0.0      # 0 for local models
  output_cost_per_mtok: 0.0
  available_models:
    - qwen3.6:27b
    - qwen2.5-coder:32b
```

## Architecture

Identical to `agent/` — see that CLAUDE.md for the full architecture description.
The only modified files are:

- `agent/agent.py` — OpenAI SDK, different message/tool format
- `agent/tools.py` — OpenAI function-calling schema
- `agent/config_schema.py` — `ModelConfig` (merged anthropic+llm sections)
- `controller.py` — uses `config.model.name` / `config.model.available_models`
- `cli.py` — imports updated for new schema
- `config_cli.py` — `model.*` keys instead of `anthropic.*`
