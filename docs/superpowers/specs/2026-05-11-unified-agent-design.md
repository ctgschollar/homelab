# Unified Agent: LLM Backend Abstraction + Tool Hints

**Date:** 2026-05-11

## Overview

Two related improvements to the homelab agent:

1. **LLM Backend Abstraction** — merge `agent/` (Anthropic) and `openai-agent/` (Ollama) into a single codebase. Provider is selected via `config.yaml` and can be switched live via the `model use` Slack command.
2. **Tool Hints** — a directory-based system for injecting contextual recovery guidance into tool results when known error patterns are detected.

---

## Feature 1: LLM Backend Abstraction

### Problem

`agent/` and `openai-agent/` are near-identical codebases maintained separately. They will diverge over time. The only meaningful differences are in the API call (`_api_create`) and how the response is parsed and serialised into conversation history.

### Solution

Extract all provider-specific logic into a `LLMBackend` abstraction. `HomelabAgent` talks only to the abstraction. `openai-agent/` is deleted.

### New file: `agent/agent/llm.py`

#### Dataclasses

```python
@dataclass
class ToolCall:
    id: str
    name: str
    input: dict

@dataclass
class LLMResponse:
    text: str                       # text content from assistant (may be empty)
    tool_calls: list[ToolCall]      # normalised tool calls (empty = none)
    stop: bool                      # True = end of turn, no tool calls
    input_tokens: int
    output_tokens: int
    assistant_history_entry: dict   # ready-to-append history entry
    cache_write_tokens: int = 0     # Anthropic only
    cache_read_tokens: int = 0      # Anthropic only
```

#### `LLMBackend` ABC

```python
class LLMBackend(ABC):
    @abstractmethod
    async def chat(
        self,
        system: str,
        history: list[dict],
        tool_defs: list[dict],
    ) -> LLMResponse: ...

    @abstractmethod
    def format_tool_results(
        self,
        results: list[tuple[str, str]],  # (tool_id, content)
    ) -> list[dict]: ...
    # Anthropic: one {"role":"user","content":[tool_result blocks]} entry
    # Ollama:    one {"role":"tool","content":result} entry per result

    @abstractmethod
    def serialize_message(self, msg: dict) -> dict: ...
    # Anthropic: calls .model_dump() on content block objects
    # Ollama:    pass-through (already plain dicts)

    @abstractmethod
    def is_orphaned_tool_result(self, msg: dict) -> bool: ...
    # Anthropic: role==user and content is list with type==tool_result
    # Ollama:    role==tool

    @abstractmethod
    def has_incomplete_tool_calls(self, msg: dict, following: list[dict]) -> bool: ...
    # Anthropic: assistant msg has tool_use blocks but not all have matching tool_results
    # Ollama:    assistant msg has tool_calls but not all followed by role:tool msgs
```

#### Factory

```python
def create_backend(config: LlmConfig) -> LLMBackend:
    if config.provider == "anthropic":
        return AnthropicBackend(config)
    if config.provider == "ollama":
        return OllamaBackend(config)
    raise ValueError(f"Unknown provider: {config.provider}")
```

#### `AnthropicBackend`

- Wraps `anthropic.AsyncAnthropic`
- Applies prompt caching headers (`anthropic-beta: prompt-caching-2024-07-31`)
- Caches system prompt and last tool definition via `cache_control: ephemeral`
- Retries on HTTP 529 (overloaded) with exponential backoff
- Tool definitions: Anthropic format (`input_schema`) — canonical, no transformation needed

#### `OllamaBackend`

- Wraps `ollama.AsyncClient`
- Passes `think=False`, `stream=False`, `options={"num_ctx": 16384}`
- Retries on `ollama.ResponseError` with exponential backoff
- Tool definitions: transforms Anthropic `input_schema` format to Ollama `function.parameters` format at call time

### Changes to `agent/agent/agent.py`

`HomelabAgent.__init__` creates `self._backend = create_backend(config.llm)`.

