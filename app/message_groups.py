"""Shared helpers for grouping chat messages into atomic tool-call units.

An assistant message with tool_calls, the tool-result messages that follow it, and
an optional trailing nudge message must always be kept or dropped together —
splitting the group produces an orphaned `tool` message with no preceding
`assistant.tool_calls`, which most chat APIs reject.
"""

# Added as a user message after tool results to keep weak models anchored to the task.
# Using a user role message is more effective than embedding the nudge in tool result content.
TOOL_RESULT_NUDGE = (
    "(Tool calls complete. Now write your response to the user's question. Do not greet the user.)"
)


def atomic_groups(messages: list[dict], start: int = 0, end: int | None = None) -> list[list[int]]:
    """Partition messages[start:end] into atomic index groups.

    Each group is either a single unrelated message, or an assistant+tool_calls
    message together with its tool-result messages and optional trailing nudge.
    """
    if end is None:
        end = len(messages)
    groups: list[list[int]] = []
    i = start
    while i < end:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            group = [i]
            j = i + 1
            while j < end and messages[j].get("role") == "tool":
                group.append(j)
                j += 1
            if j < end and messages[j].get("content") == TOOL_RESULT_NUDGE:
                group.append(j)
                j += 1
            groups.append(group)
            i = j
        else:
            groups.append([i])
            i += 1
    return groups
