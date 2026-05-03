import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from runner.db import init_db
from runner.sessions import create_session, get_session
from runner.models import Status
from runner.process import build_prompt, start_run, stop_run


@pytest.fixture(autouse=True)
async def db(tmp_env):
    (tmp_env / "logs").mkdir(exist_ok=True)
    await init_db()


def test_build_prompt_both():
    result = build_prompt("do the task", "and also this")
    assert result == "do the task\n\n---\n\nand also this"


def test_build_prompt_base_only():
    result = build_prompt("do the task", None)
    assert result == "do the task"


def test_build_prompt_extra_only():
    result = build_prompt(None, "just this")
    assert result == "just this"


def test_build_prompt_neither():
    result = build_prompt(None, None)
    assert result == "Continue with the task we discussed."


async def test_start_run_updates_status_to_running(tmp_env):
    await create_session("app", "/repos/app", session_id="uuid-123")

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.stdout = AsyncMock()
    mock_proc.stdout.__aiter__ = AsyncMock(return_value=iter([]))
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.returncode = 0

    with patch("runner.process.asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        with patch("runner.process.asyncio.create_task"):
            pid = await start_run("app", "uuid-123", "do the thing", None)

    assert pid == 9999
    session = await get_session("app")
    assert session.status == Status.RUNNING
    assert session.pid == 9999


async def test_stop_run_updates_status_to_idle(tmp_env):
    await create_session("app2", "/repos/app2", session_id="uuid-456")
    # Manually set to running
    from runner.sessions import update_session
    await update_session("app2", status=Status.RUNNING.value, pid=12345)

    with patch("runner.process.os.kill") as mock_kill:
        await stop_run("app2", 12345)
        mock_kill.assert_called_once()

    session = await get_session("app2")
    assert session.status == Status.IDLE
    assert session.pid is None
