"""Per-session conversation history persistence.

Each session (Matrix room ID or HTTP session_id) gets its own JSON file
under HISTORY_DIR so conversation history survives process restarts.
"""

import json
import os
import re
from pathlib import Path

from app.message_groups import atomic_groups

HISTORY_DIR = Path(os.getenv("HISTORY_DIR", "data/history"))
# Maximum number of messages to persist (hard cap to keep files bounded).
_MAX_PERSISTED = int(os.getenv("MAX_PERSISTED_HISTORY", "200"))


def _session_path(session_id: str) -> Path:
    safe = re.sub(r"[^\w._-]", "_", session_id)[:200]
    return HISTORY_DIR / f"{safe}.json"


def load(session_id: str) -> list[dict]:
    """Load history for a session from disk. Returns [] if not found or unreadable."""
    path = _session_path(session_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _cap_history(history: list[dict], max_messages: int) -> list[dict]:
    """Keep the most recent messages without splitting an assistant/tool-call group.

    A plain history[-N:] slice can cut between an assistant message's tool_calls
    and its tool results, producing an orphaned `tool` message that most chat
    APIs reject on the next load.
    """
    if len(history) <= max_messages:
        return history
    kept: list[int] = []
    count = 0
    for group in reversed(atomic_groups(history)):
        if kept and count + len(group) > max_messages:
            break
        kept = group + kept
        count += len(group)
    return [history[i] for i in kept]


def save(session_id: str, history: list[dict]) -> None:
    """Persist history for a session to disk, capping at _MAX_PERSISTED messages."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = _session_path(session_id)
    trimmed = _cap_history(history, _MAX_PERSISTED)
    path.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
