import pytest
from runner.models import Status


@pytest.fixture(autouse=True)
async def seeded_db(client):
    # client fixture in conftest.py already inits the DB
    pass


async def test_create_session(client):
    r = await client.post("/sessions", json={"name": "myapp", "repo_path": "/repos/myapp"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "myapp"
    assert body["status"] == "idle"


async def test_create_session_duplicate_returns_409(client):
    await client.post("/sessions", json={"name": "dup", "repo_path": "/repos/dup"})
    r = await client.post("/sessions", json={"name": "dup", "repo_path": "/repos/dup"})
    assert r.status_code == 409


async def test_get_session(client):
    await client.post("/sessions", json={"name": "get-me", "repo_path": "/repos/get-me"})
    r = await client.get("/sessions/get-me")
    assert r.status_code == 200
    assert r.json()["name"] == "get-me"


async def test_get_missing_session_returns_404(client):
    r = await client.get("/sessions/nope")
    assert r.status_code == 404


async def test_list_sessions(client):
    await client.post("/sessions", json={"name": "a", "repo_path": "/repos/a"})
    await client.post("/sessions", json={"name": "b", "repo_path": "/repos/b"})
    r = await client.get("/sessions")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert "a" in names and "b" in names


async def test_patch_session(client):
    await client.post("/sessions", json={"name": "p", "repo_path": "/repos/p"})
    r = await client.patch("/sessions/p", json={"base_prompt": "do the thing", "session_id": "abc"})
    assert r.status_code == 200
    body = r.json()
    assert body["base_prompt"] == "do the thing"
    assert body["session_id"] == "abc"


async def test_delete_session(client):
    await client.post("/sessions", json={"name": "d", "repo_path": "/repos/d"})
    r = await client.delete("/sessions/d")
    assert r.status_code == 204
    assert (await client.get("/sessions/d")).status_code == 404


async def test_run_session_no_session_id_returns_422(client):
    await client.post("/sessions", json={"name": "nosid", "repo_path": "/repos/nosid"})
    r = await client.post("/sessions/nosid/run", json={})
    assert r.status_code == 422


async def test_run_session_starts_process(client):
    from unittest.mock import AsyncMock, MagicMock, patch
    await client.post("/sessions", json={
        "name": "runner", "repo_path": "/repos/runner", "session_id": "uuid-99"
    })
    mock_proc = MagicMock()
    mock_proc.pid = 8888
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.__aiter__ = AsyncMock(return_value=iter([]))
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.returncode = 0

    with patch("runner.process.asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("runner.process.asyncio.create_task"):
            r = await client.post("/sessions/runner/run", json={"extra_prompt": "go"})

    assert r.status_code == 202
    assert r.json()["pid"] == 8888


async def test_stop_already_stopped_returns_409(client):
    await client.post("/sessions", json={
        "name": "idle-stop", "repo_path": "/r", "session_id": "x"
    })
    r = await client.post("/sessions/idle-stop/stop")
    assert r.status_code == 409


async def test_logs_endpoint(client, tmp_env):
    await client.post("/sessions", json={"name": "log-test", "repo_path": "/r"})
    log_path = tmp_env / "logs" / "log-test.jsonl"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text('{"type":"result"}\n{"type":"assistant"}\n')
    r = await client.get("/sessions/log-test/logs", params={"n": 1})
    assert r.status_code == 200
    assert len(r.json()["lines"]) == 1
