import datetime
import json
import math
import os

from openai import OpenAI as _OpenAI

from app.tools.builtin import tool_manager

MEMORY_FILE = os.getenv("MEMORY_FILE", "memory.json")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")

_UTC = datetime.timezone.utc
_embed_client: _OpenAI | None = None


def _get_embed_client() -> _OpenAI:
    global _embed_client
    if _embed_client is None:
        _embed_client = _OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1"),
            api_key=os.getenv("OPENAI_API_KEY", "ollama"),
        )
    return _embed_client


def _embed(text: str) -> list[float] | None:
    if not EMBEDDING_MODEL:
        return None
    try:
        resp = _get_embed_client().embeddings.create(model=EMBEDDING_MODEL, input=text)
        return resp.data[0].embedding
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _load() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {}
    with open(MEMORY_FILE) as f:
        return json.load(f)


def _load_live() -> dict:
    """Load memories, removing any that have passed their expiry time."""
    data = _load()
    now = datetime.datetime.now(_UTC)
    expired = [
        k for k, v in data.items()
        if v.get("expires_at") and datetime.datetime.fromisoformat(v["expires_at"]) <= now
    ]
    if expired:
        for k in expired:
            del data[k]
        _persist(data)
    return data


def _persist(data: dict) -> None:
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _save_memory(key: str, value: str, category: str = "general", ttl_days: float | None = None) -> str:
    data = _load_live()
    expires_at = None
    if ttl_days is not None and ttl_days > 0:
        expires_at = (datetime.datetime.now(_UTC) + datetime.timedelta(days=ttl_days)).isoformat()
    data[key.lower().strip()] = {
        "value": value,
        "category": category,
        "saved_at": datetime.datetime.now(_UTC).isoformat(),
        "expires_at": expires_at,
        "embedding": _embed(f"{key}: {value}"),
    }
    _persist(data)
    suffix = f" (expires in {ttl_days} day{'s' if ttl_days != 1 else ''})" if ttl_days else ""
    return f"Saved: {key} = {value}{suffix}"


def get_core_memories() -> str:
    """Return all core memories as a plain list for system prompt injection."""
    data = _load_live()
    cores = {k: v for k, v in data.items() if v.get("category") == "core"}
    if not cores:
        return ""
    return "\n".join(f"- {k}: {v['value']}" for k, v in cores.items())


def _fmt_entry(key: str, v: dict) -> str:
    expiry = f", expires {v['expires_at'][:10]}" if v.get("expires_at") else ""
    return f"- [{v['category']}] {key}: {v['value']}{expiry}"


def _recall_memory(query: str = "") -> str:
    data = _load_live()
    if not data:
        return "No memories stored yet."
    if not query:
        return "\n".join(_fmt_entry(k, v) for k, v in data.items())

    # Semantic search
    q_emb = _embed(query)
    if q_emb is not None:
        scored = sorted(
            [
                (k, v, _cosine(q_emb, v["embedding"]))
                for k, v in data.items()
                if v.get("embedding")
            ],
            key=lambda t: t[2],
            reverse=True,
        )
        matches = [(k, v) for k, v, score in scored if score >= 0.35]
        if matches:
            return "\n".join(_fmt_entry(k, v) for k, v in matches)

    # Keyword fallback
    q = query.lower()
    matches = [
        (k, v) for k, v in data.items()
        if q in k.lower() or q in v["value"].lower() or q in v["category"].lower()
    ]
    if not matches:
        return f"No memories found matching '{query}'."
    return "\n".join(_fmt_entry(k, v) for k, v in matches)


def _delete_memory(key: str) -> str:
    data = _load_live()
    key = key.lower().strip()
    if key not in data:
        return f"Error: No memory found with key '{key}'."
    del data[key]
    _persist(data)
    return f"Forgotten: {key}"


tool_manager.register(
    name="save_memory",
    fn=_save_memory,
    description=(
        "Save an important piece of information to long-term memory. "
        "Use for locations, preferences, appointments, names, or any personal detail worth remembering across conversations. "
        "Choose a short descriptive key and an optional category. "
        "For time-sensitive information such as appointments or reminders, set ttl_days so the memory expires automatically."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Short identifier for this memory, e.g. 'home_city' or 'dentist_appointment'.",
            },
            "value": {
                "type": "string",
                "description": "The information to remember.",
            },
            "category": {
                "type": "string",
                "description": (
                    "Category for this memory. Use 'core' for facts that should always be injected "
                    "into every conversation (e.g. the user's name, home city, language preference). "
                    "Other categories: location, appointment, personal, preference, general."
                ),
                "enum": ["core", "location", "appointment", "personal", "preference", "general"],
            },
            "ttl_days": {
                "type": "number",
                "description": (
                    "Optional number of days until this memory expires automatically. "
                    "Use for time-sensitive info: 1 for tomorrow, 7 for a week, 30 for a month. "
                    "Omit for permanent memories like home city or preferences."
                ),
            },
        },
        "required": ["key", "value"],
    },
    status_template="Saving to memory: {key}",
)

tool_manager.register(
    name="recall_memory",
    fn=_recall_memory,
    description=(
        "Recall stored memories. Pass a search term to find specific memories, "
        "or leave query empty to list everything. Expired memories are never returned."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search term to filter memories. Leave empty to retrieve all.",
            },
        },
        "required": [],
    },
    status_template="Searching memory...",
)

tool_manager.register(
    name="delete_memory",
    fn=_delete_memory,
    description="Delete a specific memory by its key.",
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The key of the memory to delete.",
            },
        },
        "required": ["key"],
    },
    status_template="Forgetting: {key}",
)
