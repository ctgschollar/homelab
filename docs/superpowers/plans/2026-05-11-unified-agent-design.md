# Unified Agent: LLM Backend Abstraction + Tool Hints

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge `agent/` and `openai-agent/` into a single codebase with a provider-agnostic LLM backend abstraction, and add a directory-based hint system that injects recovery guidance into tool results.

**Architecture:** Extract all provider-specific logic into a `LLMBackend` ABC in `agent/agent/llm.py`; `HomelabAgent` calls `backend.chat()` and uses `backend.format_tool_results()` / `serialize_message()` / `is_orphaned_tool_result()` / `has_incomplete_tool_calls()`. A `HintEngine` in `agent/agent/hints.py` loads YAML hint files at startup and enriches tool results when patterns match.

**Tech Stack:** Python 3.12, `anthropic>=0.40.0`, `ollama>=0.3.0`, pydantic v2, pytest, hatch.

---

## File Map

| File | Action |
|------|--------|
| `agent/agent/llm.py` | **New** — `ToolCall`, `LLMResponse`, `LLMBackend` ABC, `AnthropicBackend`, `OllamaBackend`, `create_backend` |
| `agent/agent/hints.py` | **New** — `HintEngine` |
| `agent/hints/run_shell/linstor_stale_mount.yaml` | **New** — first hint |
| `agent/tests/test_hints.py` | **New** — HintEngine unit tests |
| `agent/tests/test_llm.py` | **New** — LLMBackend helper method tests |
| `agent/agent/config_schema.py` | **Modified** — unified `LlmConfig` + `ModelEntry`, remove `AnthropicConfig`, add `hints_dir` |
| `agent/agent/agent.py` | **Modified** — use backend abstraction and hints, add `switch_backend` |
| `agent/controller.py` | **Modified** — `config.llm.*` refs, `ModelEntry` handling, `switch_backend` call |
| `agent/config.yaml` | **Modified** — replace `anthropic:` + old `llm:` with unified `llm:` section |
| `agent/config_cli.py` | **Modified** — `llm.*` key refs, pricing uses `llm.` |
| `agent/cli.py` | **Modified** — add missing `import re` |
| `agent/pyproject.toml` | **Modified** — add `ollama>=0.3.0` dependency |
| `agent/tests/test_controller.py` | **Modified** — remove `AnthropicConfig`, update model test data |
| `openai-agent/` | **Deleted** |

---

## Task 1: Write tests for HintEngine

**Files:**
- Create: `agent/tests/test_hints.py`

- [ ] **Step 1: Write the failing tests**

```python
# agent/tests/test_hints.py
import pytest
import yaml
from pathlib import Path
from agent.hints import HintEngine


def write_hint(hints_dir: Path, tool: str, name: str, pattern: str, hint: str) -> None:
    tool_dir = hints_dir / tool
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / f"{name}.yaml").write_text(yaml.dump({"pattern": pattern, "hint": hint}))


def test_no_hints_dir_is_noop(tmp_path: Path) -> None:
    engine = HintEngine(str(tmp_path / "nonexistent"))
    assert engine.enrich("run_shell", "some error output") == "some error output"


def test_no_match_returns_original(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "test", "SPECIFIC_ERROR", "hint text")
    engine = HintEngine(str(tmp_path))
    assert engine.enrich("run_shell", "completely different output") == "completely different output"


def test_matching_hint_appended(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "test", "SPECIFIC_ERROR", "hint text")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "output with SPECIFIC_ERROR inside")
    assert "[HINT: test]" in result
    assert "hint text" in result


def test_original_text_preserved_before_hint(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "test", "ERR", "fix it")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "got ERR")
    assert result.startswith("got ERR")


def test_no_hints_for_other_tool(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "test", "ERR", "fix it")
    engine = HintEngine(str(tmp_path))
    assert engine.enrich("docker_service_list", "some output with ERR") == "some output with ERR"


def test_multiple_matching_hints_all_appended(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "hint_a", "ERROR_A", "fix A")
    write_hint(tmp_path, "run_shell", "hint_b", "ERROR_B", "fix B")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "output with ERROR_A and ERROR_B")
    assert "[HINT: hint_a]" in result
    assert "[HINT: hint_b]" in result


def test_hints_appended_in_filename_order(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "aaa", "MATCH", "first")
    write_hint(tmp_path, "run_shell", "zzz", "MATCH", "last")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "MATCH")
    assert result.index("[HINT: aaa]") < result.index("[HINT: zzz]")


def test_non_matching_hint_not_appended(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "aaa", "MATCH", "hint")
    write_hint(tmp_path, "run_shell", "bbb", "NO_MATCH", "other hint")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "only MATCH here")
    assert "[HINT: aaa]" in result
    assert "[HINT: bbb]" not in result


def test_plain_string_works_as_literal_match(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "linstor", "VolumeDriver.Mount: PathIsDevice failed", "fix it")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "error: VolumeDriver.Mount: PathIsDevice failed for path")
    assert "[HINT: linstor]" in result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd agent && hatch run -e test pytest tests/test_hints.py -v
```

Expected: `ModuleNotFoundError: No module named 'agent.hints'`

---

## Task 2: Implement HintEngine

**Files:**
- Create: `agent/agent/hints.py`

- [ ] **Step 1: Implement**

```python
# agent/agent/hints.py
import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger("homelab.hints")


class HintEngine:
    def __init__(self, hints_dir: str) -> None:
        self._hints: dict[str, list[tuple[re.Pattern, str, str]]] = {}
        hints_path = Path(hints_dir)
        if not hints_path.exists():
            logger.debug("Hints directory %r not found — no hints loaded", hints_dir)
            return
        for tool_dir in sorted(hints_path.iterdir()):
            if not tool_dir.is_dir():
                continue
            entries: list[tuple[re.Pattern, str, str]] = []
            for hint_file in sorted(tool_dir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(hint_file.read_text())
                    entries.append((re.compile(data["pattern"]), data["hint"], hint_file.stem))
                except Exception as exc:
                    logger.warning("Failed to load hint %s: %s", hint_file, exc)
            if entries:
                self._hints[tool_dir.name] = entries

    def enrich(self, tool_name: str, result: str) -> str:
        additions = [
            f"\n\n[HINT: {name}]\n{hint_text}"
            for pattern, hint_text, name in self._hints.get(tool_name, [])
            if pattern.search(result)
        ]
        return result + "".join(additions)
```

- [ ] **Step 2: Run tests**

```bash
cd agent && hatch run -e test pytest tests/test_hints.py -v
```

Expected: All 9 tests PASS.

- [ ] **Step 3: Commit**

```bash
cd agent && git add agent/hints.py tests/test_hints.py
git commit -m "feat: add HintEngine for directory-based tool result enrichment"
```

