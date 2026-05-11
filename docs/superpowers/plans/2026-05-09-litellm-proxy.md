# LiteLLM Proxy + Model Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy LiteLLM and Open WebUI as Docker Swarm stacks, and add a `model` Slack command to the homelab agent for runtime model switching between Anthropic Claude and local Ollama models.

**Architecture:** LiteLLM runs as a Swarm stack at `litellm.schollar.dev`, acting as an Anthropic-compatible proxy that routes to either Ollama (on the LLM laptop at `192.168.88.144`) or Anthropic directly. The homelab agent's `config.yaml` gains an `llm` section with `base_url` and `available_models`; `controller.py` gains `model` Slack commands. Open WebUI runs as a separate Swarm stack at `chat.schollar.dev`.

**Tech Stack:** Docker Swarm, Traefik, LiteLLM (`ghcr.io/berriai/litellm:main-latest`), Open WebUI (`ghcr.io/open-webui/open-webui:main`), Pydantic v2, Python asyncio, PyYAML

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `litellm/docker-compose.yaml` | Create | LiteLLM Swarm stack |
| `litellm/litellm_config.yaml` | Create | LiteLLM model routing config |
| `open-webui/docker-compose.yaml` | Create | Open WebUI Swarm stack |
| `agent/agent/config_schema.py` | Modify | Add `LlmConfig` and optional `llm` field |
| `agent/agent/agent.py` | Modify | Pass `base_url` to Anthropic client |
| `agent/controller.py` | Modify | Add `model` Slack commands |
| `agent/config.yaml` | Modify | Add `llm` section |
| `agent/tests/test_controller.py` | Modify | Add tests for `model` commands |

---

## Task 1: LiteLLM Swarm Stack

**Files:**
- Create: `litellm/docker-compose.yaml`
- Create: `litellm/litellm_config.yaml`

- [ ] **Step 1: Create `litellm/litellm_config.yaml`**

```yaml
model_list:
  - model_name: "*"
    litellm_params:
      model: "ollama_chat/*"
      api_base: "http://192.168.88.144:11434"
```

- [ ] **Step 2: Create `litellm/docker-compose.yaml`**

```yaml
networks:
  traefik-net:
    external: true

volumes:
  litellm_config:
    driver: linbit/linstor-docker-volume
    driver_opts:
      size: "1G"
      fs: "xfs"
      replicas: "2"
      storagepool: "pool_ssd"

services:
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    command: ["--config", "/app/config/litellm_config.yaml", "--port", "4000"]
    env_file: /mnt/cephfs-configs/litellm/.env
    environment:
      LITELLM_LOG: "ERROR"
    volumes:
      - litellm_config:/app/config
    networks: [traefik-net]
    deploy:
      mode: replicated
      replicas: 1
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 0
      placement:
        constraints:
          - "node.labels.linstor==true"
      update_config:
        order: start-first
        parallelism: 1
        failure_action: rollback
      labels:
        traefik.enable: "true"
        traefik.docker.network: traefik-net
        traefik.http.routers.litellm.rule: "Host(`litellm.schollar.dev`)"
        traefik.http.routers.litellm.entrypoints: websecure
        traefik.http.routers.litellm.tls.certresolver: cf
        traefik.http.services.litellm.loadbalancer.server.port: "4000"
        prometheus.blackbox: "true"
        metrics.probe_url: "https://litellm.schollar.dev"
```

Note: `litellm_config.yaml` needs to be copied to the Linstor volume before the first deploy. After deploying the stack once (the container will fail without the config), copy it in:
```bash
docker run --rm -v litellm_litellm_config:/app/config -v $(pwd)/litellm:/src alpine cp /src/litellm_config.yaml /app/config/
```
Then redeploy.

- [ ] **Step 3: Commit**

```bash
git add litellm/
git commit -m "feat: add LiteLLM Swarm stack at litellm.schollar.dev"
```

---

## Task 2: Open WebUI Swarm Stack

