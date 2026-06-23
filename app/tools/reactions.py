import contextvars
from typing import Awaitable, Callable

from app.tools.builtin import tool_manager

_react_fn_var: contextvars.ContextVar[Callable[[str], Awaitable[None]] | None] = (
    contextvars.ContextVar("react_fn", default=None)
)


def set_react_fn(fn: Callable[[str], Awaitable[None]] | None) -> None:
    _react_fn_var.set(fn)


async def _react(emoji: str) -> str:
    fn = _react_fn_var.get()
    if fn is None:
        return f"Reaction {emoji} noted."
    await fn(emoji)
    return f"Reacted with {emoji}"


tool_manager.register(
    name="react",
    fn=_react,
    description=(
        "Add an emoji reaction to the user's message. Use to express tone naturally — "
        "e.g. 👍 for acknowledgement, ❤️ for warmth, 😄 for humor, 🤔 for uncertainty, "
        "🎉 for celebration. Don't overuse; reserve for moments where a reaction adds meaning "
        "beyond words."
    ),
    parameters={
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": "The emoji to react with, e.g. '👍', '❤️', '😄', '🤔', '🎉'.",
            },
        },
        "required": ["emoji"],
    },
    status_template="Reacting with {emoji}",
)
