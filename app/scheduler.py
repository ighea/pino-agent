import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.tz import TZ_NAME

_scheduler = AsyncIOScheduler(timezone=TZ_NAME)
_proactive_handlers: list = []  # async (room_id: str | None, text: str) -> None


def register_proactive_handler(fn) -> None:
    _proactive_handlers.append(fn)


async def fire_proactive(room_id: str | None, text: str) -> None:
    for fn in _proactive_handlers:
        try:
            await fn(room_id, text)
        except Exception as e:
            logging.warning(f"[scheduler] proactive handler error: {e}")


def get_scheduler() -> AsyncIOScheduler:
    return _scheduler


def start() -> None:
    if not _scheduler.running:
        _scheduler.start()


def stop() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
