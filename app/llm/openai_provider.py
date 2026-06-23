import os
from openai import AsyncOpenAI
from app.llm.base import BaseLLM


class OpenAIProvider(BaseLLM):
    def __init__(self, model: str | None = None) -> None:
        base_url = os.getenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
        api_key = os.getenv("OPENAI_API_KEY", "ollama")
        self._model = model or os.getenv("OPENAI_MODEL", "gemma4:latest")
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        print(f"LLM: OpenAI-compatible provider → {base_url}  model={self._model}")

    @property
    def model(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ):
        kwargs: dict = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return await self._client.chat.completions.create(**kwargs)
