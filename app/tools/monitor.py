"""Proactive URL monitoring.

Watches are stored in WATCHES_FILE and scheduled via APScheduler's interval trigger.
When a watched URL's content changes, a notification is fired through the proactive
handler (Matrix DM, CLI print, etc.).
"""

import asyncio
import contextvars
import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path

import requests as _requests

from app.tools.builtin import tool_manager
from app.tools.fetch import _check_url

_WATCHES_FILE = Path(os.getenv("WATCHES_FILE", "data/watches.json"))
_FETCH_TIMEOUT = 30
_MAX_CONTENT_BYTES = 500_000   # cap download at 500 KB for hashing
_SNIPPET_CHARS = 600           # chars of new content included in notification

_room_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "monitor_room_id", default=None
)


def set_monitor_context(room_id: str | None) -> None:
    _room_id_var.set(room_id)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_watches() -> dict:
    if _WATCHES_FILE.exists():
        try:
            return json.loads(_WATCHES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_watches(watches: dict) -> None:
    _WATCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _WATCHES_FILE.write_text(
        json.dumps(watches, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Fetch + hashing (synchronous, run via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _fetch_content(url: str) -> tuple[str, str] | None:
    """Return (content_hash, text_snippet) or None on failure."""
    err = _check_url(url)
    if err:
        return None
    try:
        resp = _requests.get(
            url,
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PinoAgent/1.0)"},
            stream=True,
        )
        if not resp.ok:
            return None

        raw = b""
        for chunk in resp.iter_content(chunk_size=65_536):
            raw += chunk
            if len(raw) >= _MAX_CONTENT_BYTES:
                break

        content_type = resp.headers.get("content-type", "")
        if "html" in content_type:
            try:
                import trafilatura
                text = trafilatura.extract(raw.decode("utf-8", errors="replace")) or ""
            except Exception:
                text = raw.decode("utf-8", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")

        content_hash = hashlib.sha256(text.encode()).hexdigest()
        snippet = text[:_SNIPPET_CHARS].strip()
        return content_hash, snippet
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scheduled check (called by APScheduler)
# ---------------------------------------------------------------------------

async def _check_watch(watch_id: str) -> None:
    from app import scheduler as _scheduler

    watches = _load_watches()
    watch = watches.get(watch_id)
    if not watch:
        return

    url = watch["url"]
    result = await asyncio.to_thread(_fetch_content, url)

    watch["last_checked"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if result is None:
        watches[watch_id] = watch
        _save_watches(watches)
        return

    content_hash, snippet = result
    previous_hash = watch.get("last_hash")

    if previous_hash and previous_hash != content_hash:
        # Content changed — update stored hash and notify
        watch["last_hash"] = content_hash
        watches[watch_id] = watch
        _save_watches(watches)

        label = watch.get("label") or url
        message = (
            f"\U0001f514 **{label}** has changed\n{url}\n\n"
            f"Content excerpt:\n{snippet}"
        )
        await _scheduler.fire_proactive(watch.get("room_id"), message)
        return

    if not previous_hash:
        # First successful fetch — store baseline, no notification
        watch["last_hash"] = content_hash

    watches[watch_id] = watch
    _save_watches(watches)


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def _watch_url(url: str, interval_minutes: int = 60, label: str | None = None) -> str:
    err = _check_url(url)
    if err:
        return f"Error: {err}"

    interval_minutes = max(5, min(int(interval_minutes), 10_080))  # 5 min – 1 week

    from app import scheduler as sched
    if not sched.get_scheduler().running:
        return "Error: scheduler is not running."

    watch_id = str(uuid.uuid4())[:8]
    room_id = _room_id_var.get()
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    watches = _load_watches()
    watches[watch_id] = {
        "url": url,
        "interval_minutes": interval_minutes,
        "label": label or url,
        "room_id": room_id,
        "last_hash": None,
        "last_checked": None,
        "created_at": now_str,
    }
    _save_watches(watches)

    # Schedule with a one-interval delay so the first check isn't immediate
    first_run = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=interval_minutes
    )
    sched.get_scheduler().add_job(
        _check_watch,
        trigger="interval",
        minutes=interval_minutes,
        args=[watch_id],
        id=f"watch_{watch_id}",
        replace_existing=True,
        next_run_time=first_run,
    )

    readable = f"{interval_minutes} minute{'s' if interval_minutes != 1 else ''}"
    return f"Watching '{label or url}' every {readable} (id: {watch_id})."


def _unwatch_url(watch_id: str) -> str:
    watches = _load_watches()
    if watch_id not in watches:
        return f"Error: no watch with id '{watch_id}'."

    label = watches[watch_id].get("label") or watches[watch_id]["url"]
    del watches[watch_id]
    _save_watches(watches)

    try:
        from app import scheduler as sched
        sched.get_scheduler().remove_job(f"watch_{watch_id}")
    except Exception:
        pass

    return f"Stopped watching '{label}' (id: {watch_id})."


def _list_watches() -> str:
    watches = _load_watches()
    if not watches:
        return "No active watches."
    lines = ["Active watches:"]
    for wid, w in watches.items():
        last = w.get("last_checked")
        last_str = last[:19].replace("T", " ") if last else "never"
        interval = w.get("interval_minutes", "?")
        label = w.get("label") or w["url"]
        lines.append(f"- [{wid}] {label} — every {interval}min, last checked: {last_str}")
    return "\n".join(lines)


def load_and_schedule_pending() -> None:
    """Restore all persisted watches to the scheduler on startup."""
    from app import scheduler as sched
    watches = _load_watches()
    for watch_id, w in watches.items():
        try:
            sched.get_scheduler().add_job(
                _check_watch,
                trigger="interval",
                minutes=w.get("interval_minutes", 60),
                args=[watch_id],
                id=f"watch_{watch_id}",
                replace_existing=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

tool_manager.register(
    name="watch_url",
    fn=_watch_url,
    description=(
        "Monitor a URL for content changes and receive a notification when it changes. "
        "On the first check the current content is stored as the baseline; subsequent checks "
        "that detect a difference trigger a proactive notification with a content excerpt. "
        "Minimum interval is 5 minutes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL to monitor.",
            },
            "interval_minutes": {
                "type": "integer",
                "description": "How often to check for changes, in minutes (default 60, minimum 5).",
            },
            "label": {
                "type": "string",
                "description": "Optional short label shown in notifications (e.g. 'HN front page'). Defaults to the URL.",
            },
        },
        "required": ["url"],
    },
    status_template="Setting up watch for: {url}",
)

tool_manager.register(
    name="unwatch_url",
    fn=_unwatch_url,
    description="Stop monitoring a URL. Use the watch ID shown by list_watches.",
    parameters={
        "type": "object",
        "properties": {
            "watch_id": {
                "type": "string",
                "description": "The 8-character watch ID.",
            },
        },
        "required": ["watch_id"],
    },
    status_template="Removing watch {watch_id}...",
)

tool_manager.register(
    name="list_watches",
    fn=_list_watches,
    description="List all active URL watches with their IDs, labels, check intervals, and last check times.",
    parameters={"type": "object", "properties": {}, "required": []},
    status_template="Fetching active watches...",
)
