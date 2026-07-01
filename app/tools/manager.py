import asyncio
import traceback
from typing import Callable

from app.logger import logger


class ToolManager:
    def __init__(self) -> None:
        self._tools: dict[str, dict] = {}

    def register(
        self,
        name: str,
        fn: Callable,
        description: str,
        parameters: dict,
        status_template: str | None = None,
    ) -> None:
        self._tools[name] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
            # e.g. "Searching the web for: {query}"
            "status_template": status_template or f"Using tool: {name}",
        }

    def get_status(self, name: str, args: dict) -> str:
        entry = self._tools.get(name)
        if not entry:
            return f"Using tool: {name}"
        try:
            return entry["status_template"].format(**args)
        except KeyError:
            return entry["status_template"]

    def get_openai_schemas(self) -> list[dict]:
        return [entry["schema"] for entry in self._tools.values()]

    def call(self, name: str, **kwargs) -> str:
        if name not in self._tools:
            return f"Error: tool '{name}' is not registered."
        try:
            result = self._tools[name]["fn"](**kwargs)
            return str(result)
        except Exception as e:
            logger.log_error(
                f"Tool '{name}' raised an exception",
                {"tool": name, "args": kwargs, "error": str(e), "traceback": traceback.format_exc()},
            )
            return f"Error: tool '{name}' raised an exception: {e}"

    def is_async(self, name: str) -> bool:
        entry = self._tools.get(name)
        return entry is not None and asyncio.iscoroutinefunction(entry["fn"])

    async def async_call(self, name: str, **kwargs) -> str:
        if name not in self._tools:
            return f"Error: tool '{name}' is not registered."
        try:
            result = await self._tools[name]["fn"](**kwargs)
            return str(result)
        except Exception as e:
            logger.log_error(
                f"Tool '{name}' raised an exception",
                {"tool": name, "args": kwargs, "error": str(e), "traceback": traceback.format_exc()},
            )
            return f"Error: tool '{name}' raised an exception: {e}"

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())
