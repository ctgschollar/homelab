# Claude Runner Revamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the queue-based claude runner with a conversation-centric Python service that lets you build context interactively, then hand off to autonomous execution without losing that context.

**Architecture:** A FastAPI service manages named sessions backed by SQLite. The CLI's `new` command launches `claude` interactively (full TTY), captures the resulting session ID from `~/.claude/projects/`, and registers it with the API. The `run` command tells the API to resume that session autonomously via `claude --resume --dangerously-skip-permissions`, streaming output to a JSONL log file that the API serves over SSE.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, aiosqlite, sse-starlette, Typer, httpx, Hatch (build/publish), pytest + pytest-asyncio (tests), Ansible (deployment), systemd.

**Spec:** `docs/superpowers/specs/2026-05-03-claude-runner-revamp-design.md`

---

## File Map

```
runner/                          ← new top-level directory
├── pyproject.toml               ← hatch project, entry points, test config
└── runner/
    ├── __init__.py
    ├── main.py                  ← FastAPI app, lifespan, all route handlers
    ├── db.py                    ← SQLite init + async connection helper
    ├── models.py                ← Session dataclass, Status enum
    ├── sessions.py              ← CRUD: create/get/list/update/delete session rows
    ├── process.py               ← spawn claude --resume subprocess, stream to log file
    ├── logs.py                  ← read last-N lines, async SSE line generator
    └── cli.py                   ← Typer CLI: new/run/stop/logs/list/remove/set-prompt

runner/tests/
    ├── conftest.py              ← tmp_path DB fixture, async test client fixture
    ├── test_sessions.py         ← CRUD unit tests
    ├── test_logs.py             ← read/stream tests
    ├── test_process.py          ← subprocess spawn/stop tests (mock subprocess)
    └── test_api.py              ← full HTTP round-trip tests via AsyncClient

ansible/
├── inventory.yml                ← add new runner VM host
├── deploy-runner.yml            ← new playbook targeting runner host
└── roles/runner/
    ├── defaults/main.yml        ← runner_base_dir, runner_port, gitea_pypi_*
    ├── tasks/main.yml           ← install deps, pip install, dirs, systemd
    ├── handlers/main.yml        ← reload systemd, restart service
    └── templates/
        └── claude-runner-api.service.j2
```

---

## Task 1: Project scaffold

**Files:**
- Create: `runner/pyproject.toml`
- Create: `runner/runner/__init__.py`
- Create: `runner/runner/main.py` (stub)
- Create: `runner/runner/db.py` (stub)
- Create: `runner/runner/models.py` (stub)
- Create: `runner/runner/sessions.py` (stub)
- Create: `runner/runner/process.py` (stub)
- Create: `runner/runner/logs.py` (stub)
- Create: `runner/runner/cli.py` (stub)
- Create: `runner/tests/__init__.py`

- [ ] **Step 1: Create the hatch project**

```bash
mkdir -p runner/runner runner/tests
touch runner/runner/__init__.py runner/tests/__init__.py
```

- [ ] **Step 2: Write pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "claude-runner"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
    "typer[all]>=0.12",
    "httpx>=0.27",
    "aiosqlite>=0.20",
    "sse-starlette>=2.1",
]

[project.scripts]
claude-runner     = "runner.cli:app"
claude-runner-api = "runner.main:main"

[tool.hatch.envs.default]
dependencies = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
]

[tool.hatch.envs.default.scripts]
test = "pytest {args}"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Write stub modules** (each file just has a module docstring, no logic yet)

`runner/runner/models.py`:
```python
"""Session model and status enum."""
```

`runner/runner/db.py`:
```python
"""SQLite connection and schema init."""
```

`runner/runner/sessions.py`:
```python
"""Session CRUD operations."""
```

`runner/runner/process.py`:
```python
"""Async subprocess management for autonomous claude runs."""
```

`runner/runner/logs.py`:
```python
"""Log file I/O and SSE streaming."""
```

`runner/runner/main.py`:
```python
"""FastAPI application."""
```

`runner/runner/cli.py`:
```python
"""Typer CLI entry point."""
```

- [ ] **Step 4: Verify hatch can install the project**

```bash
cd runner
hatch env create
hatch run python -c "import runner; print('ok')"
```
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add runner/
git commit -m "feat: scaffold claude-runner python project"
```

---

## Task 2: Models and DB layer

**Files:**
- Modify: `runner/runner/models.py`
- Modify: `runner/runner/db.py`
- Create: `runner/tests/conftest.py`
- Create: `runner/tests/test_sessions.py` (first test to drive DB init)

- [ ] **Step 1: Write the failing test for DB init**

`runner/tests/conftest.py`:
```python
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
```

`runner/tests/test_sessions.py` (first test only):
```python
import pytest
from runner.db import init_db, get_db_path


