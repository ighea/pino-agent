"""User-defined recurring scheduled prompts.

Tasks are stored in SCHEDULED_TASKS_FILE and restored on startup via
load_and_schedule_pending(server). Each task fires a fresh AgentLoop
via server.handle_event() and delivers results proactively.
"""

import contextvars
import datetime
import json
import os
import uuid
from pathlib import Path

from app.tools.builtin import tool_manager

_TASKS_FILE = Path(os.getenv("SCHEDULED_TASKS_FILE", "data/scheduled_tasks.json"))

_room_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "scheduled_task_room_id", default=None
)

_server_ref = None


def set_scheduled_task_context(room_id: str | None) -> None:
    _room_id_var.set(room_id)


def _load_tasks() -> dict:
    if _TASKS_FILE.exists():
        try:
            return json.loads(_TASKS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_tasks(tasks: dict) -> None:
    _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TASKS_FILE.write_text(json.dumps(tasks, indent=2))


async def _run_scheduled_task(task_id: str) -> None:
    from app import scheduler as _scheduler

    if _server_ref is None:
        return

    tasks = _load_tasks()
    task = tasks.get(task_id)
    if not task:
        return

    task["run_count"] = task.get("run_count", 0) + 1
    task["last_run"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tasks[task_id] = task
    _save_tasks(tasks)

    room_id = task.get("room_id")
    prompt = task["prompt"]
    label = task.get("label") or prompt[:50]

    async def respond_fn(text: str) -> None:
        await _scheduler.fire_proactive(room_id, f"📅 **{label}**\n{text}")

    from app.triggers.base import TriggerEvent
    event = TriggerEvent(
        input=prompt,
        source="scheduler",
        metadata={"room_id": room_id} if room_id else {},
        respond_fn=respond_fn,
    )

    try:
        await _server_ref.handle_event(event)
    except Exception as e:
        await _scheduler.fire_proactive(room_id, f"📅 Scheduled task '{label}' failed: {e}")


def _schedule_job(task_id: str, task: dict, scheduler) -> None:
    if task.get("interval_minutes"):
        scheduler.add_job(
            _run_scheduled_task,
            trigger="interval",
            minutes=int(task["interval_minutes"]),
            args=[task_id],
            id=f"stask_{task_id}",
            replace_existing=True,
        )
    elif task.get("cron_expr"):
        try:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(task["cron_expr"])
            scheduler.add_job(
                _run_scheduled_task,
                trigger=trigger,
                args=[task_id],
                id=f"stask_{task_id}",
                replace_existing=True,
            )
        except Exception:
            pass


def _create_scheduled_task(
    prompt: str,
    label: str | None = None,
    interval_minutes: int | None = None,
    cron_expr: str | None = None,
) -> str:
    if interval_minutes is None and cron_expr is None:
        return "Error: provide either interval_minutes or cron_expr."

    from app import scheduler as sched
    if not sched.get_scheduler().running:
        return "Error: scheduler is not running."

    if interval_minutes is not None:
        interval_minutes = max(1, int(interval_minutes))

    task_id = str(uuid.uuid4())[:8]
    room_id = _room_id_var.get()
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    entry = {
        "id": task_id,
        "prompt": prompt,
        "label": label,
        "interval_minutes": interval_minutes,
        "cron_expr": cron_expr,
        "room_id": room_id,
        "created_at": now_str,
        "last_run": None,
        "run_count": 0,
    }

    tasks = _load_tasks()
    tasks[task_id] = entry
    _save_tasks(tasks)

    _schedule_job(task_id, entry, sched.get_scheduler())

    if interval_minutes:
        freq = f"every {interval_minutes} minute{'s' if interval_minutes != 1 else ''}"
    else:
        freq = f"cron '{cron_expr}'"
    display = label or prompt[:60]
    return f"Scheduled task created ({freq}): {display!r} (id: {task_id})"


def _list_scheduled_tasks() -> str:
    tasks = _load_tasks()
    if not tasks:
        return "No scheduled tasks."
    lines = ["Scheduled tasks:"]
    for tid, t in tasks.items():
        label = t.get("label") or t["prompt"][:40]
        if t.get("interval_minutes"):
            freq = f"every {t['interval_minutes']}min"
        else:
            freq = f"cron: {t.get('cron_expr', '?')}"
        last = t.get("last_run")
        last_str = last[:19].replace("T", " ") if last else "never"
        count = t.get("run_count", 0)
        lines.append(f"- [{tid}] {label!r} — {freq}, last run: {last_str}, runs: {count}")
    return "\n".join(lines)


def _cancel_scheduled_task(task_id: str) -> str:
    tasks = _load_tasks()
    if task_id not in tasks:
        return f"Error: no scheduled task with id '{task_id}'."

    label = tasks[task_id].get("label") or tasks[task_id]["prompt"][:40]
    del tasks[task_id]
    _save_tasks(tasks)

    try:
        from app import scheduler as sched
        sched.get_scheduler().remove_job(f"stask_{task_id}")
    except Exception:
        pass

    return f"Scheduled task '{label}' cancelled (id: {task_id})."


def load_and_schedule_pending(server) -> None:
    global _server_ref
    _server_ref = server
    from app import scheduler as sched
    for task_id, task in _load_tasks().items():
        try:
            _schedule_job(task_id, task, sched.get_scheduler())
        except Exception:
            pass


tool_manager.register(
    name="create_scheduled_task",
    fn=_create_scheduled_task,
    description=(
        "Create a recurring scheduled task that runs a prompt automatically and delivers results "
        "as a proactive notification. Provide interval_minutes (e.g. 60 for hourly, 1440 for daily) "
        "OR cron_expr in standard 5-field cron format (e.g. '0 9 * * *' for 9am daily, "
        "'0 9 * * 1-5' for weekdays only). The prompt should be fully self-contained."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Self-contained instruction to execute on each run.",
            },
            "label": {
                "type": "string",
                "description": "Short name shown in notifications (e.g. 'Morning briefing'). Defaults to prompt excerpt.",
            },
            "interval_minutes": {
                "type": "integer",
                "description": "Repeat every N minutes. Mutually exclusive with cron_expr.",
            },
            "cron_expr": {
                "type": "string",
                "description": "5-field cron expression (e.g. '0 9 * * *'). Mutually exclusive with interval_minutes.",
            },
        },
        "required": ["prompt"],
    },
    status_template="Creating scheduled task...",
)

tool_manager.register(
    name="list_scheduled_tasks",
    fn=_list_scheduled_tasks,
    description="List all recurring scheduled tasks with their IDs, schedules, and last run info.",
    parameters={"type": "object", "properties": {}, "required": []},
    status_template="Fetching scheduled tasks...",
)

tool_manager.register(
    name="cancel_scheduled_task",
    fn=_cancel_scheduled_task,
    description="Cancel (permanently delete) a recurring scheduled task by its ID.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The 8-character task ID shown by list_scheduled_tasks.",
            },
        },
        "required": ["task_id"],
    },
    status_template="Cancelling scheduled task {task_id}...",
)
