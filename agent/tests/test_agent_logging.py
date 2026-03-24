"""Tests for ActionLogger.log_tier_reasoning with guard metadata fields."""
import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from agent.agent import ActionLogger


@pytest.fixture
def logger(tmp_path) -> ActionLogger:
    return ActionLogger(path=str(tmp_path / "test.log"))


async def test_log_tier_reasoning_includes_guard_fields_when_set(logger: ActionLogger) -> None:
    logged_entries = []

    async def capture(record: dict) -> None:
        logged_entries.append(record)

    with patch.object(logger, "log", side_effect=capture):
        await logger.log_tier_reasoning(
            tool="run_shell",
            agent_proposed_tier=1,
            reasoning="looks safe",
            safe_mode_active=False,
            effective_tier=2,
            override_reason="shell_pattern_guard",
            guard_matched_list="force_tier2",
            guard_matched_pattern=r"\bgit\s+push\b",
        )

    assert len(logged_entries) == 1
    logged = logged_entries[0]
    assert logged["override_reason"] == "shell_pattern_guard"
    assert logged["guard_matched_list"] == "force_tier2"
    assert logged["guard_matched_pattern"] == r"\bgit\s+push\b"


async def test_log_tier_reasoning_omits_guard_fields_when_none(logger: ActionLogger) -> None:
    logged_entries = []

    async def capture(record: dict) -> None:
        logged_entries.append(record)

    with patch.object(logger, "log", side_effect=capture):
        await logger.log_tier_reasoning(
            tool="run_shell",
            agent_proposed_tier=1,
            reasoning="looks safe",
            safe_mode_active=False,
            effective_tier=1,
            override_reason=None,
            guard_matched_list=None,
            guard_matched_pattern=None,
        )

    assert len(logged_entries) == 1
    logged = logged_entries[0]
    assert "override_reason" not in logged
    assert "guard_matched_list" not in logged
    assert "guard_matched_pattern" not in logged


async def test_log_tier_reasoning_backward_compatible_no_guard_args(logger: ActionLogger) -> None:
    """Guard fields are optional and default to None — old call sites still work."""
    logged_entries = []

    async def capture(record: dict) -> None:
        logged_entries.append(record)

    with patch.object(logger, "log", side_effect=capture):
        await logger.log_tier_reasoning(
            tool="run_shell",
            agent_proposed_tier=1,
            reasoning="looks safe",
            safe_mode_active=False,
            effective_tier=1,
        )

    assert len(logged_entries) == 1
    logged = logged_entries[0]
    assert "override_reason" not in logged
