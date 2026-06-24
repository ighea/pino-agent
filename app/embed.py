import math
import os

from openai import OpenAI as _OpenAI

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
_embed_client: _OpenAI | None = None


def _get_client() -> _OpenAI:
    global _embed_client
    if _embed_client is None:
        _embed_client = _OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1"),
            api_key=os.getenv("OPENAI_API_KEY", "ollama"),
        )
    return _embed_client


def embed(text: str) -> list[float] | None:
    """Return an embedding vector for text, or None if unavailable."""
    if not EMBEDDING_MODEL:
        return None
    try:
        resp = _get_client().embeddings.create(model=EMBEDDING_MODEL, input=text)
        return resp.data[0].embedding
    except Exception:
        return None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
