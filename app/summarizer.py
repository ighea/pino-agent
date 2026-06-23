import os

from app.llm.base import BaseLLM

MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "20"))
SUMMARY_KEEP_RECENT = int(os.getenv("SUMMARY_KEEP_RECENT", "6"))

_PROMPT = (
    "Summarize the conversation below concisely. "
    "Preserve all key facts, decisions, names, numbers, and context the assistant will need later. "
    "Write in third person. Be brief but complete."
)


async def maybe_summarize(history: list[dict], llm: BaseLLM) -> list[dict]:
    if len(history) <= MAX_HISTORY_TURNS:
        return history

    keep = history[-SUMMARY_KEEP_RECENT:]
    to_summarize = history[:-SUMMARY_KEEP_RECENT]

    lines = []
    for msg in to_summarize:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if not content:
            continue
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
        elif role == "system":
            lines.append(f"[Context: {content}]")

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
