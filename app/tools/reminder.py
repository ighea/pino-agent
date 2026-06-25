import asyncio
import contextvars
import datetime
import json
import os
import uuid
from pathlib import Path
from app.tools.builtin import tool_manager
from app.tz import TZ as _AGENT_TZ, TZ_NAME as _AGENT_TZ_NAME

_REMINDERS_FILE = Path(os.getenv("REMINDERS_FILE", "data/reminders.json"))

_room_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "reminder_room_id", default=None
)


def set_reminder_context(room_id: str | None) -> None:
    _room_id_var.set(room_id)


def _load_reminders() -> list[dict]:
    if _REMINDERS_FILE.exists():
        try:
            return json.loads(_REMINDERS_FILE.read_text())
        except Exception:
            return []
    return []


def _save_reminders(reminders: list[dict]) -> None:
    _REMINDERS_FILE.write_text(json.dumps(reminders, indent=2))


async def _fire_reminder(reminder_id: str, room_id: str | None, message: str) -> None:
    from app import scheduler
    await scheduler.fire_proactive(room_id, f"⏰ Reminder: {message}")
    _save_reminders([r for r in _load_reminders() if r["id"] != reminder_id])


def load_and_schedule_pending() -> None:
    from app import scheduler as sched
    now = datetime.datetime.now(datetime.timezone.utc)
    for r in _load_reminders():
        try:
            dt = datetime.datetime.fromisoformat(r["when"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            if dt > now:
                sched.get_scheduler().add_job(
                    _fire_reminder,
                    trigger="date",
                    run_date=dt,
                    args=[r["id"], r.get("room_id"), r["message"]],
                    id=r["id"],
                    replace_existing=True,
                )
        except Exception:
            pass


def _set_reminder(when: str, message: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(when)
    except ValueError:
        return f"Error: invalid datetime '{when}'. Use ISO 8601, e.g. 2026-06-23T15:00:00."

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_AGENT_TZ)

    now = datetime.datetime.now(datetime.timezone.utc)
    if dt <= now:
        return f"Error: {dt.isoformat()} is in the past."

    from app import scheduler as sched
    if not sched.get_scheduler().running:
        return "Error: scheduler is not running."

    reminder_id = str(uuid.uuid4())[:8]
    room_id = _room_id_var.get()

    reminders = _load_reminders()
    reminders.append({
        "id": reminder_id,
        "when": dt.isoformat(),
        "message": message,
        "room_id": room_id,
    })
    _save_reminders(reminders)

    sched.get_scheduler().add_job(
        _fire_reminder,
        trigger="date",
        run_date=dt,
        args=[reminder_id, room_id, message],
        id=reminder_id,
        replace_existing=True,
    )

    time_str = dt.astimezone(_AGENT_TZ).strftime(f"%Y-%m-%d %H:%M {_AGENT_TZ_NAME}")
    return f"Reminder set for {time_str}: {message} (id: {reminder_id})"


def _list_reminders() -> str:
    reminders = _load_reminders()
    if not reminders:
        return "No pending reminders."
    lines = ["Pending reminders:"]
    for r in reminders:
        lines.append(f"- [{r['id']}] {r['when']}: {r['message']}")
    return "\n".join(lines)


def _cancel_reminder(reminder_id: str) -> str:
    reminders = _load_reminders()
    filtered = [r for r in reminders if r["id"] != reminder_id]
    if len(filtered) == len(reminders):
        return f"Error: no reminder with id '{reminder_id}'."
    _save_reminders(filtered)
    try:
        from app import scheduler as sched
        sched.get_scheduler().remove_job(reminder_id)
    except Exception:
        pass
    return f"Reminder '{reminder_id}' cancelled."


tool_manager.register(
    name="set_reminder",
    fn=_set_reminder,
    description=(
        "Schedule a reminder to be delivered at a specific time. "
        "The reminder message will be sent back to the conversation when it fires. "
        "Use ISO 8601 format for 'when' (e.g. 2026-06-23T15:00:00). "
        f"Times without a timezone offset are interpreted as {_AGENT_TZ_NAME}. "
        "Calculate the target time from the current date/time provided in the system prompt."
    ),
    parameters={
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "description": "ISO 8601 datetime when the reminder should fire (e.g. 2026-06-23T15:00:00).",
            },
            "message": {
                "type": "string",
                "description": "The reminder message to deliver.",
            },
        },
        "required": ["when", "message"],
    },
    status_template="Setting reminder for {when}...",
)

tool_manager.register(
    name="list_reminders",
    fn=_list_reminders,
    description="List all pending reminders with their IDs and scheduled times.",
    parameters={"type": "object", "properties": {}, "required": []},
    status_template="Fetching reminders...",
)

tool_manager.register(
    name="cancel_reminder",
    fn=_cancel_reminder,
    description="Cancel a pending reminder by its ID (shown in list_reminders output).",
    parameters={
        "type": "object",
        "properties": {
            "reminder_id": {
                "type": "string",
                "description": "The 8-character reminder ID to cancel.",
            },
        },
        "required": ["reminder_id"],
    },
    status_template="Cancelling reminder {reminder_id}...",
)