**Files:**
- Create: `open-webui/docker-compose.yaml`

- [ ] **Step 1: Create `open-webui/docker-compose.yaml`**

```yaml
networks:
  traefik-net:
    external: true

volumes:
  open_webui_data:
    driver: linbit/linstor-docker-volume
    driver_opts:
      size: "20G"
      fs: "xfs"
      replicas: "2"
      storagepool: "pool_hdd"

services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    env_file: /mnt/cephfs-configs/open-webui/.env
    environment:
      OLLAMA_BASE_URL: "http://192.168.88.144:11434"
    volumes:
      - open_webui_data:/app/backend/data
    networks: [traefik-net]
    deploy:
      mode: replicated
      replicas: 1
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 0
      placement:
        constraints:
          - "node.labels.linstor==true"
      update_config:
        order: start-first
        parallelism: 1
        failure_action: rollback
      labels:
        traefik.enable: "true"
        traefik.docker.network: traefik-net
        traefik.http.routers.open-webui.rule: "Host(`chat.schollar.dev`)"
        traefik.http.routers.open-webui.entrypoints: websecure
        traefik.http.routers.open-webui.tls.certresolver: cf
        traefik.http.services.open-webui.loadbalancer.server.port: "8080"
        prometheus.blackbox: "true"
        metrics.probe_url: "https://chat.schollar.dev"
```

- [ ] **Step 2: Commit**

```bash
git add open-webui/
git commit -m "feat: add Open WebUI Swarm stack at chat.schollar.dev"
```

---

## Task 3: Agent Config Schema — Add `LlmConfig`

**Files:**
- Modify: `agent/agent/config_schema.py`

- [ ] **Step 1: Add `LlmConfig` class and optional `llm` field**

In `agent/agent/config_schema.py`, add after the `AnthropicConfig` class (after line 20):

```python
class LlmConfig(BaseModel):
    base_url: str
    available_models: list[str] = []
```

In `AgentConfig`, add the optional field after `anthropic`:

```python
llm: Optional[LlmConfig] = None
```

The full `AgentConfig` fields in order should be:
```python
anthropic: AnthropicConfig
llm: Optional[LlmConfig] = None
slack: SlackConfig
# ... rest unchanged
```

- [ ] **Step 2: Run existing tests to confirm no regressions**

```bash
cd agent && hatch run -e test pytest tests/test_config_schema.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add agent/agent/config_schema.py
git commit -m "feat: add LlmConfig to agent config schema"
```

---

## Task 4: Agent — Wire `base_url` into Anthropic Client

**Files:**
- Modify: `agent/agent/agent.py`

- [ ] **Step 1: Update `HomelabAgent.__init__` to pass `base_url`**

In `agent/agent/agent.py`, replace lines 381-382:

```python
self._model: str = config.anthropic.model
self._client = anthropic.AsyncAnthropic(api_key=config.anthropic.api_key or "")
```

With:

```python
self._model: str = config.anthropic.model
_client_kwargs: dict = {"api_key": config.anthropic.api_key or ""}
if config.llm and config.llm.base_url:
    _client_kwargs["base_url"] = config.llm.base_url
self._client = anthropic.AsyncAnthropic(**_client_kwargs)
```

- [ ] **Step 2: Run existing tests**

```bash
cd agent && hatch run -e test pytest -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add agent/agent/agent.py
git commit -m "feat: wire LiteLLM base_url into Anthropic client"
```

---

## Task 5: Controller — Add `model` Slack Commands

**Files:**
- Modify: `agent/controller.py`
- Modify: `agent/tests/test_controller.py`

- [ ] **Step 1: Write failing tests**

In `agent/tests/test_controller.py`, add after the existing imports and fixtures:

```python
# ---------------------------------------------------------------------------
# Model command tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_show_current(controller, tmp_path):
    """model command shows current active model."""
    response = await controller.handle_command("model")
    assert "claude-sonnet" in response or "Current model:" in response


@pytest.mark.asyncio
async def test_model_list(controller, tmp_path):
    """model list shows all available models."""
    response = await controller.handle_command("model list")
    assert "gemma2" in response or "No models" in response


@pytest.mark.asyncio
async def test_model_use_valid(controller, tmp_path):
    """model use <name> switches to a known model."""
    await controller.handle_command("model add test-model:7b")
    response = await controller.handle_command("model use test-model:7b")
    assert "test-model:7b" in response
    assert controller._config.anthropic.model == "test-model:7b"


@pytest.mark.asyncio
async def test_model_use_invalid(controller, tmp_path):
    """model use <name> rejects unknown model."""
    response = await controller.handle_command("model use nonexistent:99b")
    assert "not in available models" in response or "Unknown model" in response


@pytest.mark.asyncio
async def test_model_add(controller, tmp_path):
    """model add <name> adds to available list."""
    response = await controller.handle_command("model add llama3.1:8b")
    assert "llama3.1:8b" in response
    assert "llama3.1:8b" in controller._config.llm.available_models


@pytest.mark.asyncio
async def test_model_remove(controller, tmp_path):
    """model remove <name> removes from available list."""
    await controller.handle_command("model add llama3.1:8b")
    response = await controller.handle_command("model remove llama3.1:8b")
    assert "llama3.1:8b" in response
    assert "llama3.1:8b" not in controller._config.llm.available_models


@pytest.mark.asyncio
async def test_model_remove_active_rejected(controller, tmp_path):
    """model remove rejects removing the currently active model."""
    await controller.handle_command("model add mymodel:7b")
    await controller.handle_command("model use mymodel:7b")
    response = await controller.handle_command("model remove mymodel:7b")
    assert "active" in response.lower() or "cannot" in response.lower()
```

- [ ] **Step 2: Update the `make_controller` fixture in `test_controller.py` to include `LlmConfig`**

In `agent/tests/test_controller.py`, update the import line to add `LlmConfig`:

```python
from agent.config_schema import (
    AgentConfig, ControllerConfig, MonitorConfig, AnthropicConfig,
    SlackConfig, DockerConfig, SwarmConfig, AnsibleConfig,
    SafetyConfig, SafeModeResourcesConfig, ShellCommandGuardsConfig,
    ActionLogConfig, LlmConfig,
)
```

And in `make_controller`, add `llm=` to the `AgentConfig.model_construct(...)` call after `anthropic=`:

```python
llm=LlmConfig(
    base_url="http://litellm.test",
    available_models=["claude-sonnet-4-20250514", "ollama/gemma2:2b"],
),
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd agent && hatch run -e test pytest tests/test_controller.py -k "model" -v
```

Expected: FAIL with `AttributeError` or `Unknown command`.

- [ ] **Step 4: Update `_COMMANDS` in `controller.py`**

Replace:
```python
_COMMANDS = frozenset(["stop", "start", "queue", "mode monitor", "mode act"])
```

With:
```python
_COMMANDS = frozenset(["stop", "start", "queue", "mode monitor", "mode act", "model"])
```

Also update `is_command` to handle `model` subcommands:

```python
def is_command(self, text: str) -> bool:
    lower = text.lower().strip()
    return lower in _COMMANDS or lower.startswith("model ")
```

- [ ] **Step 5: Add model command handlers to `handle_command`**

In `handle_command`, add before the final `return f"Unknown command..."`:

```python
if lower == "model" or lower.startswith("model "):
    return await self._cmd_model(text.strip())
```

- [ ] **Step 6: Add `_cmd_model` and helpers**

Add these methods to `AgentController`, after `_cmd_mode`:

