from app.tools.builtin import tool_manager

# Ephemeral per-turn task state, keyed by room_id. Cleared at the start of each turn.
_tasks: dict[str, list[dict]] = {}
_current_room: str = "__default__"


def set_task_context(room_id: str | None) -> None:
    global _current_room
    _current_room = room_id or "__default__"
    _tasks[_current_room] = []


def _fmt(steps: list[dict]) -> str:
    lines = []
    for i, s in enumerate(steps):
        mark = "✓" if s["done"] else "○"
        line = f"  {i}. [{mark}] {s['step']}"
        if s["notes"]:
            line += f"\n       → {s['notes']}"
        lines.append(line)
    return "\n".join(lines)


def _plan_steps(steps: list[str]) -> str:
    _tasks[_current_room] = [{"step": s, "done": False, "notes": ""} for s in steps]
    return (
        f"Task plan created ({len(steps)} steps):\n{_fmt(_tasks[_current_room])}\n\n"
        "Work through these steps in order, calling finish_step after each one."
    )


def _finish_step(index: int, notes: str = "") -> str:
    steps = _tasks.get(_current_room, [])
    if not steps:
        return "Error: No task plan exists. Call plan_steps first."
    if index < 0 or index >= len(steps):
        return f"Error: Step index {index} is out of range (0–{len(steps) - 1})."
    steps[index]["done"] = True
    steps[index]["notes"] = notes
    summary = _fmt(steps)
    remaining = [i for i, s in enumerate(steps) if not s["done"]]
    if not remaining:
        return (
            f"All steps complete.\n\n{summary}\n\n"
            "You now have everything you need. Synthesize the results and give your final answer."
        )
    next_i = remaining[0]
    return (
        f"Step {index} done.\n\n{summary}\n\n"
        f"Next: step {next_i} — {steps[next_i]['step']}"
    )


tool_manager.register(
    name="plan_steps",
    fn=_plan_steps,
    description=(
        "Create an ordered task plan for the current request. Use this when a request requires "
        "multiple distinct steps (e.g. look up X, then calculate Y, then write Z). "
        "Do not use for simple single-step tasks. "
        "After creating the plan, work through each step and call finish_step when each one is done."
    ),
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered list of steps to complete the task.",
            },
        },
        "required": ["steps"],
    },
    status_template="Planning task...",
)

tool_manager.register(
    name="finish_step",
    fn=_finish_step,
    description=(
        "Mark a step in the current task plan as complete. "
        "Call this after completing each step, providing a brief note on the outcome. "
        "The response will show updated plan status and what to do next."
    ),
    parameters={
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "Zero-based index of the completed step.",
            },
            "notes": {
                "type": "string",
                "description": "Brief summary of what was found or done in this step.",
            },
        },
        "required": ["index"],
    },
    status_template="Completing step {index}...",
)
