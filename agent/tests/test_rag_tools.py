"""Tests for RAG-backed tools: write_incident_report and search_incidents."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config_schema import (
    ActionLogConfig, AgentConfig, AnsibleConfig, ApprovalListenerConfig,
    DockerConfig, EdgeConfig, HistoryConfig, MonitorConfig, RagConfig,
    RollbackConfig, SafetyConfig, SafeModeResourcesConfig,
    ShellCommandGuardsConfig, SlackConfig, SwarmConfig, AnthropicConfig,
)
from agent.tools import ToolExecutor
from agent.rag import IncidentRAG


def _make_config() -> AgentConfig:
    return AgentConfig.model_construct(
        anthropic=AnthropicConfig.model_construct(api_key=None, model="x", input_cost_per_mtok=3.0, output_cost_per_mtok=15.0),
        slack=SlackConfig.model_construct(bot_token=None, signing_secret=None, channel="#t", veto_window_seconds=300),
        docker=DockerConfig.model_construct(socket="unix:///var/run/docker.sock"),
        swarm=SwarmConfig.model_construct(nodes=[], ssh_key="/k", ssh_user="root"),
        edge=EdgeConfig.model_construct(cloudflare_tunnel_node="", ssh_key="", ssh_user=""),
        ansible=AnsibleConfig.model_construct(repo_path="/opt/homelab", inventory="/opt/homelab/ansible/inventory.yml", git_token=None, git_author_name="Agent", git_author_email="agent@example.com"),
        monitor=MonitorConfig.model_construct(poll_interval=30),
        safety=SafetyConfig.model_construct(global_safe_mode=False, safe_mode_resources=SafeModeResourcesConfig(), tool_tiers={}, log_agent_tier_reasoning=False, shell_command_guards=ShellCommandGuardsConfig()),
        action_log=ActionLogConfig.model_construct(path="./action.log"),
        approval_listener=ApprovalListenerConfig.model_construct(host="127.0.0.1", port=8765),
        history=HistoryConfig.model_construct(path="./h.json"),
        rollback=RollbackConfig.model_construct(state_path="./r.json"),
        rag=RagConfig.model_construct(dsn="postgresql://u:p@host/postgres", database="homelab_agent", log_rag_debug=False),
    )


def _make_mock_rag(count: int = 0) -> MagicMock:
    rag = MagicMock(spec=IncidentRAG)
    rag.store_incident = AsyncMock()
    rag.search_incidents = AsyncMock(return_value=[])
    rag.count_incidents = AsyncMock(return_value=count)
    return rag


# ---------------------------------------------------------------------------
# ToolExecutor accepts rag parameter
# ---------------------------------------------------------------------------

def test_tool_executor_accepts_rag_none() -> None:
    """ToolExecutor must accept rag=None without error."""
    slack = MagicMock()
    executor = ToolExecutor(_make_config(), slack, rag=None)
    assert executor._rag is None


def test_tool_executor_stores_rag() -> None:
    """ToolExecutor must store the provided rag instance."""
    slack = MagicMock()
    mock_rag = _make_mock_rag()
    executor = ToolExecutor(_make_config(), slack, rag=mock_rag)
    assert executor._rag is mock_rag


# ---------------------------------------------------------------------------
# write_incident_report — with RAG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_incident_report_calls_store_incident() -> None:
    """write_incident_report must call rag.store_incident with the incident dict."""
    mock_rag = _make_mock_rag(count=3)
    slack = MagicMock()
    slack.notify = AsyncMock()
    executor = ToolExecutor(_make_config(), slack, rag=mock_rag)

    tool_input = {
        "title": "Traefik crash",
        "tags": ["failure", "docker"],
        "inciting_incident": "Traefik exited unexpectedly.",
        "resolution": "Reverted to previous image.",
        "tools_used": ["docker_service_inspect"],
        "start_time": "",
    }
    result = await executor._tool_write_incident_report(tool_input)

    assert mock_rag.store_incident.called
    stored = mock_rag.store_incident.call_args[0][0]
    assert stored["id"] == "INC-0004"  # count was 3, so 3+1=4
    assert stored["title"] == "Traefik crash"
    assert stored["inciting_incident"] == "Traefik exited unexpectedly."
    assert stored["resolution"] == "Reverted to previous image."
    assert isinstance(stored["date"], datetime)


@pytest.mark.asyncio
async def test_write_incident_report_no_file_written(tmp_path) -> None:
    """write_incident_report must NOT write any Markdown file."""
    mock_rag = _make_mock_rag(count=0)
    slack = MagicMock()
    slack.notify = AsyncMock()
    executor = ToolExecutor(_make_config(), slack, rag=mock_rag)

    tool_input = {
        "title": "Test incident",
        "tags": ["failure"],
        "inciting_incident": "Something broke.",
        "resolution": "Fixed it.",
        "tools_used": [],
        "start_time": "",
    }
    await executor._tool_write_incident_report(tool_input)

    # No .md files should exist in tmp_path or /opt/homelab/reports
    assert list(tmp_path.glob("*.md")) == []


@pytest.mark.asyncio
async def test_write_incident_report_rag_none_returns_warning() -> None:
    """write_incident_report must return a warning when rag is None."""
    slack = MagicMock()
    executor = ToolExecutor(_make_config(), slack, rag=None)

    tool_input = {
        "title": "Test",
        "tags": [],
        "inciting_incident": "x",
        "resolution": "y",
        "tools_used": [],
        "start_time": "",
    }
    result = await executor._tool_write_incident_report(tool_input)
    assert "RAG" in result or "not configured" in result.lower()


# ---------------------------------------------------------------------------
# search_incidents tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_incidents_returns_formatted_text() -> None:
    mock_rag = _make_mock_rag()
    mock_rag.search_incidents = AsyncMock(return_value=[
        {
            "id": "INC-0001",
            "title": "Traefik down",
            "date": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "tags": ["failure", "docker"],
            "inciting_incident": "Container exited.",
            "resolution": "Reverted image.",
            "similarity": 0.91,
        }
    ])
    slack = MagicMock()
    executor = ToolExecutor(_make_config(), slack, rag=mock_rag)

    result = await executor._tool_search_incidents({"query": "traefik crash"})
    assert "INC-0001" in result
    assert "Traefik down" in result
    assert "0.91" in result


@pytest.mark.asyncio
async def test_search_incidents_rag_none_returns_message() -> None:
    slack = MagicMock()
    executor = ToolExecutor(_make_config(), slack, rag=None)
    result = await executor._tool_search_incidents({"query": "anything"})
    assert "not configured" in result.lower() or "RAG" in result