```python
async def _cmd_model(self, text: str) -> str:
    parts = text.split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if not sub:
        return f"Current model: `{self._config.anthropic.model}`"
    if sub == "list":
        return self._cmd_model_list()
    if sub == "use":
        return await self._cmd_model_use(arg)
    if sub == "add":
        return await self._cmd_model_add(arg)
    if sub == "remove":
        return await self._cmd_model_remove(arg)
    return f"Unknown model subcommand: `{sub}`. Try: `model`, `model list`, `model use <name>`, `model add <name>`, `model remove <name>`"

def _cmd_model_list(self) -> str:
    if not self._config.llm or not self._config.llm.available_models:
        return "No models configured. Use `model add <name>` to add one."
    current = self._config.anthropic.model
    lines = ["*Available models:*"]
    for m in self._config.llm.available_models:
        marker = " ← active" if m == current else ""
        lines.append(f"• `{m}`{marker}")
    return "\n".join(lines)

async def _cmd_model_use(self, name: str) -> str:
    if not name:
        return "Usage: `model use <name>`"
    available = self._config.llm.available_models if self._config.llm else []
    if name not in available:
        return f"`{name}` is not in available models. Use `model add {name}` first."
    self._config.anthropic.model = name
    self._persist_active_model(name)
    agent = self.agents.get("default")
    if agent is not None:
        agent._model = name  # type: ignore[attr-defined]
    return f"✅ Switched to `{name}`"

async def _cmd_model_add(self, name: str) -> str:
    if not name:
        return "Usage: `model add <name>`"
    if self._config.llm is None:
        return "No `llm` section in config. Add it manually first."
    if name not in self._config.llm.available_models:
        self._config.llm.available_models.append(name)
        self._persist_available_models(self._config.llm.available_models)
    return f"✅ Added `{name}` to available models."

async def _cmd_model_remove(self, name: str) -> str:
    if not name:
        return "Usage: `model remove <name>`"
    if self._config.llm is None or name not in self._config.llm.available_models:
        return f"`{name}` is not in available models."
    if name == self._config.anthropic.model:
        return f"Cannot remove `{name}` — it is the active model. Switch first with `model use <other>`."
    self._config.llm.available_models.remove(name)
    self._persist_available_models(self._config.llm.available_models)
    return f"✅ Removed `{name}` from available models."

def _persist_active_model(self, model: str) -> None:
    try:
        with open(self._config_path) as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("anthropic", {})["model"] = model
        with open(self._config_path, "w") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)
    except Exception as exc:
        console.print(f"[yellow]Warning: could not persist active model to config: {exc}[/yellow]")

def _persist_available_models(self, models: list[str]) -> None:
    try:
        with open(self._config_path) as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("llm", {})["available_models"] = models
        with open(self._config_path, "w") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)
    except Exception as exc:
        console.print(f"[yellow]Warning: could not persist available models to config: {exc}[/yellow]")
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd agent && hatch run -e test pytest tests/test_controller.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add agent/controller.py agent/tests/test_controller.py
git commit -m "feat: add model Slack commands to agent controller"
```

---

## Task 6: Agent `config.yaml` — Add `llm` Section

**Files:**
- Modify: `agent/config.yaml`

- [ ] **Step 1: Add `llm` section to `config.yaml`**

Add after the `anthropic` section:

```yaml
llm:
  base_url: "https://litellm.schollar.dev"
  available_models:
    - claude-sonnet-4-20250514
    - ollama/gemma2:2b
    - ollama/qwen2.5-coder:14b
    - ollama/qwen2.5-coder:32b
```

- [ ] **Step 2: Validate config loads correctly**

```bash
cd agent && hatch run config validate
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add agent/config.yaml
git commit -m "feat: add llm section to agent config with LiteLLM base_url and model list"
```

---

## Task 7: Push and verify

- [ ] **Step 1: Push all commits**

```bash
git push
```

- [ ] **Step 2: Verify test suite clean**

```bash
cd agent && hatch run -e test pytest -v
```

Expected: all tests pass, no failures.

- [ ] **Step 3: Check CLAUDE.md help text is still accurate**

The `agent/CLAUDE.md` doesn't document Slack commands so no update needed. The new commands are self-documenting via the `model` response.
