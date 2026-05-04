"""Async subprocess management for autonomous claude runs."""
import asyncio
import os
import re
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import get_db
from .models import Status

RATE_LIMIT_RE = re.compile(r"resets (\d{1,2}(?::\d{2})?(?:am|pm))\s+\(([^)]+)\)", re.IGNORECASE)


def get_base_dir() -> Path:
    return Path(os.environ.get("CLAUDE_RUNNER_BASE_DIR", "/opt/claude-runner"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blocked_file(name: str) -> Path:
    return get_base_dir() / "logs" / f"{name}.blocked"


def _done_file(name: str) -> Path:
    return get_base_dir() / "logs" / f"{name}.done"


def _parse_reset_time(time_str: str, tz_str: str) -> Optional[datetime]:
    try:
        tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        tz = timezone.utc

    now = datetime.now(tz)
    for fmt in ("%I%p", "%I:%M%p"):
        try:
            parsed = datetime.strptime(time_str.upper(), fmt).replace(
                year=now.year, month=now.month, day=now.day, tzinfo=tz
            )
            if parsed <= now:
                parsed = parsed.replace(day=now.day + 1)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def build_prompt(name: str, base_prompt: Optional[str], extra_prompt: Optional[str]) -> str:
    blocked_path = _blocked_file(name)
    done_path = _done_file(name)
    preamble = (
        "You are running autonomously with no human available. "
        "Do not ask clarifying questions — make reasonable assumptions and proceed. "
        f"When you have completed all work, write a brief summary to '{done_path}' and stop — do not wait for further input. "
        f"If you are truly blocked, write a brief reason to '{blocked_path}' and stop."
    )
    parts = [p for p in [base_prompt, extra_prompt] if p] or ["Continue with the task we discussed."]
    return "\n\n---\n\n".join([preamble] + parts)


async def start_run(
    name: str,
    session_id: str,
    repo_path: str,
    base_prompt: Optional[str],
    extra_prompt: Optional[str],
) -> int:
    log_file = get_base_dir() / "logs" / f"{name}.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _blocked_file(name).unlink(missing_ok=True)
    _done_file(name).unlink(missing_ok=True)

    prompt = build_prompt(name, base_prompt, extra_prompt)
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
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    db = await get_db()
    try:
        await db.execute(
            "UPDATE sessions SET status = ?, pid = ?, last_extra_prompt = ?, updated_at = ? WHERE name = ?",
            (Status.RUNNING.value, proc.pid, extra_prompt, _now(), name),
        )
        await db.commit()
    finally:
        await db.close()

    asyncio.create_task(_stream_to_file(proc, log_file, name, session_id, repo_path, base_prompt, extra_prompt))
    return proc.pid


async def _stream_to_file(
    proc: asyncio.subprocess.Process,
    log_file: Path,
    name: str,
    session_id: str,
    repo_path: str,
    base_prompt: Optional[str],
    extra_prompt: Optional[str],
) -> None:
    rate_limit_reset: Optional[datetime] = None

    with log_file.open("ab") as f:
        async for line in proc.stdout:
            f.write(line)
            f.flush()
            if rate_limit_reset is None:
                m = RATE_LIMIT_RE.search(line.decode(errors="replace"))
                if m:
                    rate_limit_reset = _parse_reset_time(m.group(1), m.group(2))
                    proc.terminate()

    await proc.wait()

    if rate_limit_reset is not None:
        retry_at_iso = rate_limit_reset.isoformat()
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sessions SET status = ?, pid = NULL, retry_at = ?, updated_at = ? WHERE name = ?",
                (Status.WAITING.value, retry_at_iso, _now(), name),
            )
            await db.commit()
        finally:
            await db.close()
        asyncio.create_task(_retry_at(name, session_id, repo_path, base_prompt, extra_prompt, rate_limit_reset))
        return

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


async def _retry_at(
    name: str,
    session_id: str,
    repo_path: str,
    base_prompt: Optional[str],
    extra_prompt: Optional[str],
    reset_time: datetime,
) -> None:
    delay = (reset_time - datetime.now(timezone.utc)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    await start_run(name, session_id, repo_path, base_prompt, extra_prompt)


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
