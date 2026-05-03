# agent_base.py
from __future__ import annotations
from typing import Protocol, runtime_checkable


@runtime_checkable
class AgentBase(Protocol):
    async def chat(self, message: str, trigger: str) -> tuple[str, float]: ...
    async def handle_event(self, event: dict) -> tuple[str, float]: ...
    async def cancel_all(self) -> None: ...
