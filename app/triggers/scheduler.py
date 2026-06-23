import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.logger import logger
from app.triggers.base import BaseTrigger, TriggerEvent


class SchedulerTrigger(BaseTrigger):
    def __init__(self, jobs: list[dict]) -> None:
        # jobs format: [{"cron": "0 9 * * *", "message": "Daily summary"}]
        self._jobs = jobs
        self._scheduler: AsyncIOScheduler | None = None
        self._server = None

    async def start(self, server) -> None:
        self._server = server
        self._scheduler = AsyncIOScheduler()
        for job in self._jobs:
            minute, hour, day, month, day_of_week = job["cron"].split()
            self._scheduler.add_job(
                self._fire,
                "cron",
                args=[job["message"]],
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
            )
        self._scheduler.start()
        print(f"Scheduler trigger running {len(self._jobs)} job(s).")
        await asyncio.Event().wait()

    async def _fire(self, message: str) -> None:
        event = TriggerEvent(
            input=message,
            source="scheduler",
            respond_fn=self._handle_response,
        )
        await self._server.handle_event(event)

    async def _handle_response(self, text: str) -> None:
        logger.log_event("SCHEDULER_RESPONSE", {"output": text})
        await self._server.broadcast(text)

    async def stop(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown()