---

## Task 3: Create initial hint file

**Files:**
- Create: `agent/hints/run_shell/linstor_stale_mount.yaml`

- [ ] **Step 1: Create directories and file**

```bash
mkdir -p agent/hints/run_shell
```

```yaml
# agent/hints/run_shell/linstor_stale_mount.yaml
pattern: "VolumeDriver.Mount: PathIsDevice failed"
hint: |
  Likely cause: stale Linstor volume mount preventing service restart.

  The full error looks like:
    failed to populate volume: error while mounting volume
    '/var/lib/docker/plugins/<hash>/rootfs':
    VolumeDriver.Mount: PathIsDevice failed for path "": stat : no such file or directory

  Recovery procedure:
  1. Identify which node the service is pinned to (check docker service ps <service_name>)
  2. Scale the service to 0: docker service scale <service_name>=0
  3. SSH to the affected node
  4. Run lsblk and find the mountpoint for the stale volume — look for a drbd* device
     mounted under /var/lib/docker/plugins/<hash>/propagated-mount/<volume_name>
     Example output:
       drbd1000  147:1000  0  50G  0 disk /var/lib/docker/plugins/<hash>/propagated-mount/postgres_postgres_data
  5. Unmount it: umount /var/lib/docker/plugins/<hash>/propagated-mount/<volume_name>
  6. Scale the service back up: docker service scale <service_name>=1
```

- [ ] **Step 2: Commit**

```bash
cd agent && git add hints/
git commit -m "feat: add linstor stale mount recovery hint"
```

---

## Task 4: Write tests for LLMBackend helper methods

**Files:**
- Create: `agent/tests/test_llm.py`

- [ ] **Step 1: Write the failing tests**

```python
# agent/tests/test_llm.py
from unittest.mock import MagicMock
from agent.llm import AnthropicBackend, OllamaBackend


# --- format_tool_results ---

def test_anthropic_format_tool_results_single_entry() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    formatted = backend.format_tool_results([("id1", "result1"), ("id2", "result2")])
    assert len(formatted) == 1
    assert formatted[0]["role"] == "user"
    content = formatted[0]["content"]
    assert content[0] == {"type": "tool_result", "tool_use_id": "id1", "content": "result1"}
    assert content[1] == {"type": "tool_result", "tool_use_id": "id2", "content": "result2"}


def test_ollama_format_tool_results_one_per_result() -> None:
    backend = OllamaBackend.__new__(OllamaBackend)
    formatted = backend.format_tool_results([("0", "result1"), ("1", "result2")])
    assert len(formatted) == 2
    assert formatted[0] == {"role": "tool", "content": "result1"}
    assert formatted[1] == {"role": "tool", "content": "result2"}


# --- is_orphaned_tool_result ---

def test_anthropic_identifies_orphaned_tool_result() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    msg = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "id1", "content": "x"}]}
    assert backend.is_orphaned_tool_result(msg) is True


def test_anthropic_user_text_not_orphaned() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    assert backend.is_orphaned_tool_result({"role": "user", "content": "hello"}) is False


def test_anthropic_assistant_not_orphaned() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    msg = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert backend.is_orphaned_tool_result(msg) is False


def test_ollama_role_tool_is_orphaned() -> None:
    backend = OllamaBackend.__new__(OllamaBackend)
    assert backend.is_orphaned_tool_result({"role": "tool", "content": "result"}) is True


def test_ollama_role_user_not_orphaned() -> None:
    backend = OllamaBackend.__new__(OllamaBackend)
    assert backend.is_orphaned_tool_result({"role": "user", "content": "hello"}) is False


# --- has_incomplete_tool_calls ---

def test_anthropic_complete_tool_calls_returns_false() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    assistant = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tc1", "name": "fn", "input": {}}],
    }
    following = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc1", "content": "x"}]},
    ]
    assert backend.has_incomplete_tool_calls(assistant, following) is False


def test_anthropic_incomplete_tool_calls_returns_true() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    assistant = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tc1", "name": "fn", "input": {}}],
    }
    assert backend.has_incomplete_tool_calls(assistant, []) is True


def test_anthropic_no_tool_calls_returns_false() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    assistant = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert backend.has_incomplete_tool_calls(assistant, []) is False


def test_ollama_complete_tool_calls_returns_false() -> None:
    backend = OllamaBackend.__new__(OllamaBackend)
    assistant = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "fn"}}]}
    following = [{"role": "tool", "content": "result"}]
    assert backend.has_incomplete_tool_calls(assistant, following) is False


def test_ollama_incomplete_tool_calls_returns_true() -> None:
    backend = OllamaBackend.__new__(OllamaBackend)
    assistant = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "fn"}}]}
    assert backend.has_incomplete_tool_calls(assistant, []) is True


def test_ollama_no_tool_calls_returns_false() -> None:
    backend = OllamaBackend.__new__(OllamaBackend)
    assistant = {"role": "assistant", "content": "hello", "tool_calls": []}
    assert backend.has_incomplete_tool_calls(assistant, []) is False


# --- serialize_message ---

def test_anthropic_serialize_plain_string_content() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    msg = {"role": "user", "content": "hello"}
    assert backend.serialize_message(msg) == msg


def test_anthropic_serialize_calls_model_dump_on_objects() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    mock_block = MagicMock()
    mock_block.model_dump.return_value = {"type": "text", "text": "hi"}
    result = backend.serialize_message({"role": "assistant", "content": [mock_block]})
    mock_block.model_dump.assert_called_once()
    assert result["content"] == [{"type": "text", "text": "hi"}]


def test_anthropic_serialize_plain_dict_content_passthrough() -> None:
    backend = AnthropicBackend.__new__(AnthropicBackend)
    msg = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "y"}]}
    result = backend.serialize_message(msg)
    assert result["content"][0] == {"type": "tool_result", "tool_use_id": "x", "content": "y"}


def test_ollama_serialize_is_passthrough() -> None:
    backend = OllamaBackend.__new__(OllamaBackend)
    msg = {"role": "assistant", "content": "hello", "tool_calls": []}
    assert backend.serialize_message(msg) is msg
```

- [ ] **Step 2: Run to verify failure**

```bash
cd agent && hatch run -e test pytest tests/test_llm.py -v
```

Expected: `ModuleNotFoundError: No module named 'agent.llm'`

---

## Task 5: Add ollama dependency

**Files:**
- Modify: `agent/pyproject.toml`

- [ ] **Step 1: Add ollama to dependencies**

In `agent/pyproject.toml`, add `"ollama>=0.3.0",` after the `anthropic` line:

```toml
dependencies = [
    "anthropic>=0.40.0",
    "ollama>=0.3.0",
    "docker>=7.0.0",
    ...
]
```

