"""Periodic memory consolidation: learn from recent history and compact core memories.

Configured via env vars:
  MEMORY_CONSOLIDATION_CRON      — 5-field cron expression (e.g. "0 3 * * *")
  MEMORY_CONSOLIDATION_INTERVAL_HOURS — alternative: run every N hours (float)
  MEMORY_CONSOLIDATION_CORE_THRESHOLD — compact core memories when count >= this (default 8)
  MEMORY_CONSOLIDATION_MAX_HISTORY_CHARS — max chars of history to embed in prompt (default 8000)
  MEMORY_CONSOLIDATION_STATE_FILE — path to state file (default data/memory_consolidation_state.json)
"""

import datetime
import json
import os
from pathlib import Path

from app.tools.builtin import tool_manager

_STATE_FILE = Path(
    os.getenv("MEMORY_CONSOLIDATION_STATE_FILE", "data/memory_consolidation_state.json")
)
_HISTORY_DIR = Path(os.getenv("HISTORY_DIR", "data/history"))
_CORE_THRESHOLD = int(os.getenv("MEMORY_CONSOLIDATION_CORE_THRESHOLD", "8"))
_MAX_HISTORY_CHARS = int(os.getenv("MEMORY_CONSOLIDATION_MAX_HISTORY_CHARS", "8000"))
_UTC = datetime.timezone.utc

# Injected by main.py so the on-demand tool can invoke the agent loop.
_server_ref = None


def set_consolidation_server(server) -> None:
    global _server_ref
    _server_ref = server


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _extract_text(msg: dict) -> str:
    """Return a readable line for a conversation message, or empty string to skip."""
    role = msg.get("role", "")
    if role == "tool":
        return ""
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    content = content.strip()
    if not content:
        return ""
    # Trim very long individual messages
    if len(content) > 500:
        content = content[:500] + "…"
    return f"{role}: {content}"


def _collect_recent_history(since: datetime.datetime | None) -> str:
    """Read conversation excerpts from history files modified since `since`."""
    if not _HISTORY_DIR.exists():
        return ""

    files = sorted(
        _HISTORY_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    chunks: list[str] = []
    total_chars = 0

    for path in files:
        if since is not None:
            mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=_UTC)
            if mtime <= since:
                continue

        session_id = path.stem
        try:
            messages = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(messages, list):
            continue

        # Take the last 40 messages so each session excerpt stays bounded.
        lines = [_extract_text(m) for m in messages[-40:]]
        lines = [l for l in lines if l]
        if not lines:
            continue

        excerpt = "\n".join(lines)
        remaining = _MAX_HISTORY_CHARS - total_chars
        if len(excerpt) > remaining:
            if remaining < 200:
                break
            excerpt = excerpt[:remaining] + "\n[truncated]"

        chunks.append(f"[Session: {session_id}]\n{excerpt}")
        total_chars += len(excerpt)

        if total_chars >= _MAX_HISTORY_CHARS:
            break

    return "\n\n---\n\n".join(chunks)


def _count_core_memories() -> int:
    from app.tools.memory import _load_live
    return sum(1 for v in _load_live().values() if v.get("category") == "core")


