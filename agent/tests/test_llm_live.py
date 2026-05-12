"""
Live integration tests for OllamaBackend think/tools combinations.

These tests hit the real Ollama API — run explicitly with:
    pytest tests/test_llm_live.py -v -s

Not included in the normal test suite (requires network + running Ollama).
"""
import asyncio
import sys
from pathlib import Path

# Import directly from submodules to avoid agent/__init__.py pulling in uvicorn
sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))
from config_schema import LlmConfig  # noqa: E402
from llm import OllamaBackend  # noqa: E402

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

# Simple single-turn history (always ends with user message)
_SIMPLE_HISTORY = [
    {"role": "user", "content": "Reply with exactly the words: hello world"},
]

# Multi-turn history ending with user message (realistic agent flow)
_HISTORY = [
    {"role": "user", "content": "check services"},
    {"role": "assistant", "content": "docker_service_list: all 10 services healthy"},
    {"role": "user", "content": "summarize what you found"},
]

# Multi-turn history ending with assistant message (edge case — like a mid-summary snapshot)
_HISTORY_ENDS_ASSISTANT = [
    {"role": "user", "content": "check services"},
    {"role": "assistant", "content": "docker_service_list: all 10 services healthy"},
]


def test_ollama_reachable():
    """Verify Ollama is reachable and the model is available."""
    import ollama
    client = ollama.Client(host=OLLAMA_URL)
    models = client.list()
    names = [m.model for m in models.models]
    print(f"\nAvailable models: {names}")
    assert any(MODEL in n for n in names), f"{MODEL!r} not found in {names}"


def _backend(think: bool | None = None) -> OllamaBackend:
    cfg = LlmConfig(
        provider="ollama",
        model=MODEL,
        base_url=OLLAMA_URL,
        num_ctx=4096,
        think=think,
    )
    return OllamaBackend(cfg)


def _run(coro):
    return asyncio.run(coro)


def _report(label: str, resp) -> None:
    content = "OK: got content" if resp.text else "EMPTY: no content"
    print(f"\n[{label}] tokens=({resp.input_tokens} in, {resp.output_tokens} out) → {content}")
    if resp.text:
        print(f"  text={resp.text[:120]!r}")
    if resp.tool_calls:
        print(f"  tool_calls={[t.name for t in resp.tool_calls]}")


# ---------------------------------------------------------------------------
# Baseline — simple single-turn prompts
# ---------------------------------------------------------------------------

def test_simple_no_tools_no_think():
    """Single user message, no tools, no think. Basic sanity check."""
    resp = _run(_backend().chat("You are a helpful assistant.", _SIMPLE_HISTORY))
    _report("simple / no_tools / no_think", resp)
    assert resp.text, "Expected non-empty response"


def test_simple_no_tools_think_false():
    """Single user message, no tools, think=False."""
    resp = _run(_backend(think=False).chat("You are a helpful assistant.", _SIMPLE_HISTORY))
    _report("simple / no_tools / think=False", resp)
    assert resp.text, "Expected non-empty response"


def test_simple_no_tools_think_true():
    """Single user message, no tools, think=True."""
    resp = _run(_backend(think=True).chat("You are a helpful assistant.", _SIMPLE_HISTORY))
    _report("simple / no_tools / think=True", resp)
    assert resp.text, "Expected non-empty response"


def test_simple_with_tools_think_false():
    """Single user message, with tools, think=False."""
    resp = _run(_backend(think=False).chat("You are a helpful assistant.", _SIMPLE_HISTORY, tool_defs=[_SIMPLE_TOOL]))
    _report("simple / tools / think=False", resp)
    assert resp.text or resp.tool_calls, "Expected text or tool call"


def test_simple_with_tools_think_true():
    """Single user message, with tools, think=True."""
    resp = _run(_backend(think=True).chat("You are a helpful assistant.", _SIMPLE_HISTORY, tool_defs=[_SIMPLE_TOOL]))
    _report("simple / tools / think=True", resp)
    assert resp.text or resp.tool_calls, "Expected text or tool call"


# ---------------------------------------------------------------------------
# No-tools cases (summary-like calls)
# ---------------------------------------------------------------------------

def test_no_tools_no_think_flag():
    """History ends with user message, no tools, no think. Should produce content."""
    resp = _run(_backend().chat("You are a helpful assistant.", _HISTORY))
    _report("no_tools / ends_user / no_think", resp)
    assert resp.text, "Expected non-empty response"


def test_no_tools_history_ends_assistant():
    """History ends with assistant message — documents whether model responds at all."""
    resp = _run(_backend().chat("Summarize this conversation.", _HISTORY_ENDS_ASSISTANT))
    _report("no_tools / ends_assistant / no_think", resp)
    # Just documenting behavior, not asserting
    print(f"  → {'OK' if resp.text else 'EMPTY — history ending with assistant suppresses output'}")


def test_no_tools_config_think_false():
    """No tools, think=False in config (not passed to API since no tools)."""
    resp = _run(_backend(think=False).chat("You are a helpful assistant.", _HISTORY))
    _report("no_tools / think=False (config)", resp)


def test_no_tools_config_think_true():
    """No tools, think=True in config (not passed to API since no tools)."""
    resp = _run(_backend(think=True).chat("You are a helpful assistant.", _HISTORY))
    _report("no_tools / think=True (config)", resp)


def test_no_tools_override_think_true():
    """No tools, think=True via think_override parameter."""
    resp = _run(_backend().chat("You are a helpful assistant.", _HISTORY, think_override=True))
    _report("no_tools / think_override=True", resp)


def test_no_tools_override_think_false():
    """No tools, think=False via think_override parameter."""
    resp = _run(_backend().chat("You are a helpful assistant.", _HISTORY, think_override=False))
    _report("no_tools / think_override=False", resp)


# ---------------------------------------------------------------------------
# With-tools cases (normal agent calls)
# ---------------------------------------------------------------------------

def test_with_tools_no_think_flag():
    """With tools, think not passed. Should produce content or tool call."""
    resp = _run(_backend().chat("You are a helpful assistant.", _HISTORY, tool_defs=[_SIMPLE_TOOL]))
    _report("tools / think=not passed", resp)
    assert resp.text or resp.tool_calls, "Expected text or tool call"


def test_with_tools_think_false():
    """With tools, think=False in config."""
    resp = _run(_backend(think=False).chat("You are a helpful assistant.", _HISTORY, tool_defs=[_SIMPLE_TOOL]))
    _report("tools / think=False", resp)
    assert resp.text or resp.tool_calls, "Expected text or tool call"


def test_with_tools_think_true():
    """With tools, think=True in config."""
    resp = _run(_backend(think=True).chat("You are a helpful assistant.", _HISTORY, tool_defs=[_SIMPLE_TOOL]))
    _report("tools / think=True", resp)
    assert resp.text or resp.tool_calls, "Expected text or tool call"