- [ ] **Step 2: Reinstall in test env**

```bash
cd agent && hatch run -e test pip install -e .
```

Expected: Successfully installed with ollama package available.

---

## Task 6: Implement llm.py

**Files:**
- Create: `agent/agent/llm.py`

- [ ] **Step 1: Implement**

```python
# agent/agent/llm.py
import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anthropic
import ollama

if TYPE_CHECKING:
    from .config_schema import LlmConfig

logger = logging.getLogger("homelab.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop: bool
    input_tokens: int
    output_tokens: int
    assistant_history_entry: dict
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


class LLMBackend(ABC):
    @abstractmethod
    async def chat(
        self,
        system: str,
        history: list[dict],
        tool_defs: list[dict],
    ) -> LLMResponse: ...

    @abstractmethod
    def format_tool_results(self, results: list[tuple[str, str]]) -> list[dict]: ...

    @abstractmethod
    def serialize_message(self, msg: dict) -> dict: ...

    @abstractmethod
    def is_orphaned_tool_result(self, msg: dict) -> bool: ...

    @abstractmethod
    def has_incomplete_tool_calls(self, msg: dict, following: list[dict]) -> bool: ...


class AnthropicBackend(LLMBackend):
    def __init__(self, config: "LlmConfig") -> None:
        client_kwargs: dict = {"api_key": config.api_key or "no-key"}
        if config.base_url:
            client_kwargs["base_url"] = config.base_url
        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        self._model = config.model

    async def chat(
        self,
        system: str,
        history: list[dict],
        tool_defs: list[dict],
    ) -> LLMResponse:
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        tools = list(tool_defs)
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}

        logger.debug(
            "API REQUEST model=%s history_turns=%d",
            self._model,
            len(history),
        )

        delay = 5
        for attempt in range(5):
            try:
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=4096,
                    system=system_blocks,
                    messages=history,
                    tools=tools,
                    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                )
                logger.debug(
                    "API RESPONSE stop_reason=%s usage=%s",
                    response.stop_reason,
                    response.usage,
                )
                break
            except anthropic.APIStatusError as exc:
                if exc.status_code == 529 and attempt < 4:
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    raise

        text = "\n\n".join(b.text for b in response.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input or {})
            for b in response.content
            if b.type == "tool_use"
        ]
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop=response.stop_reason == "end_turn",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            assistant_history_entry={"role": "assistant", "content": response.content},
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )

    def format_tool_results(self, results: list[tuple[str, str]]) -> list[dict]:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": content}
                    for tid, content in results
                ],
            }
        ]

    def serialize_message(self, msg: dict) -> dict:
        content = msg.get("content", [])
        if isinstance(content, list):
            return {
                "role": msg["role"],
                "content": [
                    b.model_dump() if hasattr(b, "model_dump") else b
                    for b in content
                ],
            }
        return msg

    def is_orphaned_tool_result(self, msg: dict) -> bool:
        content = msg.get("content", [])
        return (
            msg.get("role") == "user"
            and isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        )

    def has_incomplete_tool_calls(self, msg: dict, following: list[dict]) -> bool:
        if msg.get("role") != "assistant":
            return False
        content = msg.get("content", [])
        if not isinstance(content, list):
            return False
        tool_use_ids: set[str] = set()
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tool_use_ids.add(b["id"])
            elif hasattr(b, "type") and b.type == "tool_use":
                tool_use_ids.add(b.id)
        if not tool_use_ids:
            return False
        result_ids: set[str] = set()
        for m in following:
            mc = m.get("content", [])
            if isinstance(mc, list):
                for b in mc:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        result_ids.add(b.get("tool_use_id", ""))
        return not tool_use_ids.issubset(result_ids)


class OllamaBackend(LLMBackend):
    def __init__(self, config: "LlmConfig") -> None:
        self._client = ollama.AsyncClient(host=config.base_url or "http://localhost:11434")
        self._model = config.model

    @staticmethod
    def _to_ollama_tool(tool: dict) -> dict:
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }

    async def chat(
        self,
        system: str,
        history: list[dict],
        tool_defs: list[dict],
    ) -> LLMResponse:
        messages = [{"role": "system", "content": system}] + history
        ollama_tools = [self._to_ollama_tool(t) for t in tool_defs]

        logger.debug("API REQUEST model=%s history_turns=%d", self._model, len(history))

        delay = 5
        for attempt in range(5):
            try:
                response = await self._client.chat(
                    model=self._model,
                    messages=messages,
                    tools=ollama_tools,
                    think=False,
                    stream=False,
                    options={"num_ctx": 16384},
                )
                logger.debug(
                    "API RESPONSE done_reason=%s tokens=(%d in, %d out)",
                    response.done_reason,
                    response.prompt_eval_count or 0,
                    response.eval_count or 0,
                )
                break
            except ollama.ResponseError as exc:
                logger.error("Ollama ResponseError: status=%s error=%r", exc.status_code, exc.error)
                if attempt < 4:
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    raise

        message = response.message
        text = message.content or ""
        tool_calls: list[ToolCall] = []
        assistant_entry: dict = {"role": "assistant", "content": text}

        if message.tool_calls:
            tool_calls = [
                ToolCall(id=str(i), name=tc.function.name, input=tc.function.arguments)
                for i, tc in enumerate(message.tool_calls)
            ]
            assistant_entry["tool_calls"] = [
                {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in message.tool_calls
            ]

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop=not bool(message.tool_calls),
            input_tokens=response.prompt_eval_count or 0,
            output_tokens=response.eval_count or 0,
            assistant_history_entry=assistant_entry,
        )

    def format_tool_results(self, results: list[tuple[str, str]]) -> list[dict]:
        return [{"role": "tool", "content": content} for _, content in results]

    def serialize_message(self, msg: dict) -> dict:
        return msg

    def is_orphaned_tool_result(self, msg: dict) -> bool:
        return msg.get("role") == "tool"

    def has_incomplete_tool_calls(self, msg: dict, following: list[dict]) -> bool:
        if msg.get("role") != "assistant":
            return False
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            return False
        n_responses = sum(1 for m in following[: len(tool_calls)] if m.get("role") == "tool")
        return n_responses < len(tool_calls)


def create_backend(config: "LlmConfig") -> LLMBackend:
    if config.provider == "anthropic":
        return AnthropicBackend(config)
    if config.provider == "ollama":
        return OllamaBackend(config)
    raise ValueError(f"Unknown LLM provider: {config.provider!r}")
```

- [ ] **Step 2: Run tests**

```bash
cd agent && hatch run -e test pytest tests/test_llm.py -v
```

Expected: All 16 tests PASS.

- [ ] **Step 3: Commit**

```bash
cd agent && git add agent/llm.py tests/test_llm.py pyproject.toml
git commit -m "feat: add LLMBackend abstraction with Anthropic and Ollama implementations"
```

