"""Background task execution with tracking, cancellation, and per-task logging.

Each background task gets a short hex ID and moves through states:
  pending → running → done | failed | cancelled

A JSONL log file is written to BG_LOG_DIR for every task so you can inspect
exactly what the background agent did (start, completion, error).

New tools registered here:
  start_background_task  — fire-and-forget, returns a task ID
  list_background_tasks  — show all tasks and their current status
  cancel_background_task — cancel a pending or running task
  get_task_log           — read the detailed JSONL log for a task
"""

import asyncio
import contextvars
import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from app.tools.builtin import tool_manager

_llm_var: contextvars.ContextVar = contextvars.ContextVar("bg_llm", default=None)
_tools_var: contextvars.ContextVar = contextvars.ContextVar("bg_tools", default=None)
_push_fn_var: contextvars.ContextVar[Callable[[str], Awaitable[None]] | None] = (
    contextvars.ContextVar("bg_push_fn", default=None)
)
_room_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bg_room_id", default=None
)

# In-memory registry: task_id → task dict
# "_task" key holds the asyncio.Task and is not serialised.
_registry: dict[str, dict] = {}

_BG_LOG_DIR = Path(os.getenv("BG_LOG_DIR", "data/logs"))
_UTC = datetime.timezone.utc


def set_background_context(llm, tools, push_fn, room_id: str | None = None) -> None:
    _llm_var.set(llm)
    _tools_var.set(tools)
    _push_fn_var.set(push_fn)
    _room_id_var.set(room_id)


def _now_iso() -> str:
    return datetime.datetime.now(_UTC).isoformat()


