"""Synchronous sub-agent delegation.

delegate_task runs a fresh AgentLoop inline and returns the result to the
calling agent's tool chain. Unlike start_background_task (fire-and-forget),
this blocks until the sub-agent is done and feeds the result back into the
current reasoning loop — useful for decomposing complex multi-part tasks or
running independent sub-tasks in parallel within a single step.

Nesting is capped at _MAX_DEPTH to prevent runaway recursion.
"""

import contextvars

from app.tools.background import _llm_var, _room_id_var, _tools_var
from app.tools.builtin import tool_manager

_depth_var: contextvars.ContextVar[int] = contextvars.ContextVar("subagent_depth", default=0)

_MAX_DEPTH = 2


async def _delegate_task(prompt: str) -> str:
    depth = _depth_var.get()
    if depth >= _MAX_DEPTH:
        return f"Error: sub-agent nesting limit ({_MAX_DEPTH}) reached — cannot delegate further."

    llm = _llm_var.get()
    tools = _tools_var.get()
    if llm is None or tools is None:
        return "Error: agent context not available for delegation."

    token = _depth_var.set(depth + 1)
    try:
        from app.agent import AgentLoop
        from app.triggers.base import TriggerEvent

        room_id = _room_id_var.get()
        event = TriggerEvent(
            input=prompt,
            source="subagent",
            metadata={"room_id": room_id} if room_id else {},
        )
        result = await AgentLoop(llm=llm, tools=tools).run(event)
        return result or "(sub-agent returned no output)"
    finally:
        _depth_var.reset(token)


tool_manager.register(
    name="delegate_task",
    fn=_delegate_task,
    description=(
        "Delegate a sub-task to a fresh agent instance and receive the result inline. "
        "The sub-agent has full tool access and runs its own reasoning loop independently. "
        "Use this to decompose long multi-part tasks (avoiding step-limit exhaustion), "
        "or to run independent sub-tasks in parallel by calling delegate_task multiple times "
        "in a single step — the agent loop executes all tool calls concurrently. "
        "The result is returned to you, not sent to the user directly. "
        "For fire-and-forget work use start_background_task instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "A complete, self-contained instruction for the sub-agent.",
            },
        },
        "required": ["prompt"],
    },
    status_template="Delegating: {prompt:.80}",
)