---

## Task 7: Update config_schema.py

**Files:**
- Modify: `agent/agent/config_schema.py`

- [ ] **Step 1: Replace the file**

Replace `agent/agent/config_schema.py` with the following (replace `AnthropicConfig` + old `LlmConfig` with `ModelEntry` + new `LlmConfig`, add `hints_dir` to `AgentConfig`):

```python
"""Pydantic v2 config schema for the homelab agent."""
from __future__ import annotations

import os
import warnings
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic.fields import FieldInfo

TierValue = Literal[1, 2, 3, "agent"]


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


class SlackConfig(BaseModel):
    bot_token: Optional[str] = Field(default=None)
    signing_secret: Optional[str] = Field(default=None)
    channel: str
    veto_window_seconds: int = Field(gt=0, default=300)


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


class AnsibleConfig(BaseModel):
    repo_path: str
    inventory: str
    git_token: Optional[str] = Field(default=None)
    git_author_name: str
    git_author_email: str


class MonitorConfig(BaseModel):
    poll_interval: int
    grace_period_seconds: int = 600


class ControllerConfig(BaseModel):
    mode: Literal["monitor", "act"] = "monitor"
    whitelist_path: str = "./whitelist.json"


class SafeModeResourcesConfig(BaseModel):
    stacks: list[str] = []
    services: list[str] = []
    nodes: list[str] = []


class ShellCommandGuardsConfig(BaseModel):
    force_tier3: list[str] = []
    force_tier2: list[str] = []


class SafetyConfig(BaseModel):
    global_safe_mode: bool
    safe_mode_resources: SafeModeResourcesConfig
    tool_tiers: dict[str, TierValue]
    log_agent_tier_reasoning: bool
    shell_command_guards: ShellCommandGuardsConfig = Field(default_factory=ShellCommandGuardsConfig)


class RagConfig(BaseModel):
    dsn: Optional[str] = Field(default=None)
    database: str = "homelab_agent"
    log_rag_debug: bool = False


class ActionLogConfig(BaseModel):
    path: str


class ApprovalListenerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(ge=1024, le=65535, default=8765)


class HistoryConfig(BaseModel):
    path: str = "./agent_history.json"


class RollbackConfig(BaseModel):
    state_path: str = "./rollback_state.json"


class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type, yaml_path: str) -> None:
        super().__init__(settings_cls)
        self._path = yaml_path

    def get_field_value(self, field: FieldInfo, field_name: str) -> None:
        return None

    def field_is_complex(self, field: FieldInfo) -> bool:
        return True

    def __call__(self) -> dict:
        with open(self._path) as f:
            data = yaml.safe_load(f) or {}
        _env_map = {
            ("slack", "bot_token"): "SLACK_BOT_TOKEN",
            ("slack", "signing_secret"): "SLACK_SIGNING_SECRET",
            ("ansible", "git_token"): "AGENT_GITHUB_TOKEN",
            ("rag", "dsn"): "AGENT_POSTGRES_DSN",
        }
        for (section, field), env_var in _env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                data.setdefault(section, {})[field] = val
        # ANTHROPIC_API_KEY takes precedence over AGENT_LLM_API_KEY
        llm_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("AGENT_LLM_API_KEY")
        if llm_key:
            data.setdefault("llm", {})["api_key"] = llm_key
        return data


class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True)

    llm: LlmConfig
    hints_dir: str = "./hints"
    slack: SlackConfig
    docker: DockerConfig
    swarm: SwarmConfig
    edge: EdgeConfig = EdgeConfig()
    ansible: AnsibleConfig
    monitor: MonitorConfig
    controller: ControllerConfig = Field(default_factory=ControllerConfig)
    safety: SafetyConfig
    rag: RagConfig = Field(default_factory=RagConfig)
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


def load_agent_config(yaml_path: str) -> AgentConfig:
    class _Config(AgentConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type,
            **kwargs: object,
        ) -> tuple:
            return (
                YamlConfigSettingsSource(settings_cls, yaml_path),
                kwargs["init_settings"],
            )
    return _Config()
```

- [ ] **Step 2: Run existing tests to see which break**

```bash
cd agent && hatch run -e test pytest tests/ -v 2>&1 | head -60
```

Expected: `test_controller.py` will fail due to `AnthropicConfig` import. Safety/config tests pass.

---

## Task 8: Update config.yaml

**Files:**
- Modify: `agent/config.yaml`

- [ ] **Step 1: Replace the llm section**

Replace the existing `anthropic:` and `llm:` sections at the top of `agent/config.yaml` with:

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

hints_dir: "./hints"
```

Remove the old `anthropic:` block (lines 1-6) and old `llm:` block (lines 7-14).

- [ ] **Step 2: Validate config loads**

```bash
cd agent && python -c "from agent.config_schema import load_agent_config; c = load_agent_config('config.yaml'); print(c.llm.provider, c.llm.model)"
```

Expected: `anthropic claude-sonnet-4-5`

- [ ] **Step 3: Commit**

```bash
cd agent && git add agent/config_schema.py config.yaml
git commit -m "feat: unify LLM config into LlmConfig + ModelEntry, add hints_dir"
```

---

## Task 9: Update agent.py

**Files:**
- Modify: `agent/agent/agent.py`

- [ ] **Step 1: Update imports** (top of file, replace `import anthropic` and related)

Replace the import block at lines 1-25 with:

```python
import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

logger = logging.getLogger("homelab.agent")

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from rich.console import Console
from rich.text import Text

from .config_schema import AgentConfig, LlmConfig
from .hints import HintEngine
from .llm import LLMBackend, LLMResponse, ToolCall, create_backend
from .prompts import build_system_prompt
from .rag import IncidentRAG
from .safety import SafetyPolicy
from .slack import SlackClient
from .tools import TOOL_DEFINITIONS, ToolExecutor

if TYPE_CHECKING:
    from .config_schema import ModelEntry
```

- [ ] **Step 2: Update HomelabAgent.__init__**

Replace the `HomelabAgent.__init__` body (lines 382-415 in current file) with:

```python
def __init__(self, config: AgentConfig) -> None:
    self._config = config
    self._backend: LLMBackend = create_backend(config.llm)
    self._model: str = config.llm.model
    self._input_cost_per_mtok: float = config.llm.input_cost_per_mtok
    self._output_cost_per_mtok: float = config.llm.output_cost_per_mtok
    self._hints = HintEngine(getattr(config, "hints_dir", "./hints"))

    self._slack = SlackClient(
        bot_token=config.slack.bot_token,
        signing_secret=config.slack.signing_secret,
        channel=config.slack.channel,
    )
    self._veto_window: int = config.slack.veto_window_seconds

    self._logger = ActionLogger(config.action_log.path)
    self._safety = SafetyPolicy(config)
    self._rag: IncidentRAG | None = (
        IncidentRAG(config.rag) if config.rag.dsn else None
    )
    self._tools = ToolExecutor(config, self._slack, rag=self._rag)
    self._pending = PendingApprovals()

    self._history_path = Path(config.history.path)
    self._history: list[dict] = self._load_history()
    self._last_cost_breakdown: str = ""
    self._zar_rate: float | None = None
    self._zar_rate_fetched_at: datetime | None = None
    self._system_prompt = build_system_prompt()
    self._active_execution: dict | None = None
    self._active_task: asyncio.Task | None = None
