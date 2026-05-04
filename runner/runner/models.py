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
    blocked_reason: Optional[str] = None
