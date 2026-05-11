"""Tests for LLMBackend helper methods."""
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
