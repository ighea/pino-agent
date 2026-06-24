import os
from openai import AsyncOpenAI
from app.llm.base import BaseLLM

# Ollama defaults to num_ctx=2048 which is far too small for tool use + history.
# Set to 0 to use the model's built-in default (not recommended).
_OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))


class OpenAIProvider(BaseLLM):
    def __init__(self, model: str | None = None) -> None:
        base_url = os.getenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
        api_key = os.getenv("OPENAI_API_KEY", "ollama")
        self._model = model or os.getenv("OPENAI_MODEL", "gemma4:e4b")
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        print(f"LLM: OpenAI-compatible provider → {base_url}  model={self._model}  num_ctx={_OLLAMA_NUM_CTX or 'model default'}")

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
        if _OLLAMA_NUM_CTX:
            kwargs["extra_body"] = {"options": {"num_ctx": _OLLAMA_NUM_CTX}}
        return await self._client.chat.completions.create(**kwargs)
