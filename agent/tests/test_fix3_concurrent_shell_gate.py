"""Tests for Fix 3: concurrent shell gate.

Verifies that ToolExecutor._shell_gate serializes concurrent run_shell calls
so they never execute simultaneously, even when gathered at tier-1.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config_schema import (
    ActionLogConfig,
    AgentConfig,
    AnsibleConfig,
    ApprovalListenerConfig,
    DockerConfig,
    EdgeConfig,
    HistoryConfig,
    MonitorConfig,
    ReportsConfig,
    RollbackConfig,
    SafetyConfig,
    SafeModeResourcesConfig,
    ShellCommandGuardsConfig,
    SlackConfig,
    SwarmConfig,
    AnthropicConfig,
)
from agent.tools import ToolExecutor


def _make_config() -> AgentConfig:
    return AgentConfig.model_construct(
        anthropic=AnthropicConfig.model_construct(
            api_key=None,
            model="claude-test",
            input_cost_per_mtok=3.0,
            output_cost_per_mtok=15.0,
        ),
        slack=SlackConfig.model_construct(
            bot_token=None,
            signing_secret=None,
            channel="#test",
            veto_window_seconds=300,
        ),
        docker=DockerConfig.model_construct(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig.model_construct(
            nodes=["dks01.example.com"],
            ssh_key="/root/.ssh/id_rsa",
            ssh_user="root",
        ),
        edge=EdgeConfig.model_construct(cloudflare_tunnel_node="", ssh_key="", ssh_user=""),
        ansible=AnsibleConfig.model_construct(
            repo_path="/opt/homelab",
            inventory="/opt/homelab/ansible/inventory.yml",
            git_token=None,
            git_author_name="Test",
            git_author_email="test@example.com",
        ),
        monitor=MonitorConfig.model_construct(poll_interval=30, watched_stacks=[]),
        safety=SafetyConfig.model_construct(
            global_safe_mode=False,
            safe_mode_resources=SafeModeResourcesConfig(),
            tool_tiers={"run_shell": "agent"},
            log_agent_tier_reasoning=False,
            shell_command_guards=ShellCommandGuardsConfig(),
        ),
        reports=ReportsConfig.model_construct(path="reports", tags=[]),
        action_log=ActionLogConfig.model_construct(path="./action.log"),
        approval_listener=ApprovalListenerConfig.model_construct(host="127.0.0.1", port=8765),
        history=HistoryConfig.model_construct(path="./agent_history.json"),
        rollback=RollbackConfig.model_construct(state_path="./rollback_state.json"),
    )


def _make_executor() -> ToolExecutor:
    slack = MagicMock()
    return ToolExecutor(_make_config(), slack)


# ---------------------------------------------------------------------------
# test_shell_gate_serializes_concurrent_calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shell_gate_serializes_concurrent_calls() -> None:
    """Two concurrent run_shell calls must not overlap in execution."""
    executor = _make_executor()

    order: list[str] = []
    barrier = asyncio.Event()

    async def fake_run_subprocess_first(args: list[str], **kwargs: Any) -> str:
        order.append("start_1")
        # Hold until signalled, simulating a slow command
        await asyncio.sleep(0.05)
        order.append("finish_1")
        return "result_1"

    async def fake_run_subprocess_second(args: list[str], **kwargs: Any) -> str:
        order.append("start_2")
        await asyncio.sleep(0)
        order.append("finish_2")
        return "result_2"

    call_count = 0
    original_run = executor._run_subprocess

    async def dispatching_mock(args: list[str], **kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await fake_run_subprocess_first(args, **kwargs)
        return await fake_run_subprocess_second(args, **kwargs)

    executor._run_subprocess = dispatching_mock  # type: ignore[method-assign]

    inp_local = {"command": "df -h", "agent_proposed_tier": 1, "agent_reasoning": "read-only"}
    inp_ssh = {"command": "df -h", "node": "dks01.example.com", "agent_proposed_tier": 1, "agent_reasoning": "read-only"}

    # Fire both concurrently
    await asyncio.gather(
        executor._tool_run_shell(inp_local),
        executor._tool_run_shell(inp_ssh),
    )

    # The gate must have serialized them: first finishes before second starts
    assert order.index("finish_1") < order.index("start_2"), (
        f"Commands overlapped — order was: {order}"
    )


# ---------------------------------------------------------------------------
# test_shell_gate_releases_after_normal_completion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shell_gate_releases_after_normal_completion() -> None:
    """The semaphore must be released (value=1) after a successful run."""
    executor = _make_executor()
    executor._run_subprocess = AsyncMock(return_value="ok")  # type: ignore[method-assign]

    inp = {"command": "uptime", "agent_proposed_tier": 1, "agent_reasoning": "read-only"}
    await executor._tool_run_shell(inp)

    assert executor._shell_gate._value == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# test_shell_gate_releases_after_exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shell_gate_releases_after_exception() -> None:
    """The semaphore must be released even when _run_subprocess raises."""
    executor = _make_executor()
    executor._run_subprocess = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    inp = {"command": "false", "agent_proposed_tier": 1, "agent_reasoning": "will fail"}
    with pytest.raises(RuntimeError, match="boom"):
        await executor._tool_run_shell(inp)

    assert executor._shell_gate._value == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# test_shell_gate_local_and_ssh_both_gated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shell_gate_local_and_ssh_both_gated() -> None:
    """Both local (bash) and SSH commands are serialized through the same gate."""
    executor = _make_executor()

    timeline: list[tuple[str, str]] = []  # (event, command_type)

    call_count = 0

    async def timed_subprocess(args: list[str], **kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        idx = call_count
        cmd_type = "ssh" if args[0] == "ssh" else "local"
        timeline.append(("start", cmd_type))
        await asyncio.sleep(0.04)
        timeline.append(("finish", cmd_type))
        return f"result_{idx}"

    executor._run_subprocess = timed_subprocess  # type: ignore[method-assign]

    local_inp = {"command": "hostname", "agent_proposed_tier": 1, "agent_reasoning": "read-only"}
    ssh_inp = {
        "command": "hostname",
        "node": "dks01.example.com",
        "agent_proposed_tier": 1,
        "agent_reasoning": "read-only",
    }

    await asyncio.gather(
        executor._tool_run_shell(local_inp),
        executor._tool_run_shell(ssh_inp),
    )

    # Identify the finish of whichever ran first and the start of whichever ran second
    first_finish_idx = next(i for i, e in enumerate(timeline) if e[0] == "finish")
    second_start_idx = next(
        i for i, e in enumerate(timeline) if e[0] == "start" and i > 0
    )
    assert first_finish_idx < second_start_idx, (
        f"Commands overlapped — timeline: {timeline}"
    )
