# tests/test_agent_cancel.py
import asyncio
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def minimal_agent():
    """Build a HomelabAgent with the minimum viable config using model_construct."""
    from agent.agent import HomelabAgent
    from agent.config_schema import (
        AgentConfig, AnthropicConfig, SlackConfig, DockerConfig,
        SwarmConfig, AnsibleConfig, MonitorConfig, SafetyConfig,
        SafeModeResourcesConfig, ShellCommandGuardsConfig,
        ActionLogConfig, ControllerConfig,
    )
    config = AgentConfig.model_construct(
        anthropic=AnthropicConfig(model="claude-sonnet-4-20250514", input_cost_per_mtok=3.0, output_cost_per_mtok=15.0),
        slack=SlackConfig(channel="#test"),
        docker=DockerConfig(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig(nodes=[], ssh_key="/tmp/key", ssh_user="root"),
        ansible=AnsibleConfig(repo_path="/tmp", inventory="/tmp/inv.yml", git_author_name="Test", git_author_email="test@test.com"),
        monitor=MonitorConfig(poll_interval=30, grace_period_seconds=600),
        controller=ControllerConfig(mode="act"),
        safety=SafetyConfig(
            global_safe_mode=False,
            safe_mode_resources=SafeModeResourcesConfig(),
            tool_tiers={"run_shell": "agent"},
            log_agent_tier_reasoning=False,
            shell_command_guards=ShellCommandGuardsConfig(),
        ),
        action_log=ActionLogConfig(path="/tmp/action.log"),
    )
    with patch("agent.agent.anthropic.AsyncAnthropic"):
        agent = HomelabAgent(config)
    return agent


@pytest.mark.asyncio
async def test_cancel_all_cancels_pending(minimal_agent) -> None:
    """cancel_all() must cancel all pending approvals."""
    fut = minimal_agent._pending.register("plan-test", "run_shell", "plan text", tier=3)
    await minimal_agent.cancel_all()
    result = await fut
    approved, reason = result
    assert approved is False
    assert "emergency stop" in reason


@pytest.mark.asyncio
async def test_cancel_all_cancels_active_task(minimal_agent) -> None:
    """cancel_all() must cancel _active_task if set."""
    async def _long() -> None:
        await asyncio.sleep(100)

    task = asyncio.create_task(_long())
    minimal_agent._active_task = task
    await minimal_agent.cancel_all()
    await asyncio.sleep(0)
    assert task.cancelled()
