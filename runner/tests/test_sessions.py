import pytest
from runner.db import init_db, get_db_path
from runner.sessions import create_session, get_session, list_sessions, update_session, delete_session
from runner.models import Status


async def test_init_db_creates_table(tmp_env):
    await init_db()
    import aiosqlite
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        row = await cursor.fetchone()
    assert row is not None, "sessions table should exist after init_db()"


@pytest.fixture(autouse=True)
async def db(tmp_env):
    await init_db()


async def test_create_and_get_session():
    s = await create_session("myapp", "/home/claude/repos/myapp")
    assert s.name == "myapp"
    assert s.repo_path == "/home/claude/repos/myapp"
    assert s.status == Status.IDLE
    assert s.session_id is None
    assert s.pid is None

    fetched = await get_session("myapp")
    assert fetched is not None
    assert fetched.name == "myapp"


async def test_get_missing_session_returns_none():
    result = await get_session("nonexistent")
    assert result is None


async def test_list_sessions():
    await create_session("a", "/repos/a")
    await create_session("b", "/repos/b")
    sessions = await list_sessions()
    names = [s.name for s in sessions]
    assert "a" in names
    assert "b" in names


async def test_update_session():
    await create_session("x", "/repos/x")
    updated = await update_session("x", session_id="abc-123", base_prompt="do the thing")
    assert updated.session_id == "abc-123"
    assert updated.base_prompt == "do the thing"

    refetched = await get_session("x")
    assert refetched.session_id == "abc-123"


async def test_delete_session():
    await create_session("del-me", "/repos/del-me")
    deleted = await delete_session("del-me")
    assert deleted is True
    assert await get_session("del-me") is None


async def test_delete_missing_returns_false():
    result = await delete_session("nope")
    assert result is False