```

- [ ] **Step 3: Replace _api_create with _run_loop using backend**

Delete the `_api_create` method entirely and replace the full `_run_loop` method with:

```python
async def _run_loop(self, trigger: str) -> tuple[str, float]:
    final_text = ""
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_write_tokens = 0
    total_cache_read_tokens = 0
    live_to_slack = not trigger.startswith("cli:")

    for iteration in range(MAX_ITERATIONS):
        response = await self._backend.chat(self._system_prompt, self._history, TOOL_DEFINITIONS)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens
        total_cache_write_tokens += response.cache_write_tokens
        total_cache_read_tokens += response.cache_read_tokens
        self._history.append(response.assistant_history_entry)
        self._trim_history()

        if response.text:
            final_text = response.text
            label = Text("Agent: ", style="bold cyan")
            console.print(label, end="")
            console.print(response.text)
            if live_to_slack and response.text.strip():
                if response.stop:
                    slack_text = f"✅ {response.text}"
                elif iteration == 0:
                    slack_text = f"📋 {response.text}"
                else:
                    slack_text = f"🔍 {response.text}"
                console.print(f"  [dim cyan]→ Slack notify ({len(slack_text)} chars)[/dim cyan]")
                try:
                    await self._slack.notify(slack_text)
                except Exception as exc:
                    console.print(f"  [yellow]Slack notify failed: {exc}[/yellow]")

        if response.stop:
            break

        try:
            results = await self._handle_tool_calls(response.tool_calls, trigger)
        except Exception as exc:
            results = [(tc.id, f"ERROR: {exc}") for tc in response.tool_calls]
        for msg in self._backend.format_tool_results(results):
            self._history.append(msg)
        self._trim_history()

    input_cost   = total_input_tokens       / 1_000_000 * self._input_cost_per_mtok
    write_cost   = total_cache_write_tokens  / 1_000_000 * self._input_cost_per_mtok * 1.25
    read_cost    = total_cache_read_tokens   / 1_000_000 * self._input_cost_per_mtok * 0.10
    output_cost  = total_output_tokens       / 1_000_000 * self._output_cost_per_mtok
    cost_usd     = input_cost + write_cost + read_cost + output_cost

    zar = await self._get_zar_rate()

    def fmt(usd: float, tokens: int) -> str:
        s = f"${usd:.5f}"
        if zar:
            s += f"/R{usd * zar:.4f}"
        return f"{s}({tokens:,})"

    parts = [f"in={fmt(input_cost, total_input_tokens)}"]
    if total_cache_write_tokens:
        parts.append(f"cW={fmt(write_cost, total_cache_write_tokens)}")
    if total_cache_read_tokens:
        parts.append(f"cR={fmt(read_cost, total_cache_read_tokens)}")
    parts.append(f"out={fmt(output_cost, total_output_tokens)}")
    total_str = f"${cost_usd:.5f}"
    if zar:
        total_str += f"/R{cost_usd * zar:.4f}"
    parts.append(f"total={total_str}")
    breakdown = "  " + "  ".join(parts)
    console.print(f"[dim]{breakdown}[/dim]")

    self._last_cost_breakdown = breakdown
    await self._logger.log_cost(cost_usd, total_input_tokens, total_output_tokens, trigger)
    self._save_history()
    return ("" if live_to_slack else final_text), cost_usd
```

- [ ] **Step 4: Update _handle_tool_calls to accept list[ToolCall] and return list[tuple[str, str]]**

Replace the full `_handle_tool_calls` method:

```python
async def _handle_tool_calls(
    self,
    tool_calls: list[ToolCall],
    trigger: str,
) -> list[tuple[str, str]]:
    tier1_calls: list[ToolCall] = []
    mutating_calls: list[ToolCall] = []
    resolved_map: dict[str, Any] = {}

    for tc in tool_calls:
        inp = tc.input
        agent_tier = inp.get("agent_proposed_tier")
        agent_reason = inp.get("agent_reasoning")
        target = self._infer_target_resource(tc.name, inp)

        resolved = self._safety.resolve_tier(
            tc.name,
            target,
            agent_tier,
            agent_reason,
            command=inp.get("command") if tc.name == "run_shell" else None,
        )
        resolved_map[tc.id] = resolved

        if agent_tier is not None and self._safety.log_agent_tier_reasoning:
            await self._logger.log_tier_reasoning(
                tool=tc.name,
                agent_proposed_tier=agent_tier,
                reasoning=agent_reason or "",
                safe_mode_active=resolved.safe_mode_active,
                effective_tier=resolved.tier,
                override_reason=resolved.override_reason,
                guard_matched_list=resolved.guard_matched_list,
                guard_matched_pattern=resolved.guard_matched_pattern,
            )

        if resolved.tier == 1:
            tier1_calls.append(tc)
        else:
            mutating_calls.append(tc)

    results: dict[str, str] = {}

    if tier1_calls:
        async def _exec_tier1(tc: ToolCall) -> tuple[str, str]:
            self._print_tool_call(tc, resolved_map[tc.id])
            res = await self._tools.execute(tc.name, tc.input)
            res = self._hints.enrich(tc.name, res)
            await self._logger.log_action_taken(
                tool=tc.name,
                tool_input=tc.input,
                outcome=res,
                tier=resolved_map[tc.id].tier,
                safe_mode_active=resolved_map[tc.id].safe_mode_active,
                trigger=trigger,
            )
            return tc.id, res

        gathered = await asyncio.gather(*[_exec_tier1(tc) for tc in tier1_calls])
        for tid, res in gathered:
            results[tid] = res

    for tc in mutating_calls:
        resolved = resolved_map[tc.id]
        self._print_tool_call(tc, resolved)
        res = await self._handle_approval_flow(tc, resolved, trigger)
        results[tc.id] = res

    for tc in tool_calls:
        res = results.get(tc.id, "ERROR: result missing")
        if res.startswith("ERROR:"):
            console.print(f"\n  [bold red]Tool error ({tc.name}):[/bold red] {res}")
            console.print("  [dim]Waiting for agent to report and ask for instructions...[/dim]\n")

    return [(tc.id, results.get(tc.id, "ERROR: result missing")) for tc in tool_calls]
