"""FastAPI application."""
import asyncio
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
from .process import _blocked_file, _done_file, _retry_at


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _reschedule_waiting_sessions()
    yield


async def _reschedule_waiting_sessions() -> None:
    from datetime import datetime, timezone
    for session in await sess.list_waiting_sessions():
        if session.retry_at and session.session_id:
            reset_time = datetime.fromisoformat(session.retry_at)
            asyncio.create_task(_retry_at(
                session.name, session.session_id, session.repo_path,
                session.base_prompt, session.last_extra_prompt, reset_time,
            ))


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


def _with_extras(session) -> dict:
    blocked = _blocked_file(session.name)
    done = _done_file(session.name)
    return {
        **session.__dict__,
        "blocked_reason": blocked.read_text().strip() if blocked.exists() else None,
        "done_summary": done.read_text().strip() if done.exists() else None,
    }


@app.get("/sessions")
async def list_sessions():
    return [_with_extras(s) for s in await sess.list_sessions()]


@app.get("/sessions/{name}")
async def get_session(name: str):
    session = await sess.get_session(name)
    if not session:
        raise HTTPException(404, f"Session '{name}' not found")
    return _with_extras(session)


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
    pid = await proc.start_run(name, session.session_id, session.repo_path, session.base_prompt, body.extra_prompt)
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
