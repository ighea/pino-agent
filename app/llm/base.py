from abc import ABC, abstractmethod
from typing import Any


class BaseLLM(ABC):
    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> Any: ...
