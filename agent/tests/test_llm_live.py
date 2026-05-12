"""
Live integration tests for OllamaBackend think/tools combinations.

These tests hit the real Ollama API — run explicitly with:
    hatch run pytest tests/test_llm_live.py -v -s

Not included in the normal test suite (requires network + running Ollama).
"""
import pytest
from agent.config_schema import LlmConfig
from agent.llm import OllamaBackend

pytestmark = pytest.mark.live

OLLAMA_URL = "http://192.168.88.144:11434"
MODEL = "qwen3.6:27b"

_SIMPLE_TOOL = {
    "name": "echo",
    "description": "Echo a message back.",
    "input_schema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
}

_HISTORY = [{"role": "user", "content": "Reply with exactly: hello world"}]
_SUMMARY_HISTORY = [
    {"role": "user", "content": "check services"},
    {"role": "assistant", "content": "docker_service_list: all 10 services healthy"},
]


def _backend(think: bool | None = None) -> OllamaBackend:
    cfg = LlmConfig(
        provider="ollama",
        model=MODEL,
        base_url=OLLAMA_URL,
        num_ctx=4096,
        think=think,
    )
    return OllamaBackend(cfg)


async def _chat(backend: OllamaBackend, tools=None, think_override=None, history=None):
    h = history or _HISTORY
    kwargs = {}
    if tools is not None:
        kwargs["tool_defs"] = tools
    if think_override is not None:
        kwargs["think_override"] = think_override
    return await backend.chat("You are a helpful assistant.", h, **kwargs)


# ---------------------------------------------------------------------------
# No-tools cases (summary-like calls)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_tools_no_think_flag():
    """Baseline: no tools, think not passed. Should produce content."""
    backend = _backend(think=None)
    resp = await _chat(backend, history=_SUMMARY_HISTORY)
    print(f"\n[no_tools/no_think] tokens=({resp.input_tokens} in, {resp.output_tokens} out) text={resp.text!r}")
    assert resp.text, "Expected non-empty response"


@pytest.mark.asyncio
async def test_no_tools_think_false_via_config():
    """No tools, think=False in config (applied via think_override path when passed)."""
    backend = _backend(think=False)
    resp = await _chat(backend, history=_SUMMARY_HISTORY)
    print(f"\n[no_tools/think=False config] tokens=({resp.input_tokens} in, {resp.output_tokens} out) text={resp.text!r}")
    # Document behavior — may be empty
    print(f"  → {'OK: got content' if resp.text else 'EMPTY: no content'}")


@pytest.mark.asyncio
async def test_no_tools_think_true_override():
    """No tools, think=True via think_override. Known to produce empty on Qwen3?"""
    backend = _backend(think=None)
    resp = await _chat(backend, think_override=True, history=_SUMMARY_HISTORY)
    print(f"\n[no_tools/think_override=True] tokens=({resp.input_tokens} in, {resp.output_tokens} out) text={resp.text!r}")
    print(f"  → {'OK: got content' if resp.text else 'EMPTY: no content'}")


@pytest.mark.asyncio
async def test_no_tools_think_false_override():
    """No tools, think=False via think_override."""
    backend = _backend(think=None)
    resp = await _chat(backend, think_override=False, history=_SUMMARY_HISTORY)
    print(f"\n[no_tools/think_override=False] tokens=({resp.input_tokens} in, {resp.output_tokens} out) text={resp.text!r}")
    print(f"  → {'OK: got content' if resp.text else 'EMPTY: no content'}")


# ---------------------------------------------------------------------------
# With-tools cases (normal agent calls)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_with_tools_no_think_flag():
    """With tools, think not passed. Should produce content or tool call."""
    backend = _backend(think=None)
    resp = await _chat(backend, tools=[_SIMPLE_TOOL])
    print(f"\n[tools/no_think] tokens=({resp.input_tokens} in, {resp.output_tokens} out) text={resp.text!r} tool_calls={[t.name for t in resp.tool_calls]}")
    assert resp.text or resp.tool_calls, "Expected text or tool call"


@pytest.mark.asyncio
async def test_with_tools_think_false():
    """With tools, think=False in config. Should work."""
    backend = _backend(think=False)
    resp = await _chat(backend, tools=[_SIMPLE_TOOL])
    print(f"\n[tools/think=False] tokens=({resp.input_tokens} in, {resp.output_tokens} out) text={resp.text!r} tool_calls={[t.name for t in resp.tool_calls]}")
    assert resp.text or resp.tool_calls, "Expected text or tool call"


@pytest.mark.asyncio
async def test_with_tools_think_true():
    """With tools, think=True in config. Should work (extended reasoning)."""
    backend = _backend(think=True)
    resp = await _chat(backend, tools=[_SIMPLE_TOOL])
    print(f"\n[tools/think=True] tokens=({resp.input_tokens} in, {resp.output_tokens} out) text={resp.text!r} tool_calls={[t.name for t in resp.tool_calls]}")
    assert resp.text or resp.tool_calls, "Expected text or tool call"
