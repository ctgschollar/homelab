"""Log file I/O and SSE streaming."""
from pathlib import Path
import os


def get_base_dir() -> Path:
    return Path(os.environ.get("CLAUDE_RUNNER_BASE_DIR", "/opt/claude-runner"))