```

- [ ] **Step 5: Update _handle_approval_flow to accept ToolCall**

Replace the `_handle_approval_flow` method signature and body (change `block: Any` → `tc: ToolCall`):

```python
async def _handle_approval_flow(
    self,
    tc: ToolCall,
    resolved: Any,
    trigger: str,
) -> str:
    plan_id = f"plan-{secrets.token_hex(4)}"
    tool_input = tc.input
    plan_text = self._format_plan(tc.name, tool_input)
    veto_seconds = self._veto_window if resolved.tier == 2 else None

    message_ref = await self._slack.notify_plan(
        plan_id,
        plan_text,
        veto_seconds,
        tool_name=tc.name,
        command=tool_input.get("command", ""),
    )
    await self._logger.log_plan_proposed(
        plan_id=plan_id,
        tool=tc.name,
        tool_input=tool_input,
        plan_text=plan_text,
        tier=resolved.tier,
        safe_mode_active=resolved.safe_mode_active,
        trigger=trigger,
    )

    console.print(f"\n  [bold yellow]Plan ID:[/bold yellow] {plan_id}")
    console.print(f"  [yellow]{plan_text}[/yellow]")
    if veto_seconds is not None:
        console.print(f"  Type [bold]y[/bold] to approve, [bold]n[/bold] to deny, or a message to cancel with context (auto-cancels in {veto_seconds}s)")
    else:
        console.print(f"  Type [bold]y[/bold] to approve, [bold]n[/bold] to deny, or a message to cancel with context")

    fut = self._pending.register(plan_id, tc.name, plan_text, resolved.tier)
    approved: bool
    reason: str

    try:
        if veto_seconds is not None:
            approved, reason = await asyncio.wait_for(asyncio.shield(fut), timeout=veto_seconds)
        else:
            approved, reason = await fut
    except asyncio.TimeoutError:
        approved = False
        reason = "timeout"
        self._pending.resolve(plan_id, False, "timeout")

    if not approved:
        await self._logger.log_plan_cancelled(plan_id, tc.name, reason)
        detail = f" — user said: {reason}" if reason and not reason.startswith("slack:") and reason != "timeout" else ""
        return f"[cancelled: {reason}{detail}]"

    await self._logger.log_plan_approved(plan_id, tc.name)
    self._active_execution = {
        "plan_id": plan_id,
        "tool": tc.name,
        "input": {k: v for k, v in tool_input.items() if k not in ("agent_proposed_tier", "agent_reasoning")},
        "started_at": datetime.now(timezone.utc),
    }
    try:
        result = await self._tools.execute(tc.name, tool_input)
        result = self._hints.enrich(tc.name, result)
    finally:
        self._active_execution = None
    await self._logger.log_action_taken(
        tool=tc.name,
        tool_input=tool_input,
        outcome=result,
        tier=resolved.tier,
        safe_mode_active=resolved.safe_mode_active,
        trigger=trigger,
    )
    if message_ref:
        await self._slack.update_plan_result(*message_ref, plan_id, plan_text, result)
    return result
```

- [ ] **Step 6: Update _print_tool_call to accept ToolCall**

Replace the `_print_tool_call` method:

```python
def _print_tool_call(self, tc: ToolCall, resolved: Any) -> None:
    params = ", ".join(
        f"{k}={v}" for k, v in tc.input.items()
        if k not in ("agent_proposed_tier", "agent_reasoning")
    )
    console.print(f"  [yellow]> {tc.name}({params})[/yellow]")
    if resolved.safe_mode_active:
        original = f"would have been tier {resolved.original_tier}" if resolved.original_tier is not None else "original tier unknown"
        console.print(f"  [bold yellow]  [SAFE MODE — tier forced to 3, {original}][/bold yellow]")
    if resolved.agent_reasoning:
        console.print(f"  [dim italic]  tier reasoning: {resolved.agent_reasoning}[/dim italic]")
```

- [ ] **Step 7: Update _save_history and _trim_history**

Replace `_save_history`:

```python
def _save_history(self) -> None:
    serialized = [self._backend.serialize_message(msg) for msg in self._history]
    self._history_path.write_text(json.dumps(serialized, indent=2))
```

Replace `_trim_history`:

```python
def _trim_history(self) -> None:
    """Keep at most MAX_HISTORY_TURNS turn-pairs, never leaving orphaned tool messages."""
    max_entries = MAX_HISTORY_TURNS * 2
    if len(self._history) > max_entries:
        self._history = self._history[-max_entries:]

    while self._history:
        first = self._history[0]
        if self._backend.is_orphaned_tool_result(first):
            self._history.pop(0)
            continue
        if self._backend.has_incomplete_tool_calls(first, self._history[1:]):
            self._history.pop(0)
            continue
        break
```

- [ ] **Step 8: Add switch_backend method** (add after `cancel_all`, before `aclose`)

```python
def switch_backend(self, entry: "ModelEntry") -> None:
    """Switch to a different LLM backend. Clears history (formats differ between providers)."""
    new_config = LlmConfig(
        provider=entry.provider,
        model=entry.name,
        base_url=entry.base_url,
        api_key=entry.api_key,
        input_cost_per_mtok=entry.input_cost_per_mtok,
        output_cost_per_mtok=entry.output_cost_per_mtok,
        available_models=self._config.llm.available_models,
    )
    self._backend = create_backend(new_config)
    self._model = entry.name
    self._input_cost_per_mtok = entry.input_cost_per_mtok
    self._output_cost_per_mtok = entry.output_cost_per_mtok
    self._history = []
    if self._history_path.exists():
        self._history_path.unlink()
```

- [ ] **Step 9: Commit**

```bash
cd agent && git add agent/agent.py
git commit -m "feat: refactor HomelabAgent to use LLMBackend abstraction and HintEngine"
```

---

## Task 10: Update controller.py

**Files:**
- Modify: `agent/controller.py`

- [ ] **Step 1: Add ModelEntry import**

After the existing imports, add:

```python
from agent.agent.config_schema import ModelEntry
```

- [ ] **Step 2: Update _cmd_model to show provider**

Replace the body of `_cmd_model` where `not sub`:

```python
if not sub:
    return f"Current model: `{self._config.llm.model}` ({self._config.llm.provider})"
```

- [ ] **Step 3: Replace _cmd_model_list to show provider**

```python
def _cmd_model_list(self) -> str:
    available = self._config.llm.available_models
    if not available:
        return "No models configured. Use `model add <name>` to add one."
    current = self._config.llm.model
    lines = ["*Available models:*"]
    for m in available:
        marker = " ← active" if m.name == current else ""
        lines.append(f"• `{m.name}` ({m.provider}){marker}")
    return "\n".join(lines)
