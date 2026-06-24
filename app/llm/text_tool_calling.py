"""
ToolCallCompatLLM — a thin wrapper that lets any model's tool-call response reach
agent.py in a consistent shape, regardless of which variant the underlying model/
Ollama shim actually produces.

Known gemma4 / Ollama quirks we handle:
  1. tool_calls populated but finish_reason == "stop" (wrong finish_reason)
  2. Tool call JSON embedded in text content instead of tool_calls field
  3. Truly empty content — passed through so agent.py retry logic fires
"""
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import BaseLLM
from app.logger import logger


# ---------------------------------------------------------------------------
# Normalised response shape (mirrors the openai SDK objects agent.py touches)
# ---------------------------------------------------------------------------

@dataclass
class _Function:
    name: str
    arguments: str  # JSON string


@dataclass
class _ToolCall:
    id: str
    function: _Function
    type: str = "function"


@dataclass
class _Message:
    content: str | None
    tool_calls: list[_ToolCall] | None = None


@dataclass
class _Choice:
    message: _Message
    finish_reason: str


@dataclass
class _Response:
    choices: list[_Choice]


# ---------------------------------------------------------------------------
# Patterns for tool calls embedded inside plain-text content
# ---------------------------------------------------------------------------

# {"name": "tool_name", "arguments": {...}}  — top-level JSON object
_JSON_TOOL_RE = re.compile(
    r'\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*"arguments"\s*:\s*(\{[^{}]*\})[^{}]*\}',
    re.DOTALL,
)
# ```json\n{...}\n```
_CODEBLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_embedded_tool_call(content: str) -> _ToolCall | None:
    """Try to find a tool call encoded in plain text content."""
    candidates: list[str] = []

    for m in _JSON_TOOL_RE.finditer(content):
        candidates.append(m.group(0))
    for m in _CODEBLOCK_RE.finditer(content):
        candidates.append(m.group(1))

    for raw in candidates:
        try:
            data = json.loads(raw)
            name = data.get("name")
            args = data.get("arguments", {})
            if name and isinstance(name, str):
                return _ToolCall(
                    id=f"tc_{uuid.uuid4().hex[:12]}",
                    function=_Function(name=name, arguments=json.dumps(args)),
                )
        except (json.JSONDecodeError, TypeError):
            continue

    return None


def _normalise(raw: Any) -> _Response:
    """Copy an openai-SDK response (or our own _Response) into a _Response."""
    choice = raw.choices[0]
    msg = choice.message
    content = msg.content
    finish_reason = choice.finish_reason

    tool_calls: list[_ToolCall] | None = None
    if msg.tool_calls:
        tool_calls = [
            _ToolCall(
                id=tc.id,
                function=_Function(name=tc.function.name, arguments=tc.function.arguments),
            )
            for tc in msg.tool_calls
        ]

    # Quirk 1: tool_calls present but finish_reason is wrong
    if tool_calls and finish_reason != "tool_calls":
        logger.log_event("TOOL_CALL_COMPAT", {
            "reason": "tool_calls present but finish_reason was not 'tool_calls'",
            "original_finish_reason": finish_reason,
            "tools": [tc.function.name for tc in tool_calls],
        })
        finish_reason = "tool_calls"

    # Quirk 2: tool call embedded in text content
    if not tool_calls and content:
        embedded = _extract_embedded_tool_call(content)
        if embedded:
            logger.log_event("TOOL_CALL_COMPAT", {
                "reason": "tool call found embedded in text content",
                "tool": embedded.function.name,
                "raw_snippet": content[:300],
            })
            tool_calls = [embedded]
            content = None
            finish_reason = "tool_calls"

    return _Response(choices=[_Choice(
        message=_Message(content=content, tool_calls=tool_calls),
        finish_reason=finish_reason,
    )])


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------

class TextToolCallingLLM(BaseLLM):
    """
    Passes tools natively to the underlying LLM and normalises the response to
    the shape agent.py expects, working around format quirks from Ollama models.
    """

    def __init__(self, inner: BaseLLM) -> None:
        self._inner = inner

    @property
    def model(self) -> str:
        return self._inner.model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        import json as _json
        msg_chars = sum(
            len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
            for m in messages
        )
        tool_chars = len(_json.dumps(tools)) if tools else 0
        total_chars = msg_chars + tool_chars
        logger.log_event("LLM_REQUEST", {
            "model": self._inner.model,
            "messages": len(messages),
            "tools": len(tools) if tools else 0,
            "msg_chars": msg_chars,
            "tool_chars": tool_chars,
            "total_chars": total_chars,
            "total_tokens_est": total_chars // 4,
        })

        raw = await self._inner.chat(messages, tools=tools, max_tokens=max_tokens)

        choice = raw.choices[0]
        msg = choice.message
        usage = getattr(raw, "usage", None)
        logger.log_event("LLM_RAW_RESPONSE", {
            "model": self._inner.model,
            "finish_reason": choice.finish_reason,
            "has_tool_calls": bool(msg.tool_calls),
            "tool_names": [tc.function.name for tc in msg.tool_calls] if msg.tool_calls else [],
            "content_length": len(msg.content or ""),
            "content_preview": (msg.content or "")[:200],
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
        })

        return _normalise(raw)
