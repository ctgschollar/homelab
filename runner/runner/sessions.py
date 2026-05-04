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
        retry_at=row["retry_at"],
        last_extra_prompt=row["last_extra_prompt"],
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
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO sessions
               (name, repo_path, session_id, status, base_prompt, pid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
            (name, repo_path, session_id, Status.IDLE.value, base_prompt, now, now),
        )
        await db.commit()
    finally:
        await db.close()
    return Session(
        name=name, repo_path=repo_path, session_id=session_id,
        status=Status.IDLE, base_prompt=base_prompt, pid=None,
        created_at=now, updated_at=now,
    )


async def get_session(name: str) -> Optional[Session]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sessions WHERE name = ?", (name,))
        row = await cursor.fetchone()
    finally:
        await db.close()
    return _row_to_session(row) if row else None


async def list_sessions() -> list[Session]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sessions ORDER BY created_at DESC")
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return [_row_to_session(r) for r in rows]


async def update_session(name: str, **kwargs) -> Optional[Session]:
    ALLOWED_FIELDS = {"session_id", "status", "base_prompt", "pid", "retry_at", "last_extra_prompt", "updated_at"}
    unknown = set(kwargs.keys()) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"Cannot update fields: {unknown}")
    if not kwargs:
        return await get_session(name)
    kwargs["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [name]
    db = await get_db()
    try:
        await db.execute(f"UPDATE sessions SET {set_clause} WHERE name = ?", values)
        await db.commit()
    finally:
        await db.close()
    return await get_session(name)


async def list_waiting_sessions() -> list[Session]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sessions WHERE status = ?", (Status.WAITING.value,))
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return [_row_to_session(r) for r in rows]


async def delete_session(name: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM sessions WHERE name = ?", (name,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()
