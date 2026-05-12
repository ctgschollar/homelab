"""Tests for history summarization and history management commands."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agent.config_schema import (
    AgentConfig, LlmConfig, SlackConfig, DockerConfig, SwarmConfig,
    AnsibleConfig, MonitorConfig, SafetyConfig, SafeModeResourcesConfig,
    ShellCommandGuardsConfig, ActionLogConfig, ControllerConfig,
    HistoryConfig, RollbackConfig, RagConfig, EdgeConfig,
    ApprovalListenerConfig, ModelEntry,
)


def _make_config(num_ctx: int = 16384) -> AgentConfig:
    return AgentConfig.model_construct(
        llm=LlmConfig(
            provider="anthropic",
            model="claude-test",
            input_cost_per_mtok=3.0,
            output_cost_per_mtok=15.0,
            num_ctx=num_ctx,
        ),
        slack=SlackConfig(channel="#test"),
        docker=DockerConfig(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig(nodes=[], ssh_key="/tmp/key", ssh_user="root"),
        ansible=AnsibleConfig(repo_path="/tmp", inventory="/tmp/inv.yml", git_author_name="Test", git_author_email="test@test.com"),
        monitor=MonitorConfig(poll_interval=30, grace_period_seconds=600),
        controller=ControllerConfig(mode="act"),
        safety=SafetyConfig(
            global_safe_mode=False,
            safe_mode_resources=SafeModeResourcesConfig(),
            tool_tiers={},
            log_agent_tier_reasoning=False,
            shell_command_guards=ShellCommandGuardsConfig(),
        ),
        action_log=ActionLogConfig(path="/tmp/action.log"),
        history=HistoryConfig(path="/tmp/test_agent_history.json"),
        rollback=RollbackConfig(state_path="/tmp/rollback.json"),
        rag=RagConfig(dsn=None, database="homelab_agent"),
        edge=EdgeConfig(cloudflare_tunnel_node="", ssh_key="", ssh_user=""),
        approval_listener=ApprovalListenerConfig(host="127.0.0.1", port=8765),
    )


def _make_agent(num_ctx: int = 16384):
    from agent.agent import HomelabAgent
    config = _make_config(num_ctx=num_ctx)
    with patch("agent.agent.create_backend"):
        agent = HomelabAgent(config)
    agent._slack = AsyncMock()
    agent._slack.notify = AsyncMock()
    return agent


# ---------------------------------------------------------------------------
# _is_plain_text
# ---------------------------------------------------------------------------

def test_is_plain_text_accepts_user_text():
    from agent.agent import HomelabAgent
    assert HomelabAgent._is_plain_text({"role": "user", "content": "hello"})


def test_is_plain_text_accepts_assistant_text():
    from agent.agent import HomelabAgent
    assert HomelabAgent._is_plain_text({"role": "assistant", "content": "hi there"})


def test_is_plain_text_rejects_tool_role():
    from agent.agent import HomelabAgent
    assert not HomelabAgent._is_plain_text({"role": "tool", "content": "result"})


def test_is_plain_text_rejects_tool_calls():
    from agent.agent import HomelabAgent
    assert not HomelabAgent._is_plain_text({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x"}}]})


def test_is_plain_text_rejects_list_content():
    from agent.agent import HomelabAgent
    assert not HomelabAgent._is_plain_text({"role": "user", "content": [{"type": "tool_result"}]})


def test_is_plain_text_rejects_empty_content():
    from agent.agent import HomelabAgent
    assert not HomelabAgent._is_plain_text({"role": "assistant", "content": ""})


# ---------------------------------------------------------------------------
# clear_history
# ---------------------------------------------------------------------------

def test_clear_history_empties_history():
    agent = _make_agent()
    agent._history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    agent.clear_history()
    assert agent._history == []


def test_clear_history_deletes_history_file(tmp_path):
    agent = _make_agent()
    history_file = tmp_path / "agent_history.json"
    history_file.write_text("[]")
    agent._history_path = history_file
    agent._history = [{"role": "user", "content": "x"}]
    agent.clear_history()
    assert not history_file.exists()


def test_clear_history_no_error_if_file_missing(tmp_path):
    agent = _make_agent()
    agent._history_path = tmp_path / "nonexistent.json"
    agent._history = [{"role": "user", "content": "x"}]
    agent.clear_history()  # should not raise
    assert agent._history == []


# ---------------------------------------------------------------------------
# _summarize_history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summarize_history_calls_backend():
    agent = _make_agent()
    agent._history = [
        {"role": "user", "content": "sonarr is down"},
        {"role": "assistant", "content": "Let me check"},
        {"role": "user", "content": "what did you find?"},
        {"role": "assistant", "content": "It crashed"},
    ]
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(return_value=MagicMock(text="Summary: sonarr crashed.", tool_calls=[]))

    summary = await agent._summarize_history()

    assert agent._backend.chat.called
    assert summary == "Summary: sonarr crashed."


@pytest.mark.asyncio
async def test_summarize_history_notifies_slack():
    agent = _make_agent()
    agent._history = [
        {"role": "user", "content": "sonarr is down"},
        {"role": "assistant", "content": "Investigating"},
    ]
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(return_value=MagicMock(text="Summary text.", tool_calls=[]))

    await agent._summarize_history()

    agent._slack.notify.assert_called_once()
    call_text = agent._slack.notify.call_args[0][0]
    assert "Summary text." in call_text


@pytest.mark.asyncio
async def test_summarize_history_keeps_last_3_turns():
    agent = _make_agent()
    agent._history = [
        {"role": "user", "content": "old1"},
        {"role": "assistant", "content": "old2"},
        {"role": "user", "content": "old3"},
        {"role": "assistant", "content": "old4"},
        {"role": "user", "content": "recent1"},
        {"role": "assistant", "content": "recent2"},
        {"role": "user", "content": "recent3"},
    ]
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(return_value=MagicMock(text="The summary.", tool_calls=[]))

    await agent._summarize_history()

    # History should be: [summary_user, summary_assistant, recent1, recent2, recent3]
    assert len(agent._history) == 5
    assert agent._history[0]["content"] == "[Earlier conversation summary — use this as context]"
    assert agent._history[1]["content"] == "The summary."
    assert agent._history[2]["content"] == "recent1"
    assert agent._history[4]["content"] == "recent3"


@pytest.mark.asyncio
async def test_summarize_history_short_history_uses_all():
    """With fewer than 3 turns, summarize everything and keep no verbatim turns."""
    agent = _make_agent()
    agent._history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(return_value=MagicMock(text="Brief summary.", tool_calls=[]))

    await agent._summarize_history()

    assert len(agent._history) == 2
    assert agent._history[1]["content"] == "Brief summary."


@pytest.mark.asyncio
async def test_summarize_history_returns_empty_on_error():
    agent = _make_agent()
    agent._history = [{"role": "user", "content": "x"}]
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(side_effect=RuntimeError("API error"))

    result = await agent._summarize_history()

    assert result == ""
    agent._slack.notify.assert_not_called()


# ---------------------------------------------------------------------------
# get_summary (on-demand)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_summary_returns_text():
    agent = _make_agent()
    agent._history = [
        {"role": "user", "content": "sonarr down"},
        {"role": "assistant", "content": "fixed"},
    ]
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(return_value=MagicMock(text="On-demand summary.", tool_calls=[]))

    result = await agent.get_summary()
    assert result == "On-demand summary."


@pytest.mark.asyncio
async def test_get_summary_empty_history():
    agent = _make_agent()
    agent._history = []
    result = await agent.get_summary()
    assert "no history" in result.lower() or result == ""


# ---------------------------------------------------------------------------
# auto-summarization threshold in _run_loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_loop_triggers_summarize_when_over_threshold():
    """When input_tokens >= num_ctx // 2 and cooldown elapsed, _summarize_history is called."""
    agent = _make_agent(num_ctx=1000)
    agent._history = []
    agent._messages_since_summary = 10  # bypass cooldown

    from agent.llm import LLMResponse
    # First response: over threshold (600 > 500), with stop=True
    response = LLMResponse(
        text="done",
        tool_calls=[],
        stop=True,
        input_tokens=600,
        output_tokens=10,
        assistant_history_entry={"role": "assistant", "content": "done"},
    )
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(return_value=response)
    agent._summarize_history = AsyncMock(return_value="summary")

    agent._history.append({"role": "user", "content": "test"})
    await agent._run_loop("cli:test")

    agent._summarize_history.assert_called_once()