```

- [ ] **Step 4: Replace _cmd_model_use to find ModelEntry and call switch_backend**

```python
async def _cmd_model_use(self, name: str) -> str:
    if not name:
        return "Usage: `model use <name>`"
    entry = next((m for m in self._config.llm.available_models if m.name == name), None)
    if entry is None:
        return f"`{name}` is not in available models. Use `model add {name}` first."
    self._config.llm.model = entry.name
    self._config.llm.provider = entry.provider
    self._config.llm.base_url = entry.base_url
    self._config.llm.api_key = entry.api_key
    self._config.llm.input_cost_per_mtok = entry.input_cost_per_mtok
    self._config.llm.output_cost_per_mtok = entry.output_cost_per_mtok
    self._persist_active_model(entry)
    agent = self.agents.get("default")
    if agent is not None and hasattr(agent, "switch_backend"):
        agent.switch_backend(entry)  # type: ignore[attr-defined]
    return f"✅ Switched to `{name}` ({entry.provider})"
```

- [ ] **Step 5: Replace _cmd_model_add to build a ModelEntry**

```python
async def _cmd_model_add(self, name: str) -> str:
    if not name:
        return "Usage: `model add <name>`"
    if any(m.name == name for m in self._config.llm.available_models):
        return f"✅ `{name}` already in available models."
    self._config.llm.available_models.append(ModelEntry(name=name, provider="anthropic"))
    self._persist_available_models(self._config.llm.available_models)
    return f"✅ Added `{name}` (anthropic) to available models."
```

- [ ] **Step 6: Replace _cmd_model_remove**

```python
async def _cmd_model_remove(self, name: str) -> str:
    if not name:
        return "Usage: `model remove <name>`"
    entry = next((m for m in self._config.llm.available_models if m.name == name), None)
    if entry is None:
        return f"`{name}` is not in available models."
    if name == self._config.llm.model:
        return f"Cannot remove `{name}` — it is the active model. Switch first with `model use <other>`."
    self._config.llm.available_models.remove(entry)
    self._persist_available_models(self._config.llm.available_models)
    return f"✅ Removed `{name}` from available models."
```

- [ ] **Step 7: Replace _persist_active_model to write all llm fields**

```python
def _persist_active_model(self, entry: ModelEntry) -> None:
    try:
        with open(self._config_path) as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("llm", {}).update({
            "model": entry.name,
            "provider": entry.provider,
            "base_url": entry.base_url,
            "api_key": entry.api_key,
            "input_cost_per_mtok": entry.input_cost_per_mtok,
            "output_cost_per_mtok": entry.output_cost_per_mtok,
        })
        with open(self._config_path, "w") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)
    except Exception as exc:
        console.print(f"[yellow]Warning: could not persist active model to config: {exc}[/yellow]")
```

- [ ] **Step 8: Replace _persist_available_models to serialize ModelEntry**

```python
def _persist_available_models(self, models: list[ModelEntry]) -> None:
    try:
        with open(self._config_path) as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("llm", {})["available_models"] = [m.model_dump() for m in models]
        with open(self._config_path, "w") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)
    except Exception as exc:
        console.print(f"[yellow]Warning: could not persist available models to config: {exc}[/yellow]")
```

- [ ] **Step 9: Commit**

```bash
cd agent && git add controller.py
git commit -m "feat: update controller for unified LlmConfig with ModelEntry"
```

---

## Task 11: Update config_cli.py

**Files:**
- Modify: `agent/config_cli.py`

- [ ] **Step 1: Update cmd_set to use llm.model instead of anthropic.model**

In `cmd_set`, replace the `anthropic.model` special case:

```python
if key_path == "llm.model" and isinstance(value, str):
    pricing = MODEL_PRICING.get(value)
    if pricing:
        data["llm"]["input_cost_per_mtok"] = pricing[0]
        data["llm"]["output_cost_per_mtok"] = pricing[1]
        print(f"  input_cost_per_mtok  → {pricing[0]}")
        print(f"  output_cost_per_mtok → {pricing[1]}")
    else:
        print(f"  WARNING: no pricing known for {value!r} — update MODEL_PRICING in config_cli.py")
