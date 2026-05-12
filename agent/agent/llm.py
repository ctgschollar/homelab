import asyncio
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
        think_override: bool | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    def set_think(self, value: bool | None) -> None: ...

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

    def set_think(self, value: bool | None) -> None:
        pass

    async def chat(
        self,
        system: str,
        history: list[dict],
        tool_defs: list[dict],
        think_override: bool | None = None,
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
        self._num_ctx = config.num_ctx
        self._think = config.think

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

    def set_think(self, value: bool | None) -> None:
        self._think = value

    async def chat(
        self,
        system: str,
        history: list[dict],
        tool_defs: list[dict],
        think_override: bool | None = None,
    ) -> LLMResponse:
        messages = [{"role": "system", "content": system}] + history
        ollama_tools = [self._to_ollama_tool(t) for t in tool_defs]

        logger.debug("API REQUEST model=%s history_turns=%d", self._model, len(history))

        delay = 5
        for attempt in range(5):
            try:
                kwargs: dict = {
                    "model": self._model,
                    "messages": messages,
                    "stream": False,
                    "options": {"num_ctx": self._num_ctx},
                }
                if ollama_tools:
                    kwargs["tools"] = ollama_tools
                    effective_think = think_override if think_override is not None else self._think
                    if effective_think is not None:
                        kwargs["think"] = effective_think
                elif think_override is not None:
                    kwargs["think"] = think_override
                response = await self._client.chat(**kwargs)
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