@pytest.mark.asyncio
async def test_run_loop_does_not_summarize_within_cooldown():
    """Even if over threshold, _summarize_history is NOT called within the 10-message cooldown."""
    agent = _make_agent(num_ctx=1000)
    agent._messages_since_summary = 5  # under cooldown

    from agent.llm import LLMResponse
    response = LLMResponse(
        text="done",
        tool_calls=[],
        stop=True,
        input_tokens=600,  # over threshold
        output_tokens=10,
        assistant_history_entry={"role": "assistant", "content": "done"},
    )
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(return_value=response)
    agent._summarize_history = AsyncMock(return_value="")

    agent._history.append({"role": "user", "content": "test"})
    await agent._run_loop("cli:test")

    agent._summarize_history.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_does_not_summarize_below_threshold():
    """When input_tokens < num_ctx // 2, _summarize_history is NOT called."""
    agent = _make_agent(num_ctx=1000)

    from agent.llm import LLMResponse
    response = LLMResponse(
        text="done",
        tool_calls=[],
        stop=True,
        input_tokens=100,
        output_tokens=10,
        assistant_history_entry={"role": "assistant", "content": "done"},
    )
    agent._backend = AsyncMock()
    agent._backend.chat = AsyncMock(return_value=response)
    agent._summarize_history = AsyncMock(return_value="")

    agent._history.append({"role": "user", "content": "test"})
    await agent._run_loop("cli:test")

    agent._summarize_history.assert_not_called()


