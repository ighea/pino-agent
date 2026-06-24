import os
import requests
from openai import AsyncOpenAI
from app.llm.base import BaseLLM

# Ollama defaults to num_ctx=2048 which is far too small for tool use + history.
# Set to 0 to use the model's built-in default (not recommended).
_OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))


def _query_ollama_model_info(base_url: str, model: str) -> dict:
    """Query Ollama /api/show and return context info: native limit and modelfile num_ctx."""
    ollama_base = base_url.rstrip("/")
    if ollama_base.endswith("/v1"):
        ollama_base = ollama_base[:-3]
    result = {"native": None, "modelfile_num_ctx": None}
    try:
        resp = requests.post(f"{ollama_base}/api/show", json={"name": model}, timeout=5)
        if not resp.ok:
            return result
        data = resp.json()
        for key, val in data.get("model_info", {}).items():
            if key.endswith(".context_length"):
                result["native"] = int(val)
                break
        # Parse "num_ctx NNNN" from the parameters string
        params = data.get("parameters", "")
        for line in params.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "num_ctx":
                try:
                    result["modelfile_num_ctx"] = int(parts[1])
                except ValueError:
                    pass
    except Exception:
        pass
    return result


class OpenAIProvider(BaseLLM):
    def __init__(self, model: str | None = None) -> None:
        base_url = os.getenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
        api_key = os.getenv("OPENAI_API_KEY", "ollama")
        self._model = model or os.getenv("OPENAI_MODEL", "gemma4:e4b")
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        info = _query_ollama_model_info(base_url, self._model)
        native_str = f"{info['native']:,}" if info["native"] else "unknown"
        configured_str = str(_OLLAMA_NUM_CTX) if _OLLAMA_NUM_CTX else "model default"
        print(f"LLM: {self._model}  context window: {configured_str} (native: {native_str})")
        if info["modelfile_num_ctx"] and _OLLAMA_NUM_CTX and info["modelfile_num_ctx"] != _OLLAMA_NUM_CTX:
            print(
                f"  WARNING: Modelfile sets num_ctx={info['modelfile_num_ctx']} — "
                f"Ollama will use {info['modelfile_num_ctx']}, not {_OLLAMA_NUM_CTX}. "
                f"Run: ollama show {self._model} --modelfile > Modelfile && "
                f"echo 'PARAMETER num_ctx {_OLLAMA_NUM_CTX}' >> Modelfile && "
                f"ollama create {self._model} -f Modelfile"
            )

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
