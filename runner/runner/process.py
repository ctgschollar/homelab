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
    repo_path: str,
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
        "--verbose",
        "--print", prompt,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    db = await get_db()
    try:
        await db.execute(
            "UPDATE sessions SET status = ?, pid = ?, updated_at = ? WHERE name = ?",
            (Status.RUNNING.value, proc.pid, _now(), name),
        )
        await db.commit()
    finally:
        await db.close()

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

    db = await get_db()
    try:
        await db.execute(
            "UPDATE sessions SET status = ?, pid = NULL, updated_at = ? WHERE name = ?",
            (status.value, _now(), name),
        )
        await db.commit()
    finally:
        await db.close()


async def stop_run(name: str, pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    db = await get_db()
    try:
        await db.execute(
            "UPDATE sessions SET status = ?, pid = NULL, updated_at = ? WHERE name = ?",
            (Status.IDLE.value, _now(), name),
        )
        await db.commit()
    finally:
        await db.close()