`_run_loop` becomes provider-agnostic:

```python
for iteration in range(MAX_ITERATIONS):
    response = await self._backend.chat(self._system_prompt, self._history, TOOL_DEFINITIONS)
    self._history.append(response.assistant_history_entry)
    self._trim_history()

    # print text, notify Slack...

    if response.stop:
        break

    results = await self._handle_tool_calls(response.tool_calls, trigger)
    for msg in self._backend.format_tool_results(results):
        self._history.append(msg)
    self._trim_history()
```

`_handle_tool_calls` accepts `list[ToolCall]` instead of provider-specific block types. Returns `list[tuple[str, str]]` (tool_id, content).

`_save_history` uses `self._backend.serialize_message(msg)` for each entry.

`_trim_history` uses `self._backend.is_orphaned_tool_result(msg)` and `self._backend.has_incomplete_tool_calls(msg, following)`.

New method `switch_backend(entry: ModelEntry) -> None` — reinitialises `self._backend`, updates `self._model` and cost-per-token fields. Called by `controller.py` after `model use`.

### Config schema changes (`agent/agent/config_schema.py`)

`AnthropicConfig` and the optional `LlmConfig` are replaced by a single required `LlmConfig`:

```python
class ModelEntry(BaseModel):
    name: str
    provider: Literal["anthropic", "ollama"]
    base_url: str = ""
    api_key: str = ""
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0

class LlmConfig(BaseModel):
    provider: Literal["anthropic", "ollama"]
    model: str
    base_url: str = ""
    api_key: str = ""
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0
    available_models: list[ModelEntry] = []
```

`AgentConfig` gains `llm: LlmConfig` and loses `anthropic: AnthropicConfig` and the optional `llm: Optional[LlmConfig]`.

Env var mapping updated:
- `ANTHROPIC_API_KEY` → `llm.api_key` (when provider is anthropic)
- `AGENT_LLM_API_KEY` → `llm.api_key` (when provider is ollama)
- Both are checked; `ANTHROPIC_API_KEY` takes precedence.

### Config yaml (`agent/config.yaml`)

```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-5
  base_url: ""
  api_key: ""                     # overridden by ANTHROPIC_API_KEY env var
  input_cost_per_mtok: 3.0
  output_cost_per_mtok: 15.0
  available_models:
    - name: claude-sonnet-4-5
      provider: anthropic
      input_cost_per_mtok: 3.0
      output_cost_per_mtok: 15.0
    - name: qwen3.6:27b
      provider: ollama
      base_url: "http://192.168.88.144:11434"
      input_cost_per_mtok: 0.0
      output_cost_per_mtok: 0.0
```

### `controller.py` changes

- All `config.anthropic.model` references → `config.llm.model`
- All `config.llm.available_models` references updated to `ModelEntry` objects
- `_cmd_model_list` shows provider: `• qwen3.6:27b (ollama)`
- `_cmd_model_use` finds the `ModelEntry`, updates `config.llm` fields, persists, calls `agent.switch_backend(entry)`
- `_persist_active_model` writes to `llm.model` + `llm.provider` + `llm.base_url` etc.
- `_persist_available_models` writes `ModelEntry` dicts

### Deletion

`openai-agent/` is deleted entirely after the merge.

---

## Feature 2: Directory-based Tool Hints

### Problem

The agent has no built-in knowledge of known failure patterns and their recovery procedures. The incident RAG is the intended store for this, but it's unavailable when the service it's hosted on (postgres) is itself the failing service — which is exactly when the knowledge is most needed.

### Solution

A `HintEngine` that loads static hint files from a directory at startup. When a tool returns a result matching a known pattern, the hint is appended to the result before it enters conversation history, giving the agent targeted recovery instructions at the moment they're relevant.

### Directory structure

```
agent/
  hints/
    <tool_name>/
      <hint_name>.yaml
```

The directory name is the tool name. Each YAML file defines one hint. Multiple hints per tool are supported.

