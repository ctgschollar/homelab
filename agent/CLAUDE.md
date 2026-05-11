# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the agent (interactive REPL + monitor)
hatch run agent

# Run the agent in daemon mode (monitor + Slack approval listener, no stdin)
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
hatch run config safe-resource add stack|service|node <value>
hatch run config log-reasoning on|off
hatch run config validate

# Run tests
hatch run -e test pytest
hatch run -e test pytest tests/test_safety.py   # single file

# Post a test approval plan to Slack
hatch run agent --test-slack
```

The hatch default env uses Python 3.12 with a venv at `.venv`. Test env is separate.

## Architecture

The agent is an async Python application (`asyncio`) built around the Anthropic API. It manages Docker Swarm infrastructure autonomously, with human-in-the-loop approval for mutating operations.

### Core components

**`agent/agent.py` — `HomelabAgent`**
The central class. Owns the agentic loop: calls `messages.create`, routes tool calls through `SafetyPolicy.resolve_tier`, then either executes immediately (tier 1) or gates execution behind Slack/CLI approval (tier 2/3). Prompt caching is applied to the system prompt and tool definitions on every API call. Conversation history is trimmed to `MAX_HISTORY_TURNS` and serialized to `agent_history.json`. Cost is tracked per-turn and logged to `action.log` (JSONL).

**`agent/safety.py` — `SafetyPolicy`**
Resolves each tool call's effective execution tier (1/2/3). Resolution order: global safe mode → per-resource safe mode → explicit `tool_tiers` config → `agent`-discretion (for `run_shell` only, with regex pattern guards). Pattern guards in `_SHELL_FORCE_TIER3` / `_SHELL_FORCE_TIER2` can only escalate, never lower, the tier.

**`agent/tools.py` — `ToolExecutor` + `TOOL_DEFINITIONS`**
All tool implementations live here as `_tool_<name>` async methods. `TOOL_DEFINITIONS` is the list of Anthropic tool schemas — the last entry gets `cache_control: ephemeral` in `agent.py`. Docker calls run in a thread executor (blocking SDK); shell/SSH calls are serialized through `_shell_gate` semaphore.

**`agent/monitor.py` — `MonitorDaemon`**
Polls Docker Swarm every `monitor.poll_interval` seconds. Emits `services_down` (batched) and `service_recovered` events into the shared `asyncio.Queue`. First detection only — won't re-fire until the service recovers and degrades again.

**`agent/rag.py` — `IncidentRAG`**
Incident memory backed by PostgreSQL + pgvector. Uses Ollama (`nomic-embed-text`, 768-dim) for embeddings — configured via `rag.embed_url` and `rag.embed_model` in `config.yaml`. Only active when `AGENT_POSTGRES_DSN` is set. Schema is bootstrapped at startup via `init_schema()`.

**`agent/prompts.py`**
Builds the system prompt from three sections: infrastructure topology (`INFRA_CONTEXT`), tier rules (`TIER_RULES`), and behaviour rules (`BEHAVIOUR_RULES`). The system prompt is static across all turns and cached.

**`agent/slack.py` — `SlackClient`**
Wraps Slack Web API calls. Sends plan proposals as Block Kit messages with Approve/Deny buttons. Verifies request signatures when `SLACK_SIGNING_SECRET` is set.

**`agent/config_schema.py`**
Pydantic v2 settings schema. Config is loaded from `config.yaml`; secrets (`ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `AGENT_GITHUB_TOKEN`, `AGENT_POSTGRES_DSN`) are injected from environment variables via `YamlConfigSettingsSource`.

**`cli.py`**
Entry point. Wires up `HomelabAgent`, `MonitorDaemon`, the event consumer task, the REPL, and (in daemon mode) the FastAPI approval listener. The approval listener runs as a uvicorn server on the same event loop, handling `/slack/events` and `/slack/interactions`.

**`config_cli.py`**
CLI for editing `config.yaml` in place. Does a round-trip through the Pydantic schema on every write to prevent saving invalid config.

### Event flow

1. `MonitorDaemon` or Slack message → `asyncio.Queue`
2. `event_consumer` task dequeues and calls `agent.chat()` or `agent.handle_event()`
3. `HomelabAgent._run_loop()` iterates the Anthropic API, appending to `_history`
4. Tool calls → `_handle_tool_calls()` → `SafetyPolicy.resolve_tier()`
5. Tier 1: executed immediately (concurrently via `asyncio.gather`)
6. Tier 2/3: `_handle_approval_flow()` posts plan to Slack, registers a `Future` in `PendingApprovals`, awaits it
7. `/slack/interactions` POST resolves the `Future` → execution proceeds or is cancelled

### Approval flow

`PendingApprovals` maps `plan_id → asyncio.Future`. The FastAPI app and the REPL both call `pending.resolve()`. Tier 2 uses `asyncio.wait_for` with a timeout (defaults to 300s); tier 3 awaits indefinitely. A free-form REPL message cancels all pending plans and re-queues the text as agent context.

### Tool tiers

Tools have a hardcoded default in `safety.py:_DEFAULT_TIERS` and can be overridden per-tool in `config.yaml:safety.tool_tiers`. Valid values: `1`, `2`, `3`, or `"agent"`. Only `run_shell` uses `"agent"` — the agent must supply `agent_proposed_tier` and `agent_reasoning` in every call, subject to pattern guards.

### Rollback

`docker_stack_deploy` snapshots the current image tags for all services in the stack into `rollback_state.json` before deploying. `docker_stack_rollback` reads this snapshot and issues `docker service update --image` for each service.

### Adding a new tool

1. Add the tool schema to `TOOL_DEFINITIONS` in `agent/tools.py`
2. Add an `async def _tool_<name>(self, inp: dict) -> str` method to `ToolExecutor`
3. Add a default tier to `_DEFAULT_TIERS` in `agent/safety.py`
4. Optionally add an explicit `tool_tiers` entry in `config.yaml`