def _write_log(task_id: str, event: str, **data) -> None:
    try:
        _BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": _now_iso(), "event": event, **data}
        with (_BG_LOG_DIR / f"bg_{task_id}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _notify(push_fn, room_id: str | None, text: str) -> None:
    """Deliver text via push_fn; fall back to fire_proactive if push_fn raises or is None."""
    if push_fn:
        try:
            await push_fn(text)
            return
        except Exception:
            pass
    from app import scheduler as _scheduler
    await _scheduler.fire_proactive(room_id, text)


async def _run_task(
    task_id: str,
    prompt: str,
    push_fn,
    llm,
    tools,
    delay: float,
    room_id: str | None,
) -> None:
    from app.agent import AgentLoop
    from app.triggers.base import TriggerEvent

    entry = _registry[task_id]

    if delay > 0:
        await asyncio.sleep(delay)

    entry["status"] = "running"
    entry["started_at"] = _now_iso()
    _write_log(task_id, "started", prompt=prompt)

    event = TriggerEvent(
        input=prompt,
        source="background",
        metadata=({"room_id": room_id, "bg_task_id": task_id} if room_id
                  else {"bg_task_id": task_id}),
    )
    try:
        result = await AgentLoop(llm=llm, tools=tools).run(event)
        entry["status"] = "done"
        entry["result"] = result
        entry["completed_at"] = _now_iso()
        _write_log(task_id, "completed", result=result)
        await _notify(push_fn, room_id, f"**[Background task complete]**\n{result}")
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
        entry["completed_at"] = _now_iso()
        _write_log(task_id, "cancelled")
        raise
    except Exception as e:
        entry["status"] = "failed"
        entry["error"] = str(e)
        entry["completed_at"] = _now_iso()
        _write_log(task_id, "failed", error=str(e))
        await _notify(push_fn, room_id, f"**[Background task failed]** {e}")


async def _start_background_task(task: str, delay_seconds: float = 0) -> str:
    llm = _llm_var.get()
    tools = _tools_var.get()
    push_fn = _push_fn_var.get()
    room_id = _room_id_var.get()

    if llm is None or tools is None:
        return "Error: Background tasks are not available in this context."

    task_id = uuid.uuid4().hex[:8]
    _registry[task_id] = {
        "id": task_id,
        "prompt": task,
        "status": "pending",
        "room_id": room_id,
        "created_at": _now_iso(),
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error": None,
    }
    _write_log(task_id, "created", prompt=task, delay_seconds=delay_seconds)

    asyncio_task = asyncio.create_task(
        _run_task(task_id, task, push_fn, llm, tools, delay_seconds, room_id)
    )
    _registry[task_id]["_task"] = asyncio_task

    if delay_seconds > 0:
        m, s = divmod(int(delay_seconds), 60)
        delay_str = f"{m}m {s}s" if m else f"{s}s"
        return f"Background task `{task_id}` scheduled in {delay_str}."
    return f"Background task `{task_id}` started."


def _list_background_tasks() -> str:
    if not _registry:
        return "No background tasks."
    lines = []
    for entry in sorted(_registry.values(), key=lambda e: e["created_at"], reverse=True):
        tid = entry["id"]
        status = entry["status"].upper()
        prompt = entry["prompt"]
        if len(prompt) > 80:
            prompt = prompt[:80] + "…"
        created = entry["created_at"][:19].replace("T", " ")
        line = f"[{tid}] {status}  created: {created}\n  Task: {prompt}"
        if entry["status"] == "done" and entry["result"]:
            preview = (entry["result"] or "")[:120].replace("\n", " ")
            line += f"\n  Result: {preview}…"
        if entry["error"]:
            line += f"\n  Error: {entry['error']}"
        lines.append(line)
    return "\n\n".join(lines)


async def _cancel_background_task(task_id: str) -> str:
    entry = _registry.get(task_id)
    if not entry:
        return f"Error: No background task found with ID '{task_id}'."
    status = entry["status"]
    if status in ("done", "failed", "cancelled"):
        return f"Task `{task_id}` is already {status} — nothing to cancel."
    asyncio_task = entry.get("_task")
    if asyncio_task and not asyncio_task.done():
        asyncio_task.cancel()
    entry["status"] = "cancelled"
    entry["completed_at"] = _now_iso()
    _write_log(task_id, "cancelled_by_user")
    return f"Background task `{task_id}` cancelled."


def _get_task_log(task_id: str) -> str:
    log_file = _BG_LOG_DIR / f"bg_{task_id}.jsonl"
    if not log_file.exists():
        if task_id in _registry:
            return (
                f"Task `{task_id}` is in the registry (status: {_registry[task_id]['status']}) "
                f"but has no log file yet."
            )
        return f"Error: No task or log found for ID '{task_id}'."
    try:
        raw = log_file.read_text(encoding="utf-8").strip()
        if not raw:
            return f"Log file for task `{task_id}` is empty."
        formatted = []
        for line in raw.split("\n"):
            if not line:
                continue
            e = json.loads(line)
            ts = e.pop("timestamp", "")[:19].replace("T", " ")
            event = e.pop("event", "?")
            parts = [f"[{ts}] {event}"]
            for k, v in e.items():
                val = str(v)
                if len(val) > 400:
                    val = val[:400] + "…"
                parts.append(f"  {k}: {val}")
            formatted.append("\n".join(parts))
        return "\n\n".join(formatted)
    except Exception as exc:
        return f"Error reading log for `{task_id}`: {exc}"


tool_manager.register(
    name="start_background_task",
    fn=_start_background_task,
    description=(
        "Start a task that runs in the background and delivers the result automatically when done. "
        "Use for long-running work the user doesn't need to wait for: extended research, "
        "multi-step lookups, file generation, or processing-heavy tasks. "
        "Returns a short task ID — use list_background_tasks to check status "
        "or cancel_background_task to abort it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Self-contained instruction for the background agent.",
            },
            "delay_seconds": {
                "type": "number",
                "description": "Seconds to wait before starting. 0 = start immediately.",
            },
        },
        "required": ["task"],
    },
    status_template="Starting background task: {task:.80}",
)

tool_manager.register(
    name="list_background_tasks",
    fn=_list_background_tasks,
    description=(
        "List all background tasks and their current status "
        "(pending, running, done, failed, cancelled). "
        "Shows task ID, prompt, timestamps, and a short result preview for completed tasks."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    status_template="Listing background tasks...",
)

tool_manager.register(
    name="cancel_background_task",
    fn=_cancel_background_task,
    description="Cancel a pending or running background task by its ID.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The 8-character task ID returned by start_background_task.",
            },
        },
        "required": ["task_id"],
    },
    status_template="Cancelling background task: {task_id}",
)

tool_manager.register(
    name="get_task_log",
    fn=_get_task_log,
    description=(
        "Read the detailed event log for a background task. "
        "Shows each lifecycle event (created, started, completed, failed) with timestamps. "
        "Useful for debugging failed tasks or reviewing what the background agent did."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The 8-character task ID.",
            },
        },
        "required": ["task_id"],
    },
    status_template="Reading log for task: {task_id}",
)
