import asyncio
import sys
from contextlib import suppress

from app.triggers.base import BaseTrigger, TriggerEvent


class CLITrigger(BaseTrigger):
    def __init__(self, message: str | None = None, interactive: bool = False) -> None:
        self.message = message
        self.interactive = interactive
        self._server = None
        self._history: list[dict] = []
        self._broadcast_queue: asyncio.Queue | None = None

    async def start(self, server) -> None:
        self._server = server
        self._broadcast_queue = server.get_broadcast_queue()
        if self.message:
            await self._fire(self.message)
        elif self.interactive:
            await self._interactive_loop()

    async def _fire(self, text: str) -> None:
        event = TriggerEvent(
            input=text,
            source="cli",
            history=self._history,
            respond_fn=self._respond,
            status_fn=self._status,
            react_fn=self._react,
            deliver_fn=self._deliver,
        )
        await self._server.handle_event(event)

    @staticmethod
    async def _respond(text: str) -> None:
        print(f"\n{text}\n")

    @staticmethod
    async def _status(text: str) -> None:
        if text.startswith("Error:"):
            print(f"  ✗ {text}")
        else:
            print(f"  → {text}")

    @staticmethod
    async def _react(emoji: str) -> None:
        print(f"  {emoji}")

    @staticmethod
    async def _deliver(path: str) -> str:
        from app.tools.files import WORKSPACE_DIR
        full = WORKSPACE_DIR / path
        print(f"  File: {full}")
        return f"File available at: {full}"

    async def _interactive_loop(self) -> None:
        print("Pino ready. Type a message and press Enter (Ctrl+C or Ctrl+D to quit).\n")
        loop = asyncio.get_running_loop()
        broadcast_task = asyncio.create_task(self._watch_broadcasts())
        try:
            while True:
                try:
                    sys.stdout.write("> ")
                    sys.stdout.flush()
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    if not line:
                        break
                    text = line.strip()
                    if text:
                        await self._fire(text)
                except (KeyboardInterrupt, EOFError):
                    print("\nGoodbye.")
                    break
        finally:
            broadcast_task.cancel()
            with suppress(asyncio.CancelledError):
                await broadcast_task

    async def _watch_broadcasts(self) -> None:
        while True:
            if self._broadcast_queue is None:
                return
            text = await self._broadcast_queue.get()
            sys.stdout.write(f"\r\n[scheduler] {text}\n\n> ")
            sys.stdout.flush()

    async def stop(self) -> None:
        pass