async def test_init_db_creates_table(tmp_env):
    await init_db()
    import aiosqlite
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        row = await cursor.fetchone()
    assert row is not None, "sessions table should exist after init_db()"
```

- [ ] **Step 2: Run test — expect failure**

```bash
cd runner
hatch run pytest tests/test_sessions.py::test_init_db_creates_table -v
```
Expected: `FAILED` — `ImportError` or `AttributeError` (db not implemented yet)

- [ ] **Step 3: Implement models.py**

```python
"""Session model and status enum."""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Status(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Session:
    name: str
    repo_path: str
    session_id: Optional[str]
    status: Status
    base_prompt: Optional[str]
    pid: Optional[int]
    created_at: str
    updated_at: str
```

- [ ] **Step 4: Implement db.py**

```python
"""SQLite connection and schema init."""
import os
from pathlib import Path
import aiosqlite


def get_db_path() -> Path:
    return Path(os.environ.get("CLAUDE_RUNNER_BASE_DIR", "/opt/claude-runner")) / "runner.db"


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(get_db_path())
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                name        TEXT PRIMARY KEY,
                repo_path   TEXT NOT NULL,
                session_id  TEXT,
                status      TEXT NOT NULL DEFAULT 'idle',
                base_prompt TEXT,
                pid         INTEGER,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        await db.commit()
```

- [ ] **Step 5: Run test — expect pass**

```bash
cd runner
hatch run pytest tests/test_sessions.py::test_init_db_creates_table -v
```
Expected: `PASSED`

- [ ] **Step 6: Commit**

```bash
git add runner/
git commit -m "feat: add Session model and SQLite DB layer"
```

---

## Task 3: Sessions CRUD

**Files:**
- Modify: `runner/runner/sessions.py`
- Modify: `runner/tests/test_sessions.py`

- [ ] **Step 1: Write failing tests for CRUD**

Append to `runner/tests/test_sessions.py`:
```python
from runner.db import init_db
from runner.sessions import create_session, get_session, list_sessions, update_session, delete_session
from runner.models import Status


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
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd runner
hatch run pytest tests/test_sessions.py -v -k "not test_init_db"
```
Expected: `FAILED` — `ImportError` (sessions.py is empty)

- [ ] **Step 3: Implement sessions.py**

```python
"""Session CRUD operations."""
from datetime import datetime, timezone
from typing import Optional

from .db import get_db
from .models import Session, Status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_session(row) -> Session:
    return Session(
        name=row["name"],
        repo_path=row["repo_path"],
        session_id=row["session_id"],
        status=Status(row["status"]),
        base_prompt=row["base_prompt"],
        pid=row["pid"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_session(
    name: str,
    repo_path: str,
    session_id: Optional[str] = None,
    base_prompt: Optional[str] = None,
) -> Session:
    now = _now()
    async with await get_db() as db:
        await db.execute(
            """INSERT INTO sessions
               (name, repo_path, session_id, status, base_prompt, pid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
            (name, repo_path, session_id, Status.IDLE.value, base_prompt, now, now),
        )
        await db.commit()
    return Session(
        name=name, repo_path=repo_path, session_id=session_id,
        status=Status.IDLE, base_prompt=base_prompt, pid=None,
        created_at=now, updated_at=now,
    )


async def get_session(name: str) -> Optional[Session]:
    async with await get_db() as db:
        cursor = await db.execute("SELECT * FROM sessions WHERE name = ?", (name,))
        row = await cursor.fetchone()
    return _row_to_session(row) if row else None


async def list_sessions() -> list[Session]:
    async with await get_db() as db:
        cursor = await db.execute("SELECT * FROM sessions ORDER BY created_at DESC")
        rows = await cursor.fetchall()
    return [_row_to_session(r) for r in rows]


async def update_session(name: str, **kwargs) -> Optional[Session]:
    if not kwargs:
        return await get_session(name)
    kwargs["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [name]
    async with await get_db() as db:
        await db.execute(f"UPDATE sessions SET {set_clause} WHERE name = ?", values)
        await db.commit()
    return await get_session(name)


async def delete_session(name: str) -> bool:
    async with await get_db() as db:
        cursor = await db.execute("DELETE FROM sessions WHERE name = ?", (name,))
        await db.commit()
        return cursor.rowcount > 0
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd runner
hatch run pytest tests/test_sessions.py -v
```
Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add runner/
git commit -m "feat: add sessions CRUD layer"
```

---

## Task 4: Log I/O

**Files:**
- Modify: `runner/runner/logs.py`
- Create: `runner/tests/test_logs.py`

- [ ] **Step 1: Write failing tests**

`runner/tests/test_logs.py`:
```python
import asyncio
import json
import pytest
from runner.logs import read_last_n, stream_log, get_base_dir


@pytest.fixture(autouse=True)
def setup_logs_dir(tmp_env):
    (tmp_env / "logs").mkdir(exist_ok=True)


def _write_log(tmp_env, name: str, lines: list[str]):
    log_path = tmp_env / "logs" / f"{name}.jsonl"
    log_path.write_text("\n".join(lines) + "\n")


def test_read_last_n_returns_lines(tmp_env):
    _write_log(tmp_env, "myapp", [json.dumps({"type": "assistant", "n": i}) for i in range(20)])
    lines = read_last_n("myapp", n=5)
    assert len(lines) == 5
    assert json.loads(lines[-1])["n"] == 19


def test_read_last_n_missing_file_returns_empty(tmp_env):
    lines = read_last_n("noexist", n=10)
    assert lines == []


def test_read_last_n_fewer_than_n_lines(tmp_env):
    _write_log(tmp_env, "small", ["line1", "line2"])
    lines = read_last_n("small", n=100)
    assert lines == ["line1", "line2"]


async def test_stream_log_yields_existing_lines(tmp_env):
    _write_log(tmp_env, "stream-test", ["line1", "line2", "line3"])
    received = []

    async def collect():
        async for line in stream_log("stream-test"):
            received.append(line)
            if len(received) == 3:
                break

    await asyncio.wait_for(collect(), timeout=2.0)
    assert received == ["line1", "line2", "line3"]


async def test_stream_log_picks_up_new_lines(tmp_env):
    log_path = tmp_env / "logs" / "live.jsonl"
    log_path.write_text("")
    received = []

    async def collect():
        async for line in stream_log("live"):
            received.append(line)
            if len(received) == 2:
                break

    async def writer():
        await asyncio.sleep(0.1)
        with log_path.open("a") as f:
            f.write("first\n")
        await asyncio.sleep(0.1)
        with log_path.open("a") as f:
            f.write("second\n")

    await asyncio.gather(
        asyncio.wait_for(collect(), timeout=3.0),
        writer(),
    )
    assert received == ["first", "second"]
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd runner
hatch run pytest tests/test_logs.py -v
```
Expected: `FAILED` — `ImportError`

- [ ] **Step 3: Implement logs.py**

```python
"""Log file I/O and SSE streaming."""
import asyncio
import os
from pathlib import Path
from typing import AsyncGenerator


def get_base_dir() -> Path:
    return Path(os.environ.get("CLAUDE_RUNNER_BASE_DIR", "/opt/claude-runner"))


def log_path(name: str) -> Path:
    return get_base_dir() / "logs" / f"{name}.jsonl"


def read_last_n(name: str, n: int = 100) -> list[str]:
    path = log_path(name)
    if not path.exists():
        return []
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    return lines[-n:]


async def stream_log(name: str) -> AsyncGenerator[str, None]:
    path = log_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    with path.open("r") as f:
        for line in f:
            line = line.rstrip("\n")
            if line:
                yield line
        while True:
            line = f.readline()
            if line:
                line = line.rstrip("\n")
                if line:
                    yield line
            else:
                await asyncio.sleep(0.1)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd runner
hatch run pytest tests/test_logs.py -v
```
Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add runner/
git commit -m "feat: add log I/O and SSE streaming layer"
```

---

## Task 5: Process management

**Files:**
- Modify: `runner/runner/process.py`
- Create: `runner/tests/test_process.py`

- [ ] **Step 1: Write failing tests**

`runner/tests/test_process.py`:
```python
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

    with patch("runner.process.asyncio.create_subprocess_exec", return_value=mock_proc):
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
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd runner
hatch run pytest tests/test_process.py -v
```
Expected: `FAILED` — `ImportError`

- [ ] **Step 3: Implement process.py**

```python
"""Async subprocess management for autonomous claude runs."""
import asyncio
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import get_db
from .models import Status


def get_base_dir() -> Path:
    return Path(os.environ.get("CLAUDE_RUNNER_BASE_DIR", "/opt/claude-runner"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_prompt(base_prompt: Optional[str], extra_prompt: Optional[str]) -> str:
    parts = [p for p in [base_prompt, extra_prompt] if p]
    if not parts:
        return "Continue with the task we discussed."
    return "\n\n---\n\n".join(parts)


async def start_run(
    name: str,
    session_id: str,
    base_prompt: Optional[str],
    extra_prompt: Optional[str],
) -> int:
    log_file = get_base_dir() / "logs" / f"{name}.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    prompt = build_prompt(base_prompt, extra_prompt)
    cmd = [
        "claude",
        "--resume", session_id,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--print", prompt,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async with await get_db() as db:
        await db.execute(
            "UPDATE sessions SET status = ?, pid = ?, updated_at = ? WHERE name = ?",
            (Status.RUNNING.value, proc.pid, _now(), name),
        )
        await db.commit()

    asyncio.create_task(_stream_to_file(proc, log_file, name))
    return proc.pid


async def _stream_to_file(
    proc: asyncio.subprocess.Process,
    log_file: Path,
    name: str,
) -> None:
    with log_file.open("ab") as f:
        async for line in proc.stdout:
            f.write(line)
            f.flush()

    await proc.wait()
    status = Status.DONE if proc.returncode == 0 else Status.ERROR

    async with await get_db() as db:
        await db.execute(
            "UPDATE sessions SET status = ?, pid = NULL, updated_at = ? WHERE name = ?",
            (status.value, _now(), name),
        )
        await db.commit()


async def stop_run(name: str, pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    async with await get_db() as db:
        await db.execute(
            "UPDATE sessions SET status = ?, pid = NULL, updated_at = ? WHERE name = ?",
            (Status.IDLE.value, _now(), name),
        )
        await db.commit()
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd runner
hatch run pytest tests/test_process.py -v
```
Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add runner/
git commit -m "feat: add async subprocess management for autonomous runs"
```

---

## Task 6: FastAPI app

**Files:**
- Modify: `runner/runner/main.py`
- Create: `runner/tests/test_api.py`

- [ ] **Step 1: Write failing API tests**

`runner/tests/test_api.py`:
```python
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
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd runner
hatch run pytest tests/test_api.py -v
```
Expected: `FAILED` — `ImportError` (main.py is empty)

- [ ] **Step 3: Implement main.py**

```python
"""FastAPI application."""
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .db import init_db
from .models import Status
from . import sessions as sess
from . import process as proc
from . import logs as log_io


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Claude Runner", lifespan=lifespan)


class CreateSessionBody(BaseModel):
    name: str
    repo_path: str
    session_id: Optional[str] = None
    base_prompt: Optional[str] = None


class UpdateSessionBody(BaseModel):
    base_prompt: Optional[str] = None
    session_id: Optional[str] = None


class RunBody(BaseModel):
    extra_prompt: Optional[str] = None


@app.post("/sessions", status_code=201)
async def create_session(body: CreateSessionBody):
    if await sess.get_session(body.name):
        raise HTTPException(409, f"Session '{body.name}' already exists")
    return await sess.create_session(body.name, body.repo_path, body.session_id, body.base_prompt)


@app.get("/sessions")
async def list_sessions():
    return await sess.list_sessions()


@app.get("/sessions/{name}")
async def get_session(name: str):
    session = await sess.get_session(name)
    if not session:
        raise HTTPException(404, f"Session '{name}' not found")
    return session


@app.patch("/sessions/{name}")
async def update_session(name: str, body: UpdateSessionBody):
    if not await sess.get_session(name):
        raise HTTPException(404, f"Session '{name}' not found")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return await sess.update_session(name, **updates)


@app.delete("/sessions/{name}", status_code=204)
async def delete_session(name: str):
    if not await sess.delete_session(name):
        raise HTTPException(404, f"Session '{name}' not found")


@app.post("/sessions/{name}/run", status_code=202)
async def run_session(name: str, body: RunBody = RunBody()):
    session = await sess.get_session(name)
    if not session:
        raise HTTPException(404, f"Session '{name}' not found")
    if session.status == Status.RUNNING:
        raise HTTPException(409, f"Session '{name}' is already running")
    if not session.session_id:
        raise HTTPException(422, f"Session '{name}' has no Claude session ID — use PATCH to set one")
    pid = await proc.start_run(name, session.session_id, session.base_prompt, body.extra_prompt)
    return {"pid": pid}


@app.post("/sessions/{name}/stop", status_code=202)
async def stop_session(name: str):
    session = await sess.get_session(name)
    if not session:
        raise HTTPException(404, f"Session '{name}' not found")
    if session.status != Status.RUNNING or not session.pid:
        raise HTTPException(409, f"Session '{name}' is not running")
    await proc.stop_run(name, session.pid)
    return {"stopped": True}


@app.get("/sessions/{name}/logs")
async def get_logs(name: str, n: int = 100):
    if not await sess.get_session(name):
        raise HTTPException(404, f"Session '{name}' not found")
    return {"lines": log_io.read_last_n(name, n)}


@app.get("/sessions/{name}/logs/stream")
async def stream_logs(name: str):
    if not await sess.get_session(name):
        raise HTTPException(404, f"Session '{name}' not found")

    async def event_generator():
        async for line in log_io.stream_log(name):
            yield {"data": line}

    return EventSourceResponse(event_generator())


def main():
    import uvicorn
    port = int(os.environ.get("CLAUDE_RUNNER_PORT", "8080"))
    uvicorn.run("runner.main:app", host="0.0.0.0", port=port, reload=False)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd runner
hatch run pytest tests/test_api.py -v
```
Expected: all `PASSED`

- [ ] **Step 5: Run full test suite**

```bash
cd runner
hatch run pytest -v
```
Expected: all `PASSED`

- [ ] **Step 6: Commit**

```bash
git add runner/
git commit -m "feat: add FastAPI app with session and log endpoints"
```

---

## Task 7: Typer CLI

**Files:**
- Modify: `runner/runner/cli.py`
- Create: `runner/tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

`runner/tests/test_cli.py`:
```python
import json
import pytest
from pathlib import Path
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock
from runner.cli import app, _encode_path, _capture_session_id

runner_cli = CliRunner()


def test_encode_path():
    assert _encode_path("/home/claude/repos/myapp") == "-home-claude-repos-myapp"


def test_capture_session_id_finds_most_recent(tmp_path):
    encoded = _encode_path(str(tmp_path / "myrepo"))
    projects_dir = Path.home() / ".claude" / "projects" / encoded
    projects_dir.mkdir(parents=True, exist_ok=True)
    older = projects_dir / "old-uuid.jsonl"
    newer = projects_dir / "new-uuid.jsonl"
    older.write_text("")
    import time; time.sleep(0.01)
    newer.write_text("")

    result = _capture_session_id(str(tmp_path / "myrepo"))
    assert result == "new-uuid"


def test_capture_session_id_missing_dir(tmp_path):
    result = _capture_session_id(str(tmp_path / "nonexistent-repo"))
    assert result is None


def test_list_command_no_sessions():
    with patch("runner.cli._api") as mock_api:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value.json.return_value = []
        mock_client.get.return_value.raise_for_status = MagicMock()
        mock_api.return_value = mock_client

        result = runner_cli.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No sessions" in result.output


def test_list_command_shows_sessions():
    with patch("runner.cli._api") as mock_api:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value.json.return_value = [
            {"name": "myapp", "status": "idle", "pid": None, "repo_path": "/repos/myapp"}
        ]
        mock_client.get.return_value.raise_for_status = MagicMock()
        mock_api.return_value = mock_client

        result = runner_cli.invoke(app, ["list"])
    assert "myapp" in result.output
    assert "idle" in result.output


def test_run_command_calls_api():
    with patch("runner.cli._api") as mock_api:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value.status_code = 202
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_api.return_value = mock_client

        result = runner_cli.invoke(app, ["run", "myapp", "do the thing"])
    assert result.exit_code == 0
    assert "myapp" in result.output


def test_print_log_line_parses_assistant():
    from runner.cli import _print_log_line
    from io import StringIO
    import sys

    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello there"}]}
    })
    # Should not raise; output contains text
    captured = []
    with patch("runner.cli.typer.echo", side_effect=lambda s, **kw: captured.append(s)):
        _print_log_line(line)
    assert any("hello there" in str(c) for c in captured)
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd runner
hatch run pytest tests/test_cli.py -v
```
Expected: `FAILED` — `ImportError`

- [ ] **Step 3: Implement cli.py**

```python
"""Typer CLI entry point."""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer

app = typer.Typer(help="Claude Runner — manage named Claude Code sessions", add_completion=False)

API_URL = os.environ.get("CLAUDE_RUNNER_API", "http://localhost:8080")


def _api() -> httpx.Client:
    return httpx.Client(base_url=API_URL, timeout=30)


def _encode_path(repo_path: str) -> str:
    """Encode repo path to match Claude Code's ~/.claude/projects/ dir name.

    Claude Code encodes /home/user/repo as -home-user-repo (replacing / with -).
    """
    return repo_path.replace("/", "-")


def _capture_session_id(repo_path: str) -> Optional[str]:
    """Find the most recently modified .jsonl session file under ~/.claude/projects/<encoded>/."""
    encoded = _encode_path(repo_path)
    projects_dir = Path.home() / ".claude" / "projects" / encoded
    if not projects_dir.exists():
        return None
    session_files = sorted(
        projects_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return session_files[0].stem if session_files else None


def _print_log_line(line: str) -> None:
    """Parse a stream-json log line and print human-readable output."""
    try:
        obj = json.loads(line)
        t = obj.get("type")
        if t == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    typer.echo(block["text"], nl=False)
                elif block.get("type") == "tool_use":
                    snippet = json.dumps(block["input"])[:200]
                    typer.echo(f"\n[tool: {block['name']}] {snippet}")
        elif t == "result":
            cost = obj.get("cost_usd", 0) or 0
            typer.echo(f"\n[session] turns={obj.get('num_turns')} cost=${cost:.4f}")
    except (json.JSONDecodeError, KeyError):
        typer.echo(line)


@app.command()
def new(
    name: str = typer.Argument(..., help="Unique name for this session"),
    repo: str = typer.Argument(..., help="Absolute path to the git repo"),
    base_prompt: Optional[str] = typer.Option(
        None, "--base-prompt", "-p",
        help="Instructions injected on every autonomous run",
    ),
):
    """Start an interactive Claude session, then register it with the API."""
    repo_path = str(Path(repo).resolve())
    if not Path(repo_path).is_dir():
        typer.echo(f"Error: repo path does not exist: {repo_path}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Starting Claude session '{name}' in {repo_path} ...")
    subprocess.run(["claude"], cwd=repo_path)

    session_id = _capture_session_id(repo_path)
    if not session_id:
        typer.echo(
            "Warning: could not capture session ID. Use 'claude-runner set-prompt' to register manually.",
            err=True,
        )

    with _api() as client:
        r = client.post("/sessions", json={
            "name": name,
            "repo_path": repo_path,
            "session_id": session_id,
            "base_prompt": base_prompt,
        })
        if r.status_code == 409:
            typer.echo(f"Error: session '{name}' already exists", err=True)
            raise typer.Exit(1)
        r.raise_for_status()

    sid_display = f" (session_id: {session_id})" if session_id else " (no session ID captured)"
    typer.echo(f"Registered session '{name}'{sid_display}")


@app.command()
def run(
    name: str = typer.Argument(..., help="Session name"),
    extra_prompt: Optional[str] = typer.Argument(None, help="Extra instructions appended to base prompt"),
):
    """Resume a session autonomously."""
    with _api() as client:
        r = client.post(f"/sessions/{name}/run", json={"extra_prompt": extra_prompt})
        if r.status_code == 404:
            typer.echo(f"Error: session '{name}' not found", err=True)
            raise typer.Exit(1)
        r.raise_for_status()
    typer.echo(f"Started autonomous run for '{name}'. Follow with: claude-runner logs {name} --follow")


@app.command()
def stop(name: str = typer.Argument(..., help="Session name")):
    """Kill a running autonomous session."""
    with _api() as client:
        r = client.post(f"/sessions/{name}/stop")
        if r.status_code == 404:
            typer.echo(f"Error: session '{name}' not found", err=True)
            raise typer.Exit(1)
        r.raise_for_status()
    typer.echo(f"Stopped '{name}'")


@app.command()
def logs(
    name: str = typer.Argument(..., help="Session name"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream live output"),
    n: int = typer.Option(100, "--lines", "-n", help="Number of recent lines to show"),
):
    """Show log output for a session."""
    if follow:
        with httpx.stream("GET", f"{API_URL}/sessions/{name}/logs/stream", timeout=None) as r:
            if r.status_code == 404:
                typer.echo(f"Error: session '{name}' not found", err=True)
                raise typer.Exit(1)
            for line in r.iter_lines():
                if line.startswith("data: "):
                    _print_log_line(line[6:])
    else:
        with _api() as client:
            r = client.get(f"/sessions/{name}/logs", params={"n": n})
            if r.status_code == 404:
                typer.echo(f"Error: session '{name}' not found", err=True)
                raise typer.Exit(1)
            r.raise_for_status()
            for line in r.json()["lines"]:
                _print_log_line(line)


@app.command(name="list")
def list_sessions():
    """Show all sessions."""
    with _api() as client:
        r = client.get("/sessions")
        r.raise_for_status()
    sessions = r.json()
    if not sessions:
        typer.echo("No sessions. Create one with: claude-runner new <name> <repo>")
        return
    fmt = "%-20s %-10s %-6s %s"
    typer.echo(fmt % ("NAME", "STATUS", "PID", "REPO"))
    typer.echo(fmt % ("----", "------", "---", "----"))
    for s in sessions:
        typer.echo(fmt % (s["name"], s["status"], s["pid"] or "-", s["repo_path"]))


@app.command()
def remove(name: str = typer.Argument(..., help="Session name")):
    """Delete a session."""
    with _api() as client:
        r = client.delete(f"/sessions/{name}")
        if r.status_code == 404:
            typer.echo(f"Error: session '{name}' not found", err=True)
            raise typer.Exit(1)
        r.raise_for_status()
    typer.echo(f"Removed session '{name}'")


@app.command()
def set_prompt(
    name: str = typer.Argument(..., help="Session name"),
    prompt: str = typer.Argument(..., help="Base prompt injected on every autonomous run"),
):
    """Update the base prompt for a session."""
    with _api() as client:
        r = client.patch(f"/sessions/{name}", json={"base_prompt": prompt})
        if r.status_code == 404:
            typer.echo(f"Error: session '{name}' not found", err=True)
            raise typer.Exit(1)
        r.raise_for_status()
    typer.echo(f"Updated base prompt for '{name}'")
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd runner
hatch run pytest tests/test_cli.py -v
```
Expected: all `PASSED`

- [ ] **Step 5: Run full test suite**

```bash
cd runner
hatch run pytest -v
```
Expected: all `PASSED`

- [ ] **Step 6: Commit**

```bash
git add runner/
git commit -m "feat: add Typer CLI for claude-runner"
```

---

## Task 8: Gitea PyPI publish setup

**Files:**
- Modify: `runner/pyproject.toml`

- [ ] **Step 1: Add Gitea publish config to pyproject.toml**

Add to the end of `runner/pyproject.toml`:
```toml
[tool.hatch.publish.index]
url = "https://gitea.schollar.dev/api/packages/chris/pypi"
```

- [ ] **Step 2: Build the package**

```bash
cd runner
hatch build
```
Expected: `dist/claude_runner-0.1.0.tar.gz` and `dist/claude_runner-0.1.0-py3-none-any.whl` created

- [ ] **Step 3: Publish to Gitea**

Generate a Gitea API token at `https://gitea.schollar.dev/user/settings/applications` with `write:package` scope, then:

```bash
cd runner
HATCH_INDEX_USER=chris HATCH_INDEX_AUTH=<your-token> hatch publish
```
Expected: `Successfully uploaded claude_runner-0.1.0`

- [ ] **Step 4: Verify package is available**

```bash
pip index versions claude-runner \
  --extra-index-url https://gitea.schollar.dev/api/packages/chris/pypi/simple/ \
  --no-deps 2>&1 | grep claude-runner
```
Expected: `claude-runner (0.1.0)`

- [ ] **Step 5: Commit**

```bash
git add runner/pyproject.toml
git commit -m "feat: add Gitea PyPI publish config"
```

---

## Task 9: Ansible role and playbook

**Files:**
- Modify: `ansible/inventory.yml`
- Create: `ansible/deploy-runner.yml`
- Create: `ansible/roles/runner/defaults/main.yml`
- Create: `ansible/roles/runner/tasks/main.yml`
- Create: `ansible/roles/runner/handlers/main.yml`
- Create: `ansible/roles/runner/templates/claude-runner-api.service.j2`

- [ ] **Step 1: Add new VM to inventory.yml**

Add the new host under `proxmox_vms:` in `ansible/inventory.yml`:
```yaml
        claude2:
          ansible_host: 192.168.3.XX   # replace with actual IP of new VM
```
(Replace `XX` with the actual IP assigned to the new VM.)

- [ ] **Step 2: Create role defaults**

`ansible/roles/runner/defaults/main.yml`:
```yaml
---
runner_base_dir: /opt/claude-runner
runner_port: 8080
claude_user: claude
claude_user_home: /home/claude

# Gitea PyPI registry settings
gitea_pypi_url: https://gitea.schollar.dev/api/packages/chris/pypi/simple/
gitea_pypi_user: chris
gitea_pypi_token: ""   # set via --extra-vars or ansible-vault
```

- [ ] **Step 3: Create role tasks**

`ansible/roles/runner/tasks/main.yml`:
```yaml
---
- name: Create claude user
  user:
    name: "{{ claude_user }}"
    home: "{{ claude_user_home }}"
    shell: /bin/bash
    state: present
    create_home: yes

- name: Add claude user to docker group
  user:
    name: "{{ claude_user }}"
    groups: docker
    append: yes

- name: Install system dependencies
  apt:
    name:
      - python3
      - python3-pip
      - nodejs
      - npm
      - git
    state: present
    update_cache: yes

- name: Install Claude Code globally
  npm:
    name: "@anthropic-ai/claude-code"
    global: yes

- name: Configure pip to use Gitea PyPI index
  ini_file:
    path: /etc/pip.conf
    section: global
    option: extra-index-url
    value: "https://{{ gitea_pypi_user }}:{{ gitea_pypi_token }}@{{ gitea_pypi_url | urlsplit('hostname') }}/api/packages/chris/pypi/simple/"
    create: yes
    owner: root
    group: root
    mode: '0644'

- name: Install claude-runner from Gitea PyPI
  pip:
    name: claude-runner
    state: latest
    extra_args: "--extra-index-url https://{{ gitea_pypi_user }}:{{ gitea_pypi_token }}@{{ gitea_pypi_url | urlsplit('hostname') }}/api/packages/chris/pypi/simple/"

- name: Create base directories
  file:
    path: "{{ item }}"
    state: directory
    owner: "{{ claude_user }}"
    group: "{{ claude_user }}"
    mode: '0755'
  loop:
    - "{{ runner_base_dir }}"
    - "{{ runner_base_dir }}/logs"

- name: Install systemd service
  template:
    src: claude-runner-api.service.j2
    dest: /etc/systemd/system/claude-runner-api.service
    owner: root
    group: root
    mode: '0644'
  notify:
    - reload systemd
    - restart claude-runner-api

- name: Enable and start service
  systemd:
    name: claude-runner-api
    enabled: yes
    state: started
```

- [ ] **Step 4: Create handlers**

`ansible/roles/runner/handlers/main.yml`:
```yaml
---
- name: reload systemd
  systemd:
    daemon_reload: yes

- name: restart claude-runner-api
  systemd:
    name: claude-runner-api
    state: restarted
```

- [ ] **Step 5: Create systemd unit template**

`ansible/roles/runner/templates/claude-runner-api.service.j2`:
```ini
[Unit]
Description=Claude Runner API
After=network.target

[Service]
User={{ claude_user }}
Environment=PATH={{ claude_user_home }}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=CLAUDE_RUNNER_BASE_DIR={{ runner_base_dir }}
Environment=CLAUDE_RUNNER_PORT={{ runner_port }}
ExecStart=/usr/local/bin/claude-runner-api
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 6: Create the playbook**

`ansible/deploy-runner.yml`:
```yaml
---
- name: Deploy claude-runner API service
  hosts: claude2
  become: true

  vars:
    required_tools: [tea, jq]

  pre_tasks:
    - name: Install required tools
      import_role:
        name: tools

  roles:
    - runner
```

- [ ] **Step 7: Run the playbook against the new VM**

```bash
cd ansible
ansible-playbook deploy-runner.yml -i inventory.yml \
  --extra-vars "gitea_pypi_token=<your-token>"
```
Expected: play runs with no failures, `claude-runner-api.service` is active

- [ ] **Step 8: Verify the service is running**

SSH into the new VM and check:
```bash
ssh root@<new-vm-ip>
systemctl status claude-runner-api
curl http://localhost:8080/sessions
```
Expected: service `active (running)`, curl returns `[]`

- [ ] **Step 9: Verify CLI works from the new VM**

```bash
CLAUDE_RUNNER_API=http://localhost:8080 claude-runner list
```
Expected: `No sessions. Create one with: claude-runner new <name> <repo>`

- [ ] **Step 10: Commit**

```bash
git add ansible/
git commit -m "feat: add ansible role and playbook for claude-runner API service"
```

---

## Self-Review Checklist

**Spec coverage:**

| Spec requirement | Covered by |
|---|---|
| Named sessions, freely named | Task 2–3: `name` is primary key |
| `new` launches interactive claude + captures session ID | Task 7: `new` command |
| `run` resumes autonomously with `--dangerously-skip-permissions` | Task 5: `start_run()` |
| Base prompt + per-run extra prompt | Task 5: `build_prompt()` |
| SSE log streaming | Task 4: `stream_log()`, Task 6: `/logs/stream` |
| SQLite state storage | Task 2–3 |
| FastAPI with full CRUD + run/stop/logs endpoints | Task 6 |
| Typer CLI | Task 7 |
| Hatch project + Gitea PyPI publish | Task 1 + Task 8 |
| Ansible role deploying to new VM | Task 9 |
| Existing role untouched | No modifications to `ansible/roles/claude-runner/` |
| `session_id` captured from `~/.claude/projects/` | Task 7: `_capture_session_id()` |

All spec requirements are covered. No gaps found.
