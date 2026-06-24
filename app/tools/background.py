import asyncio
import contextvars
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


def set_background_context(llm, tools, push_fn, room_id: str | None = None) -> None:
    _llm_var.set(llm)
    _tools_var.set(tools)
    _push_fn_var.set(push_fn)
    _room_id_var.set(room_id)


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


async def _run_task(task: str, push_fn, llm, tools, delay: float, room_id: str | None) -> None:
    from app.agent import AgentLoop
    from app.triggers.base import TriggerEvent

    if delay > 0:
        await asyncio.sleep(delay)

    event = TriggerEvent(
        input=task,
        source="background",
        metadata={"room_id": room_id} if room_id else {},
    )
    try:
        result = await AgentLoop(llm=llm, tools=tools).run(event)
        await _notify(push_fn, room_id, f"**[Background task complete]**\n{result}")
    except Exception as e:
        await _notify(push_fn, room_id, f"**[Background task failed]** {e}")


async def _start_background_task(task: str, delay_seconds: float = 0) -> str:
    llm = _llm_var.get()
    tools = _tools_var.get()
    push_fn = _push_fn_var.get()
    room_id = _room_id_var.get()

    if llm is None or tools is None:
        return "Error: Background tasks are not available in this context."

    asyncio.create_task(_run_task(task, push_fn, llm, tools, delay_seconds, room_id))

    if delay_seconds > 0:
        m, s = divmod(int(delay_seconds), 60)
        delay_str = f"{m}m {s}s" if m else f"{s}s"
        return f"Background task scheduled in {delay_str}: {task}"
    return f"Background task started: {task}"


tool_manager.register(
    name="start_background_task",
    fn=_start_background_task,
    description=(
        "Start a task that runs in the background and reports back when complete. "
        "Use when the user asks for something that would take a long time — extended research, "
        "multi-step lookups, or tasks the user doesn't need to wait for. "
        "The result is delivered back to the user automatically. "
        "Set delay_seconds to schedule the task for later."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Self-contained instruction for what the agent should do.",
            },
            "delay_seconds": {
                "type": "number",
                "description": "Seconds to wait before starting. 0 = start immediately.",
            },
        },
        "required": ["task"],
    },
    status_template="Starting background task: {task}",
)
