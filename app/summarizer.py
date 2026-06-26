import os
from collections.abc import Awaitable, Callable

from app.llm.base import BaseLLM

# Trigger summarization when total history char count exceeds this.
MAX_HISTORY_CHARS = int(os.getenv("MAX_HISTORY_CHARS", "12000"))
# Target char budget to keep as the verbatim "recent" tail after summarizing.
SUMMARY_KEEP_CHARS = int(os.getenv("SUMMARY_KEEP_CHARS", "4000"))

_PROMPT = (
    "Summarize the conversation below concisely. "
    "Preserve all key facts, decisions, names, numbers, and context the assistant will need later. "
    "Write in third person. Be brief but complete."
)


def _msg_chars(msg: dict) -> int:
    return len(str(msg.get("content") or "")) + len(str(msg.get("tool_calls") or ""))


def _find_split_index(history: list[dict]) -> int:
    """Return the index where the verbatim 'keep' tail begins.

    We scan from the end until we've accumulated SUMMARY_KEEP_CHARS, then
    walk forward to the next user-message boundary so we never split inside
    a tool-call chain (assistant + one-or-more tool results).
    """
    cumulative = 0
    for i in range(len(history) - 1, -1, -1):
        cumulative += _msg_chars(history[i])
        if cumulative >= SUMMARY_KEEP_CHARS:
            for j in range(i, len(history)):
                if history[j].get("role") == "user":
                    return j
            return len(history)
    return 0


async def maybe_summarize(
    history: list[dict],
    llm: BaseLLM,
    notify: Callable[[], Awaitable[None]] | None = None,
) -> list[dict]:
    total = sum(_msg_chars(m) for m in history)
    if total <= MAX_HISTORY_CHARS:
        return history

    split = _find_split_index(history)
    if split == 0:
        return history

    if notify:
        await notify()

    to_summarize = history[:split]
    keep = history[split:]

    lines = []
    for msg in to_summarize:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls")
        if role == "user" and content:
            lines.append(f"User: {content}")
        elif role == "assistant":
            if content:
                lines.append(f"Assistant: {content}")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    lines.append(f"Assistant used tool: {fn.get('name', '?')}")
        elif role == "tool" and content:
            brief = content[:200].strip()
            if brief:
                lines.append(f"Tool result: {brief}")

    if not lines:
        return list(keep)

    try:
        response = await llm.chat([
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ])
        summary = response.choices[0].message.content or ""
    except Exception:
        return list(keep)

    return [{"role": "system", "content": f"[Earlier conversation summary]\n{summary}"}] + list(keep)


async def summarize_all(messages: list[dict], llm: BaseLLM) -> str | None:
    """Compress an arbitrary message list into a summary string with no size threshold.

    Unlike maybe_summarize, this always attempts compression — intended for forced
    paths (nuclear context strip, mid-turn tool chain compression). Returns None on
    failure or if there is nothing to summarize.
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls")
        if role == "user" and content:
            lines.append(f"User: {content[:500]}")
        elif role == "assistant":
            if content:
                lines.append(f"Assistant: {content[:500]}")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    lines.append(f"Assistant used tool: {fn.get('name', '?')}")
        elif role == "tool" and content:
            lines.append(f"Tool result: {content[:200].strip()}")

    if not lines:
        return None

    try:
        response = await llm.chat([
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ])
        summary = (response.choices[0].message.content or "").strip()
        return summary or None
    except Exception:
        return None
