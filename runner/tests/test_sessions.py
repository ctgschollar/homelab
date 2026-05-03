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
