import argparse
import asyncio
import os

from dotenv import load_dotenv
load_dotenv()

from app.llm.base import BaseLLM
from app.llm.openai_provider import OpenAIProvider
from app.llm.text_tool_calling import TextToolCallingLLM
from app.server import CoreServer
from app.tools.builtin import tool_manager
import app.tools.memory      # registers memory tools onto tool_manager
import app.tools.reactions   # registers react tool onto tool_manager
import app.tools.fetch       # registers fetch_page tool onto tool_manager
import app.tools.background  # registers start_background_task tool onto tool_manager
import app.tools.files       # registers list_files, find_files, read_file, write_file onto tool_manager
import app.tools.share       # registers share_file onto tool_manager
import app.tools.calendar    # registers get_calendar_events onto tool_manager
import app.tools.reminder    # registers set_reminder, list_reminders, cancel_reminder onto tool_manager
import app.workspace_index   # registers search_files_semantic onto tool_manager
import app.tools.code        # registers run_python onto tool_manager
import app.tools.monitor     # registers watch_url, unwatch_url, list_watches onto tool_manager
import app.tools.scheduled_tasks  # registers create_scheduled_task, list_scheduled_tasks, cancel_scheduled_task onto tool_manager
import app.tools.subagent    # registers delegate_task onto tool_manager
import app.tools.tasks       # registers plan_steps, finish_step onto tool_manager
import app.tools.memory_consolidation  # registers consolidate_memories onto tool_manager
from app.triggers.cli import CLITrigger
from app.triggers.http import HTTPTrigger
from app.triggers.matrix import MatrixTrigger
import app.scheduler as _scheduler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pino AI Agent")
    parser.add_argument(
        "--mode",
        choices=["cli", "http", "matrix", "all"],
        default="cli",
        help="Which trigger(s) to activate (default: cli).",
    )
    parser.add_argument(
        "--message",
        type=str,
        default=None,
        help="One-shot message in CLI mode. Omit to run interactively.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive CLI mode even when --message is given.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=os.getenv("HTTP_HOST", "0.0.0.0"),
        help="HTTP server bind host (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("HTTP_PORT", "8000")),
        help="HTTP server port (default: 8000).",
    )
    return parser.parse_args()


FAST_MODEL = os.getenv("FAST_MODEL", "qwen2.5:1.5b")
DAILY_BRIEFING_TIME = os.getenv("DAILY_BRIEFING_TIME", "")
DAILY_BRIEFING_TZ = os.getenv("DAILY_BRIEFING_TZ", "UTC")
DAILY_BRIEFING_PROMPT = os.getenv(
    "DAILY_BRIEFING_PROMPT",
    "Use recall_memory to find the user's home location and any relevant preferences. "
    "Then provide a concise morning briefing: get the weather for that location, "
    "check today's calendar events, and give a friendly summary of the day ahead.",
)


TEXT_TOOL_CALLING = os.getenv("LLM_TEXT_TOOL_CALLING", "1") == "1"


async def run(args: argparse.Namespace) -> None:
    llm: BaseLLM = OpenAIProvider()
    if TEXT_TOOL_CALLING:
        llm = TextToolCallingLLM(llm)
    fast_llm = OpenAIProvider(model=FAST_MODEL) if FAST_MODEL else None
    server = CoreServer(llm=llm, tools=tool_manager, fast_llm=fast_llm)

    if args.mode in ("cli", "all"):
        interactive = args.interactive or (args.message is None)
        server.register_trigger(CLITrigger(message=args.message, interactive=interactive))

    if args.mode in ("http", "all"):
        server.register_trigger(HTTPTrigger(host=args.host, port=args.port))

    if args.mode in ("matrix", "all"):
        server.register_trigger(MatrixTrigger())

    # Start scheduler and restore pending reminders and watches
    _scheduler.start()
    app.tools.reminder.load_and_schedule_pending()
    app.tools.monitor.load_and_schedule_pending()
    app.tools.scheduled_tasks.load_and_schedule_pending(server)

    # Schedule daily briefing if configured
    if DAILY_BRIEFING_TIME:
        _setup_daily_briefing(server)

    # Schedule memory consolidation if configured
    app.tools.memory_consolidation.setup_consolidation_schedule(server)

    try:
        await server.start()
    finally:
        _scheduler.stop()


def _setup_daily_briefing(server: CoreServer) -> None:
    try:
        hour_str, minute_str = DAILY_BRIEFING_TIME.split(":")
        hour, minute = int(hour_str), int(minute_str)
    except ValueError:
        print(f"[scheduler] Invalid DAILY_BRIEFING_TIME={DAILY_BRIEFING_TIME!r} — expected HH:MM, skipping briefing.")
        return

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(DAILY_BRIEFING_TZ)
    except Exception as e:
        print(f"[scheduler] Invalid DAILY_BRIEFING_TZ={DAILY_BRIEFING_TZ!r} ({e}), using UTC.")
        import datetime
        tz = datetime.timezone.utc

    async def _daily_briefing() -> None:
        import datetime
        now = datetime.datetime.now(tz)
        now_str = now.strftime("%A, %Y-%m-%d %H:%M")
        prompt = (
            f"The current date and time is {now_str} ({DAILY_BRIEFING_TZ}). "
            f"{DAILY_BRIEFING_PROMPT}"
        )

        async def respond_fn(text: str) -> None:
            await _scheduler.fire_proactive(None, text)

        from app.triggers.base import TriggerEvent
        event = TriggerEvent(input=prompt, source="scheduler", respond_fn=respond_fn)
        try:
            await server.handle_event(event)
        except Exception as e:
            print(f"[scheduler] Daily briefing failed: {e}")

    _scheduler.get_scheduler().add_job(
        _daily_briefing,
        trigger="cron",
        hour=hour,
        minute=minute,
        timezone=tz,
        id="daily_briefing",
        replace_existing=True,
    )
    print(f"[scheduler] Daily briefing scheduled at {DAILY_BRIEFING_TIME} {DAILY_BRIEFING_TZ}.")


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