```

(Remove the old `if key_path == "anthropic.model" ...` block.)

- [ ] **Step 2: Update cmd_pricing to use llm.* keys**

Replace the `cmd_pricing` function body:

```python
def cmd_pricing(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: config_cli.py pricing <input_per_mtok> <output_per_mtok>")
        print("  e.g. config_cli.py pricing 3.0 15.0")
        sys.exit(1)
    try:
        input_cost = float(args[0])
        output_cost = float(args[1])
    except ValueError:
        print("ERROR: costs must be numbers (USD per million tokens)")
        sys.exit(1)
    data = _load_raw()
    old_in = data.get("llm", {}).get("input_cost_per_mtok", "unset")
    old_out = data.get("llm", {}).get("output_cost_per_mtok", "unset")
    data.setdefault("llm", {})["input_cost_per_mtok"] = input_cost
    data["llm"]["output_cost_per_mtok"] = output_cost
    _save_raw(data)
    print(f"  input_cost_per_mtok:  {old_in!r} → {input_cost}")
    print(f"  output_cost_per_mtok: {old_out!r} → {output_cost}")
```

- [ ] **Step 3: Commit**

```bash
cd agent && git add config_cli.py
git commit -m "feat: update config_cli to use llm.* keys"
```

---

## Task 12: Fix missing import re in cli.py

**Files:**
- Modify: `agent/cli.py`

- [ ] **Step 1: Add missing import**

Add `import re` after `import logging` in `agent/cli.py`:

```python
import logging
import re
import sys
```

- [ ] **Step 2: Commit**

```bash
cd agent && git add cli.py
git commit -m "fix: add missing import re in cli.py"
```

---

## Task 13: Update test_controller.py

**Files:**
- Modify: `agent/tests/test_controller.py`

- [ ] **Step 1: Update make_controller to use new schema**

Replace the `make_controller` function's imports and config construction:

```python
def make_controller(mode: str = "act", grace_period: int = 10, tmp_path: Path | None = None):
    """Build an AgentController with mocked dependencies."""
    from controller import AgentController
    from agent.config_schema import (
        AgentConfig, ControllerConfig, MonitorConfig, ModelEntry, LlmConfig,
        SlackConfig, DockerConfig, SwarmConfig, AnsibleConfig,
        SafetyConfig, SafeModeResourcesConfig, ShellCommandGuardsConfig,
        ActionLogConfig,
    )
    config = AgentConfig.model_construct(
        controller=ControllerConfig(
            mode=mode,
            whitelist_path=str(tmp_path / "whitelist.json") if tmp_path else "/tmp/whitelist_test.json",
        ),
        monitor=MonitorConfig(poll_interval=30, grace_period_seconds=grace_period),
        llm=LlmConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            input_cost_per_mtok=3.0,
            output_cost_per_mtok=15.0,
            available_models=[
                ModelEntry(name="claude-sonnet-4-20250514", provider="anthropic",
                           input_cost_per_mtok=3.0, output_cost_per_mtok=15.0),
                ModelEntry(name="qwen3.6:27b", provider="ollama",
                           base_url="http://192.168.88.144:11434"),
            ],
        ),
        slack=SlackConfig(channel="#test"),
        docker=DockerConfig(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig(nodes=[], ssh_key="/tmp/key", ssh_user="root"),
        ansible=AnsibleConfig(repo_path="/tmp", inventory="/tmp/inv.yml", git_author_name="Test", git_author_email="test@test.com"),
        safety=SafetyConfig(
            global_safe_mode=False,
            safe_mode_resources=SafeModeResourcesConfig(),
            tool_tiers={"run_shell": "agent"},
            log_agent_tier_reasoning=False,
            shell_command_guards=ShellCommandGuardsConfig(),
        ),
        action_log=ActionLogConfig(path="/tmp/action.log"),
    )
    agent = AsyncMock()
    agent.handle_event = AsyncMock(return_value=("", 0.0))
    agent.chat = AsyncMock(return_value=("", 0.0))
    agent.cancel_all = AsyncMock()
    agent._slack = AsyncMock()

    slack = AsyncMock()
    slack.configured = True
    slack.notify = AsyncMock(return_value={"ok": True})
    slack.notify_deferred_alert = AsyncMock(return_value=("#ch", "12345.0"))

    return AgentController(
        config=config,
        agents={"default": agent},
        slack=slack,
        config_path="/tmp/test_config.yaml",
    ), agent, slack
```

- [ ] **Step 2: Update model command tests**

Replace the model command test section (lines 273–352) with:

```python
# ---------------------------------------------------------------------------
# Model command tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_show_current(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model")
    assert "claude-sonnet-4-20250514" in result
    assert "anthropic" in result


@pytest.mark.asyncio
async def test_model_list(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model list")
    assert "qwen3.6" in result
    assert "ollama" in result
    assert "claude-sonnet" in result
    assert "anthropic" in result


@pytest.mark.asyncio
async def test_model_list_marks_active(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model list")
    assert "← active" in result


@pytest.mark.asyncio
async def test_model_use_valid(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model use qwen3.6:27b")
    assert "qwen3.6:27b" in result
    assert controller._config.llm.model == "qwen3.6:27b"
    assert controller._config.llm.provider == "ollama"
    agent.switch_backend.assert_called_once()


@pytest.mark.asyncio
async def test_model_use_invalid(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model use nonexistent:99b")
    assert "not in available models" in result
    assert controller._config.llm.model == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_model_add(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model add llama3.1:8b")
    assert "llama3.1:8b" in result
    assert any(m.name == "llama3.1:8b" for m in controller._config.llm.available_models)


@pytest.mark.asyncio
async def test_model_add_idempotent(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    await controller.handle_command("model add llama3.1:8b")
    await controller.handle_command("model add llama3.1:8b")
    count = sum(1 for m in controller._config.llm.available_models if m.name == "llama3.1:8b")
    assert count == 1


@pytest.mark.asyncio
async def test_model_remove(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model remove qwen3.6:27b")
    assert "qwen3.6:27b" in result
    assert not any(m.name == "qwen3.6:27b" for m in controller._config.llm.available_models)


@pytest.mark.asyncio
async def test_model_remove_active_rejected(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model remove claude-sonnet-4-20250514")
    assert "active model" in result.lower() or "cannot" in result.lower()
    assert any(m.name == "claude-sonnet-4-20250514" for m in controller._config.llm.available_models)


@pytest.mark.asyncio
async def test_model_is_command(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    assert controller.is_command("model") is True
    assert controller.is_command("model list") is True
    assert controller.is_command("model use foo:7b") is True
    assert controller.is_command("model add foo:7b") is True
    assert controller.is_command("model remove foo:7b") is True
```

- [ ] **Step 3: Run the full test suite**

```bash
cd agent && hatch run -e test pytest tests/ -v
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
cd agent && git add tests/test_controller.py
git commit -m "test: update controller tests for unified LlmConfig schema"
```

---

## Task 14: Delete openai-agent/

- [ ] **Step 1: Remove the directory**

```bash
cd /home/claude/src/homelab
git rm -r openai-agent/
```

- [ ] **Step 2: Run final test suite**

```bash
cd agent && hatch run -e test pytest tests/ -v
```

Expected: All tests pass, no references to openai-agent.

- [ ] **Step 3: Final commit**

```bash
cd /home/claude/src/homelab
git commit -m "feat: delete openai-agent/ — functionality merged into agent/ via LLMBackend"
```

---

## Self-Review Checklist

Spec requirements vs plan tasks:

| Spec requirement | Task |
|---|---|
| `LLMBackend` ABC with `chat`, `format_tool_results`, `serialize_message`, `is_orphaned_tool_result`, `has_incomplete_tool_calls` | Task 6 |
| `AnthropicBackend` with caching, 529 retry | Task 6 |
| `OllamaBackend` with `think=False`, num_ctx, tool format transform | Task 6 |
| `create_backend` factory | Task 6 |
| `HintEngine` loading `hints/<tool>/*.yaml` | Task 2 |
| `HintEngine.enrich` appends matching hints | Task 2 |
| `hints_dir` config field | Task 7 |
| `ModelEntry` with name/provider/base_url/api_key/costs | Task 7 |
| `LlmConfig` unified (replaces AnthropicConfig + old LlmConfig) | Task 7 |
| env var `ANTHROPIC_API_KEY` → `llm.api_key` (takes precedence) | Task 7 |
| `HomelabAgent` uses backend abstraction | Task 9 |
| `HomelabAgent.switch_backend` clears history | Task 9 |
| Hints applied in `_handle_tool_calls` and `_handle_approval_flow` | Task 9 |
| `_trim_history` uses `is_orphaned_tool_result` + `has_incomplete_tool_calls` | Task 9 |
| `controller.py` uses `config.llm.*`, `ModelEntry`, `switch_backend` | Task 10 |
| `_cmd_model_list` shows provider | Task 10 |
| `_persist_active_model` writes full `llm.*` | Task 10 |
| `_persist_available_models` writes `ModelEntry` dicts | Task 10 |
| `config_cli.py` uses `llm.*` keys | Task 11 |
| Initial hint file `linstor_stale_mount.yaml` | Task 3 |
| `openai-agent/` deleted | Task 14 |
| `ollama` in project dependencies | Task 5 |

No gaps found.
