# tests/test_controller.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


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
    agent.switch_backend = MagicMock()

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


# --- mode routing ---

@pytest.mark.asyncio
async def test_monitor_mode_does_not_call_agent(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="monitor")
    event = {"source": "monitor", "type": "services_down", "data": {"services": [{"service": "foo", "running": 0, "desired": 1, "last_error": ""}]}}
    await controller.handle_event(event)
    agent.handle_event.assert_not_called()
    slack.notify.assert_called_once()


@pytest.mark.asyncio
async def test_act_mode_defers_monitor_event(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    event = {"source": "monitor", "type": "services_down", "data": {"services": [{"service": "foo", "running": 0, "desired": 1, "last_error": ""}]}}
    await controller.handle_event(event)
    # Should be deferred, not sent to agent immediately
    agent.handle_event.assert_not_called()
    assert len(controller.deferred) == 1
    # cleanup
    for alert in controller.deferred.values():
        alert.timer_task.cancel()


@pytest.mark.asyncio
async def test_stopped_drops_event(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    controller.stopped = True
    event = {"source": "monitor", "type": "services_down", "data": {"services": [{"service": "foo", "running": 0, "desired": 1, "last_error": ""}]}}
    await controller.handle_event(event)
    agent.handle_event.assert_not_called()
    assert len(controller.deferred) == 0


# --- control commands ---

@pytest.mark.asyncio
async def test_cmd_stop_sets_stopped_flag(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    result = await controller.handle_command("stop")
    assert controller.stopped is True
    assert "Stopped" in result


@pytest.mark.asyncio
async def test_cmd_start_clears_stopped_flag(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    controller.stopped = True
    result = await controller.handle_command("start")
    assert controller.stopped is False
    assert "Resumed" in result


@pytest.mark.asyncio
async def test_cmd_stop_calls_agent_cancel_all(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    await controller.handle_command("stop")
    agent.cancel_all.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_queue_empty(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    result = await controller.handle_command("queue")
    assert "No active work" in result or "No pending" in result


@pytest.mark.asyncio
async def test_cmd_mode_monitor(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    with patch.object(controller, "_persist_mode"):
        result = await controller.handle_command("mode monitor")
    assert controller.mode == "monitor"
    assert "monitor" in result.lower()


@pytest.mark.asyncio
async def test_cmd_mode_act(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="monitor")
    with patch.object(controller, "_persist_mode"):
        result = await controller.handle_command("mode act")
    assert controller.mode == "act"
    assert "act" in result.lower()


@pytest.mark.asyncio
async def test_cmd_case_insensitive(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act")
    result = await controller.handle_command("STOP")
    assert controller.stopped is True


# --- grace period ---

@pytest.mark.asyncio
async def test_defer_creates_deferred_alert(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=10, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "sonarr_sonarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    assert len(controller.deferred) == 1
    alert = list(controller.deferred.values())[0]
    assert "sonarr_sonarr" in alert.services
    alert.timer_task.cancel()  # cleanup


@pytest.mark.asyncio
async def test_grace_period_timer_fires_agent(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=0, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "sonarr_sonarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    await asyncio.sleep(0.05)  # let timer fire
    agent.handle_event.assert_called_once()
    assert len(controller.deferred) == 0


@pytest.mark.asyncio
async def test_start_alert_fires_agent_immediately(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "radarr_radarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    alert_id = list(controller.deferred.keys())[0]
    result = await controller.start_alert(alert_id)
    assert result is True
    await asyncio.sleep(0.05)
    agent.handle_event.assert_called_once()
    assert len(controller.deferred) == 0


@pytest.mark.asyncio
async def test_ignore_alert_cancels_without_agent(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "radarr_radarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    alert_id = list(controller.deferred.keys())[0]
    result = await controller.ignore_alert(alert_id)
    assert result is True
    agent.handle_event.assert_not_called()
    assert len(controller.deferred) == 0


@pytest.mark.asyncio
async def test_self_healed_writes_rag_incident(tmp_path) -> None:
    rag = AsyncMock()
    rag.count_incidents = AsyncMock(return_value=5)
    rag.store_incident = AsyncMock()
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    controller._rag = rag

    # First defer an alert
    down_event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "jellyfin_jellyfin", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(down_event)

    # Then recover
    recovery_event = {
        "source": "monitor", "type": "service_recovered",
        "data": {"service": "jellyfin_jellyfin", "down_duration_seconds": 45},
    }
    await controller.handle_event(recovery_event)

    rag.store_incident.assert_called_once()
    call_args = rag.store_incident.call_args[0][0]
    assert "self-healed" in call_args["tags"]
    assert "jellyfin_jellyfin" in call_args["title"]
    agent.handle_event.assert_not_called()
    slack.notify.assert_called()


@pytest.mark.asyncio
async def test_recovery_with_no_deferred_alert_goes_to_agent(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    # No deferred alert — recovery goes straight to agent
    recovery_event = {
        "source": "monitor", "type": "service_recovered",
        "data": {"service": "jellyfin_jellyfin", "down_duration_seconds": 300},
    }
    await controller.handle_event(recovery_event)
    agent.handle_event.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_queue_shows_pending_alert(tmp_path) -> None:
    controller, agent, slack = make_controller(mode="act", grace_period=3600, tmp_path=tmp_path)
    event = {
        "source": "monitor", "type": "services_down",
        "data": {"services": [{"service": "sonarr_sonarr", "running": 0, "desired": 1, "last_error": ""}]},
    }
    await controller.handle_event(event)
    result = await controller.handle_command("queue")
    assert "sonarr_sonarr" in result
    # cleanup
    for alert in controller.deferred.values():
        alert.timer_task.cancel()


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
    entry = next(m for m in controller._config.llm.available_models if m.name == "llama3.1:8b")
    assert entry.provider == "anthropic"


@pytest.mark.asyncio
async def test_model_add_with_ollama_provider(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model add llama3.1:8b ollama")
    assert "llama3.1:8b" in result
    assert "ollama" in result
    entry = next(m for m in controller._config.llm.available_models if m.name == "llama3.1:8b")
    assert entry.provider == "ollama"


@pytest.mark.asyncio
async def test_model_add_invalid_provider(tmp_path) -> None:
    controller, agent, slack = make_controller(tmp_path=tmp_path)
    result = await controller.handle_command("model add llama3.1:8b openai")
    assert "Unknown provider" in result
    assert not any(m.name == "llama3.1:8b" for m in controller._config.llm.available_models)


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
