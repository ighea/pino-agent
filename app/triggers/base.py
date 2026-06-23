from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable
import uuid

if TYPE_CHECKING:
    from app.server import CoreServer


@dataclass
class TriggerEvent:
    input: str | list
    source: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    respond_fn: Callable[[str], Awaitable[None]] | None = None
    status_fn: Callable[[str], Awaitable[None]] | None = None
    react_fn: Callable[[str], Awaitable[None]] | None = None
    deliver_fn: Callable[[str], Awaitable[str]] | None = None


class BaseTrigger:
    async def start(self, server: CoreServer) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError
