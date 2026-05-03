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