# ---------------------------------------------------------------------------
# Controller history commands
# ---------------------------------------------------------------------------

def make_controller(tmp_path: Path):
    from controller import AgentController
    from agent.config_schema import ControllerConfig, MonitorConfig, LlmConfig, SlackConfig, DockerConfig, SwarmConfig, AnsibleConfig, SafetyConfig, SafeModeResourcesConfig, ShellCommandGuardsConfig, ActionLogConfig, AgentConfig

    config = AgentConfig.model_construct(
        controller=ControllerConfig(mode="act", whitelist_path=str(tmp_path / "whitelist.json")),
        monitor=MonitorConfig(poll_interval=30, grace_period_seconds=10),
        llm=LlmConfig(provider="anthropic", model="claude-test", input_cost_per_mtok=3.0, output_cost_per_mtok=15.0),
        slack=SlackConfig(channel="#test"),
        docker=DockerConfig(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig(nodes=[], ssh_key="/tmp/key", ssh_user="root"),
        ansible=AnsibleConfig(repo_path="/tmp", inventory="/tmp/inv.yml", git_author_name="Test", git_author_email="test@test.com"),
        safety=SafetyConfig(global_safe_mode=False, safe_mode_resources=SafeModeResourcesConfig(), tool_tiers={}, log_agent_tier_reasoning=False, shell_command_guards=ShellCommandGuardsConfig()),
        action_log=ActionLogConfig(path="/tmp/action.log"),
    )
    agent = AsyncMock()
    agent.clear_history = MagicMock()
    agent.get_summary = AsyncMock(return_value="Summary of investigation.")
    slack = AsyncMock()
    slack.notify = AsyncMock()

    return AgentController(config=config, agents={"default": agent}, slack=slack, config_path="/tmp/test_config.yaml"), agent, slack


@pytest.mark.asyncio
async def test_cmd_history_clear(tmp_path):
    controller, agent, slack = make_controller(tmp_path)
    result = await controller.handle_command("history clear")
    agent.clear_history.assert_called_once()
    assert "cleared" in result.lower()


@pytest.mark.asyncio
async def test_cmd_history_summary(tmp_path):
    controller, agent, slack = make_controller(tmp_path)
    result = await controller.handle_command("history summary")
    agent.get_summary.assert_called_once()
    assert "Summary posted" in result or "posted" in result.lower()


@pytest.mark.asyncio
async def test_cmd_history_summary_notifies_slack(tmp_path):
    controller, agent, slack = make_controller(tmp_path)
    await controller.handle_command("history summary")
    slack.notify.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_history_unknown_subcommand(tmp_path):
    controller, agent, slack = make_controller(tmp_path)
    result = await controller.handle_command("history foo")
    assert "unknown" in result.lower() or "usage" in result.lower()


@pytest.mark.asyncio
async def test_history_is_command(tmp_path):
    controller, agent, slack = make_controller(tmp_path)
    assert controller.is_command("history clear")
    assert controller.is_command("history summary")
    assert controller.is_command("history")
