"""Session model and status enum."""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Status(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    WAITING = "waiting"


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
    retry_at: Optional[str] = None
    last_extra_prompt: Optional[str] = None
    blocked_reason: Optional[str] = None
    model: Optional[str] = None
