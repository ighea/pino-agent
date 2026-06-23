import asyncio

from app.agent import AgentLoop
from app.llm.base import BaseLLM
from app.tools.manager import ToolManager
from app.triggers.base import BaseTrigger, TriggerEvent


class CoreServer:
    def __init__(self, llm: BaseLLM, tools: ToolManager, fast_llm: BaseLLM | None = None) -> None:
        self.llm = llm
        self.tools = tools
        self.agent = AgentLoop(llm, tools, fast_llm=fast_llm)
        self._triggers: list[BaseTrigger] = []
        self._broadcast_queue: asyncio.Queue = asyncio.Queue()

    def register_trigger(self, trigger: BaseTrigger) -> None:
        self._triggers.append(trigger)

    def get_broadcast_queue(self) -> asyncio.Queue:
        return self._broadcast_queue

    async def broadcast(self, text: str) -> None:
        await self._broadcast_queue.put(text)

    async def handle_event(self, event: TriggerEvent) -> str:
        result = await self.agent.run(event)
        if event.respond_fn and result:
            await event.respond_fn(result)
        return result

    async def start(self) -> None:
        if not self._triggers:
            print("Warning: no triggers registered, nothing to do.")
            return
        await asyncio.gather(*[t.start(self) for t in self._triggers])

    async def stop(self) -> None:
        await asyncio.gather(*[t.stop() for t in self._triggers])
