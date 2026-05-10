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
                name              TEXT PRIMARY KEY,
                repo_path         TEXT NOT NULL,
                session_id        TEXT,
                status            TEXT NOT NULL DEFAULT 'idle',
                base_prompt       TEXT,
                pid               INTEGER,
                retry_at          TEXT,
                last_extra_prompt TEXT,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            )
        """)
        for col, typedef in [("retry_at", "TEXT"), ("last_extra_prompt", "TEXT"), ("model", "TEXT"), ("base_url", "TEXT"), ("auth_token", "TEXT")]:
            try:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        await db.commit()