def build_consolidation_prompt() -> str:
    """Build a self-contained consolidation prompt and advance the last-run timestamp."""
    state = _load_state()
    now = datetime.datetime.now(_UTC)

    last_run_str = state.get("last_run")
    since: datetime.datetime | None = None
    if last_run_str:
        try:
            since = datetime.datetime.fromisoformat(last_run_str)
        except ValueError:
            pass

    history_text = _collect_recent_history(since)
    core_count = _count_core_memories()

    # Commit last_run before the agent runs so we don't re-process on partial failure.
    state["last_run"] = now.isoformat()
    _save_state(state)

    since_label = (
        since.strftime("%Y-%m-%d %H:%M UTC") if since else "the beginning"
    )
    now_label = now.strftime("%Y-%m-%d %H:%M UTC")

    parts = [
        f"You are performing a scheduled memory consolidation. Current time: {now_label}.",
        f"You are reviewing conversations since {since_label}.",
        "",
        "═══ TASK 1: LEARN FROM RECENT CONVERSATIONS ═══",
    ]

    if history_text:
        parts += [
            "Review these conversation excerpts. Extract any facts, preferences, interests, "
            "recurring topics, corrections the user made, or inferred context that should be "
            "saved to long-term memory. Use save_memory for each finding. "
            "Be liberal — an unused memory is cheap; making the user repeat themselves is not. "
            "Use appropriate categories: 'core' for always-relevant facts (name, location, "
            "language), 'preference' for likes/dislikes, 'personal' for life details, "
            "'appointment' for time-sensitive items with a ttl_days.",
            "",
            "--- CONVERSATION EXCERPTS ---",
            history_text,
            "--- END OF EXCERPTS ---",
        ]
    else:
        parts.append("No new conversations since the last consolidation — skip this task.")

    parts += [
        "",
        "═══ TASK 2: COMPACT MEMORIES ═══",
    ]

    if core_count >= _CORE_THRESHOLD:
        parts += [
            f"There are {core_count} core memories (threshold: {_CORE_THRESHOLD}). "
            "This is getting unwieldy. Use recall_memory with no query to list everything, then:",
            "• Merge any two core entries that describe the same fact into one.",
            "• Delete core entries that are superseded, stale, or better suited as 'preference' or 'personal'.",
            "• Re-save merged entries with save_memory (category='core').",
            "• Also look for redundant or stale entries across all other categories and clean those up.",
            "Be conservative: only remove what you are confident is truly redundant or outdated.",
        ]
    else:
        parts += [
            f"Core memory count is {core_count}/{_CORE_THRESHOLD} — no compaction required. "
            "Still, use recall_memory with no query to do a quick pass: "
            "delete anything that is obviously stale, expired, or duplicated across categories.",
        ]

    parts += [
        "",
        "After completing both tasks, reply with a short summary: "
        "how many memories were saved, updated, or deleted, and any notable observations.",
    ]

    return "\n".join(parts)


async def _run_consolidation_now() -> str:
    """Run a consolidation immediately and return the agent's summary."""
    if _server_ref is None:
        return "Error: consolidation is not available (server not initialized)."

    from app import scheduler as _scheduler
    from app.triggers.base import TriggerEvent

    result_holder: list[str] = []

    async def respond_fn(text: str) -> None:
        result_holder.append(text)
        await _scheduler.fire_proactive(None, f"🧠 **Memory consolidation**\n{text}")

    prompt = build_consolidation_prompt()
    event = TriggerEvent(input=prompt, source="scheduler", respond_fn=respond_fn)
    try:
        await _server_ref.handle_event(event)
    except Exception as e:
        return f"Error during consolidation: {e}"

    return result_holder[0] if result_holder else "Consolidation complete (no summary returned)."


def setup_consolidation_schedule(server) -> None:
    """Register the memory consolidation job with the APScheduler instance."""
    set_consolidation_server(server)

    cron = os.getenv("MEMORY_CONSOLIDATION_CRON", "").strip()
    interval_hours_str = os.getenv("MEMORY_CONSOLIDATION_INTERVAL_HOURS", "").strip()

    if not cron and not interval_hours_str:
        return

    from app import scheduler as sched

    async def _job() -> None:
        prompt = build_consolidation_prompt()

        async def respond_fn(text: str) -> None:
            await sched.fire_proactive(None, f"🧠 **Memory consolidation**\n{text}")

        from app.triggers.base import TriggerEvent
        event = TriggerEvent(input=prompt, source="scheduler", respond_fn=respond_fn)
        try:
            await server.handle_event(event)
        except Exception as e:
            print(f"[memory_consolidation] Failed: {e}")

    scheduler = sched.get_scheduler()

    if cron:
        try:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(cron)
            scheduler.add_job(
                _job,
                trigger=trigger,
                id="memory_consolidation",
                replace_existing=True,
            )
            print(f"[scheduler] Memory consolidation scheduled with cron '{cron}'.")
        except Exception as e:
            print(f"[scheduler] Invalid MEMORY_CONSOLIDATION_CRON={cron!r}: {e}")
    else:
        try:
            hours = float(interval_hours_str)
            scheduler.add_job(
                _job,
                trigger="interval",
                hours=hours,
                id="memory_consolidation",
                replace_existing=True,
            )
            print(f"[scheduler] Memory consolidation scheduled every {hours}h.")
        except Exception as e:
            print(f"[scheduler] Invalid MEMORY_CONSOLIDATION_INTERVAL_HOURS={interval_hours_str!r}: {e}")


tool_manager.register(
    name="consolidate_memories",
    fn=_run_consolidation_now,
    description=(
        "Trigger an immediate memory consolidation: reviews recent conversation history to extract "
        "and save new learnings, then compacts core memories if they are getting too numerous or redundant. "
        "Use when the user asks to consolidate, review, or tidy up memories."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    status_template="Running memory consolidation…",
)
