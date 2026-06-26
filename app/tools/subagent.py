"""Synchronous sub-agent delegation with per-call logging.

delegate_task runs a fresh AgentLoop inline and returns the result to the
calling agent's tool chain. Unlike start_background_task (fire-and-forget),
this blocks until the sub-agent finishes and feeds the result back into the
current reasoning loop — useful for decomposing complex multi-part tasks or
running independent sub-tasks in parallel within a single step.

Each call gets a short hex ID and is logged to SUBAGENT_LOG_DIR for tracing.
Nesting is capped at _MAX_DEPTH to prevent runaway recursion.
"""

import contextvars
import datetime
import json
import os
import uuid
from pathlib import Path

from app.tools.background import _llm_var, _room_id_var, _tools_var
from app.tools.builtin import tool_manager

_depth_var: contextvars.ContextVar[int] = contextvars.ContextVar("subagent_depth", default=0)

_MAX_DEPTH = 2

_SUBAGENT_LOG_DIR = Path(os.getenv("SUBAGENT_LOG_DIR", os.getenv("BG_LOG_DIR", "data/logs")))
_UTC = datetime.timezone.utc


def _write_subagent_log(agent_id: str, event: str, **data) -> None:
    try:
        _SUBAGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.datetime.now(_UTC).isoformat(),
            "event": event,
            **data,
        }
        with (_SUBAGENT_LOG_DIR / f"subagent_{agent_id}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _delegate_task(prompt: str) -> str:
    depth = _depth_var.get()
    if depth >= _MAX_DEPTH:
        return f"Error: sub-agent nesting limit ({_MAX_DEPTH}) reached — cannot delegate further."

    llm = _llm_var.get()
    tools = _tools_var.get()
    if llm is None or tools is None:
        return "Error: agent context not available for delegation."

    agent_id = uuid.uuid4().hex[:8]
    _write_subagent_log(agent_id, "started", prompt=prompt, depth=depth)

    token = _depth_var.set(depth + 1)
    try:
        from app.agent import AgentLoop
        from app.triggers.base import TriggerEvent

        room_id = _room_id_var.get()
        event = TriggerEvent(
            input=prompt,
            source="subagent",
            metadata=({"room_id": room_id, "subagent_id": agent_id} if room_id
                      else {"subagent_id": agent_id}),
        )
        result = await AgentLoop(llm=llm, tools=tools).run(event)
        _write_subagent_log(agent_id, "completed", result=result or "(no output)")
        return result or "(sub-agent returned no output)"
    except Exception as e:
        _write_subagent_log(agent_id, "failed", error=str(e))
        raise
    finally:
        _depth_var.reset(token)


tool_manager.register(
    name="delegate_task",
    fn=_delegate_task,
    description=(
        "Delegate a sub-task to a fresh agent instance and receive the result inline. "
        "The sub-agent has full tool access (memory, files, web search, code execution, calendar, etc.) "
        "and runs its own reasoning loop independently. "
        "Use to decompose long multi-part tasks (preventing step-limit exhaustion), "
        "or run independent sub-tasks in parallel by calling delegate_task multiple times "
        "in a single step — the agent loop executes all tool calls in a step concurrently. "
        "The result is returned to you directly, not delivered to the user. "
        "For fire-and-forget work, use start_background_task instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "A complete, self-contained instruction for the sub-agent. "
                    "Include all context the sub-agent needs — it starts with no conversation history."
                ),
            },
        },
        "required": ["prompt"],
    },
    status_template="Delegating: {prompt:.80}",
)