### Hint file format

```yaml
pattern: "VolumeDriver.Mount: PathIsDevice failed"
hint: |
  Likely cause: stale Linstor volume mount. Recovery procedure:
  1. Scale the service to 0: docker service scale <service_name>=0
  2. SSH to the node listed in the error
  3. Run lsblk and find the mountpoint containing the service's volume name
  4. Run: umount <path>
  5. Scale the service back up
```

- `pattern`: matched as a Python regex against the tool result string. A plain string with no regex metacharacters works as a literal substring match.
- `hint`: freeform text appended to the tool result. Can be multi-line.

Multiple hint files can match the same result — all matching hints are appended in filename order.

### New file: `agent/agent/hints.py`

```python
class HintEngine:
    def __init__(self, hints_dir: str) -> None:
        # Walk hints/<tool_name>/*.yaml, compile patterns
        # self._hints: dict[str, list[tuple[re.Pattern, str, str]]]
        #   tool_name -> [(pattern, hint_text, filename), ...]

    def enrich(self, tool_name: str, result: str) -> str:
        # Check result against patterns for tool_name
        # For each match, append: "\n\n[HINT: <filename>]\n<hint_text>"
        # Return enriched result (or original if no matches)
```

Loaded once at startup. Hint files are not watched for changes — agent restart required to pick up new hints.

### Integration point

In `HomelabAgent._handle_tool_calls`, after each `self._tools.execute()` call:

```python
result = await self._tools.execute(tc.name, tc.input)
result = self._hints.enrich(tc.name, result)   # <-- new line
```

This applies before the result is logged, returned to history, or shown on the terminal — so the agent sees the hint in context alongside the error.

### Config addition

```yaml
hints_dir: "./hints"   # optional; defaults to ./hints relative to working directory
```

`HintEngine` is a no-op if the directory does not exist (logs a debug warning, does not error).

### Initial hint file

`agent/hints/run_shell/linstor_stale_mount.yaml`:

```yaml
pattern: "VolumeDriver.Mount: PathIsDevice failed"
hint: |
  Likely cause: stale Linstor volume mount preventing service restart. Recovery procedure:
  1. Scale the service to 0 replicas: docker service scale <service_name>=0
  2. SSH to the node listed in the error output
  3. Run lsblk and identify the mountpoint containing the service's volume name
     (look for a drbd* device mounted under the Docker plugins path)
  4. Unmount it: umount <full_mountpoint_path>
  5. Scale the service back up: docker service scale <service_name>=1
```

---

## File Change Summary

| File | Action |
|------|--------|
| `agent/agent/llm.py` | **New** — `ToolCall`, `LLMResponse`, `LLMBackend`, `AnthropicBackend`, `OllamaBackend`, `create_backend` |
| `agent/agent/hints.py` | **New** — `HintEngine` |
| `agent/hints/run_shell/linstor_stale_mount.yaml` | **New** — first hint |
| `agent/agent/config_schema.py` | **Modified** — unified `LlmConfig`, `ModelEntry`, add `hints_dir` |
| `agent/agent/agent.py` | **Modified** — use backend abstraction, apply hints |
| `agent/controller.py` | **Modified** — `config.llm.*` references, `ModelEntry` in model commands, `agent.switch_backend()` |
| `agent/config.yaml` | **Modified** — new `llm:` section replacing `anthropic:` + `llm:` |
| `agent/cli.py` | **Modified** — init backend via `create_backend`, init `HintEngine` |
| `agent/config_cli.py` | **Modified** — model key references updated |
| `agent/agent/tools.py` | **No change** — Anthropic format stays canonical |
| `agent/agent_base.py` | **No change** |
| `openai-agent/` | **Deleted** |

## Out of Scope

- Supporting providers other than Anthropic and Ollama
- Hot-reloading hint files without restart
- Per-hint enable/disable flags
- History format migration (existing `agent_history.json` files may need manual deletion if switching providers)
