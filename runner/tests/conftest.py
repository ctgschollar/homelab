import os
import pytest
import pytest_asyncio
import runner.db as db_mod
import runner.logs as logs_mod
import runner.process as proc_mod
from httpx import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_RUNNER_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(db_mod, "get_db_path", lambda: tmp_path / "runner.db")
    monkeypatch.setattr(logs_mod, "get_base_dir", lambda: tmp_path)
    monkeypatch.setattr(proc_mod, "get_base_dir", lambda: tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def client(tmp_env):
    from runner.main import app
    from runner.db import init_db
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
