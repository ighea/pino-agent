# Pino — Agent & Contributor Instructions

## Project layout

```text
pino/
├── main.py                      # Entry point — parse args, load .env, start server + scheduler
├── app/
│   ├── server.py                # CoreServer: trigger registry + event dispatch
│   ├── agent.py                 # AgentLoop: LLM ↔ tool call orchestration + quick ack
│   ├── summarizer.py            # Conversation summarization (compresses old history turns)
│   ├── logger.py                # Structured JSONL logging → agent_history.jsonl
│   ├── scheduler.py             # APScheduler singleton + proactive message handler registry
│   ├── triggers/
│   │   ├── base.py              # TriggerEvent dataclass + BaseTrigger interface
│   │   ├── cli.py               # Interactive and one-shot CLI
│   │   ├── http.py              # FastAPI HTTP trigger + /files/ file-serving route
│   │   └── matrix.py            # Matrix bot trigger (E2EE, text/image/file input, proactive send)
│   ├── llm/
│   │   ├── base.py              # BaseLLM abstract interface
│   │   └── openai_provider.py   # OpenAI-compatible provider (Ollama by default)
│   └── tools/
│       ├── manager.py           # ToolManager: registry, schema generation, sync/async dispatch
│       ├── builtin.py           # search_web, get_weather, calculate, get_datetime + shared tool_manager
│       ├── memory.py            # save_memory, recall_memory, delete_memory (JSON + embeddings)
│       ├── reactions.py         # react tool (ContextVar-based, async)
│       ├── fetch.py             # fetch_page tool (SSRF-safe web page text extraction)
│       ├── background.py        # start_background_task tool (asyncio tasks)
│       ├── files.py             # list_files, find_files, read_file, write_file, download_file
│       ├── share.py             # share_file tool (ContextVar-based, trigger-aware delivery)
│       ├── calendar.py          # get_calendar_events (multi-calendar ICS, RRULE support)
│       └── reminder.py          # set_reminder, list_reminders, cancel_reminder (APScheduler)
├── .env.example                 # Documents all env vars — copy to .env and fill in
├── .claudeignore                # Prevents Claude from reading .env and memory.json
├── requirements.txt
└── docker-compose.yml
```

## Core concepts

### TriggerEvent

All input enters the system as a `TriggerEvent` (defined in `app/triggers/base.py`):

```python
@dataclass
class TriggerEvent:
    input: str                                          # the user's message
    source: str                                         # "cli", "http", "matrix", "scheduler", …
    id: str                                             # auto-generated UUID
    metadata: dict                                      # trigger-specific extras
    history: list[dict]                                 # mutable conversation history (updated in-place)
    respond_fn:  Callable[[str], Awaitable[None]]       # called with the final answer (and quick ack)
    status_fn:   Callable[[str], Awaitable[None]]       # called during tool use
    react_fn:    Callable[[str], Awaitable[None]]       # called to attach an emoji reaction
    deliver_fn:  Callable[[str], Awaitable[str]]        # called to deliver a workspace file
```

All callbacks are optional (can be `None`). The agent never knows or cares which trigger produced the event.

### AgentLoop

`app/agent.py` runs an iterative loop:

1. If history is long, call `maybe_summarize()` to compress old turns.
2. Set per-request ContextVars: `react_fn`, `deliver_fn`, background context, `reminder_room_id`.
3. If `fast_llm` is set and `respond_fn` is present, fire `_quick_ack()` as a concurrent asyncio task — it sends a short acknowledgment (with a topic emoji) before the main loop starts.
4. Build `[system, …history, user]` message list. The system prompt includes the current UTC datetime so the LLM can calculate reminder times correctly.
5. Call the primary LLM with all registered tool schemas (with exponential-backoff retry).
6. If the response contains `tool_calls`: emit a status message, execute all tools concurrently via `asyncio.gather`, append `tool` messages, loop.
7. If the response is plain text: call `event.respond_fn(text)` and return. Empty responses are retried up to 2 times before returning a user-facing error.

Maximum steps default is 10. Tool errors are surfaced immediately via `status_fn`.

### ContextVar isolation

Per-request state (`react_fn`, `deliver_fn`, `llm`, `tools`, `push_fn` for background tasks, `reminder_room_id`) is stored in `contextvars.ContextVar`. Each asyncio task inherits a copy of the context at creation time, so concurrent requests from different rooms or HTTP sessions cannot interfere with each other.

### Proactive messages (scheduler)

`app/scheduler.py` holds a singleton `AsyncIOScheduler` and a list of registered proactive handlers. Any component that can send unsolicited messages (e.g. `MatrixTrigger`) registers an async `(room_id: str | None, text: str) -> None` handler at startup. Callers use `fire_proactive(room_id, text)` — `room_id=None` broadcasts to all configured rooms.

The scheduler is started in `main.py` before triggers and stopped in the `finally` block. Reminders are persisted to `REMINDERS_FILE` (default: `reminders.json`) and rescheduled on startup via `load_and_schedule_pending()`.

### ToolManager

`app/tools/manager.py` holds the tool registry. Each entry stores the callable `fn`, an OpenAI-format JSON schema, and a `status_template` string. Key methods:

- `register(name, fn, description, parameters, status_template)` — add a tool
- `call(name, **kwargs)` — synchronous dispatch (used via `run_in_executor`)
- `async_call(name, **kwargs)` — async dispatch for coroutine tools
- `is_async(name)` — returns `True` if the registered function is a coroutine function
- `get_openai_schemas()` — returns the list of tool schemas for the LLM
- `get_status(name, args)` — formats the status message from the template

The shared `tool_manager` instance lives in `app/tools/builtin.py` and is imported by all tool modules.

### Conversation summarization

`app/summarizer.py` exports `maybe_summarize(history, llm)`. When `len(history) > MAX_HISTORY_TURNS`, it summarizes all but the most recent `SUMMARY_KEEP_RECENT` turns into a single `system` message using the primary LLM. Called at the start of each `AgentLoop.run()`.

### Workspace file tools

All file tools operate inside `WORKSPACE_DIR` (default: `<project_root>/workspace`). `_safe_path()` in `app/tools/files.py` resolves every user-supplied path and rejects anything that escapes the workspace root via `Path.relative_to()`. The workspace directory is created on first use.

`share_file` in `app/tools/share.py` uses a `deliver_fn` ContextVar. Each trigger sets its own implementation:

| Trigger | Behaviour |
| --- | --- |
| HTTP | Returns a `GET /files/{path}` URL; emits `{"type": "file", ...}` SSE event |
| Matrix | Uploads to homeserver media API; sends `m.file`/`m.image`/`m.video`/`m.audio` event |
| CLI | Prints and returns the absolute workspace path |

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_BASE_URL` | `http://host.docker.internal:11434/v1` | LLM endpoint |
| `OPENAI_API_KEY` | `ollama` | API key |
| `OPENAI_MODEL` | `gemma4:latest` | Primary model name |
| `FAST_MODEL` | `qwen2.5:1.5b` | Quick-ack model; set empty to disable |
| `BRAVE_API_KEY` | — | Brave Search (required for web search) |
| `OPENWEATHERMAP_API_KEY` | — | OpenWeatherMap (required for weather) |
| `MEMORY_FILE` | `memory.json` | Persistent memory store path |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for semantic memory |
| `MAX_HISTORY_TURNS` | `20` | Summarize when history exceeds this |
| `SUMMARY_KEEP_RECENT` | `6` | Verbatim turns kept after summarization |
| `WORKSPACE_DIR` | `<project_root>/workspace` | Sandboxed directory for file tools |
| `HTTP_HOST` | `0.0.0.0` | HTTP trigger bind host |
| `HTTP_PORT` | `8000` | HTTP trigger port |
| `HTTP_API_KEY` | — | Bearer token for HTTP auth; unset = open |
| `HTTP_PUBLIC_URL` | `http://localhost:{port}` | Base URL for `share_file` download links |
| `MATRIX_HOMESERVER` | — | Matrix homeserver URL |
| `MATRIX_USER` | — | Bot Matrix ID |
| `MATRIX_PASSWORD` | — | Bot account password |
| `MATRIX_ROOM_IDS` | — | Comma-separated room IDs |
| `MATRIX_STORE_PATH` | `./nio_store` | E2EE key storage path |
| `MATRIX_MAX_MSG_LEN` | `4000` | Split Matrix messages at this character count |
| `CALENDAR_<name>` | — | ICS URL for a named calendar (e.g. `CALENDAR_WORK=…`) |
| `REMINDERS_FILE` | `reminders.json` | Persistent reminder store path |
| `DAILY_BRIEFING_TIME` | — | `HH:MM` to fire the daily briefing (unset = disabled) |
| `DAILY_BRIEFING_TZ` | `UTC` | Timezone for `DAILY_BRIEFING_TIME` (e.g. `Europe/Helsinki`) |
| `DAILY_BRIEFING_PROMPT` | see .env.example | Agent prompt for the daily briefing |

## Running

```bash
python main.py --mode cli              # interactive CLI
python main.py --mode cli --message "…" # one-shot
python main.py --mode http             # HTTP only
python main.py --mode matrix           # Matrix bot only
python main.py --mode all              # all triggers concurrently
```

Docker: `docker compose up` starts with `--mode all` by default.

## Extending

### Add a tool

1. Write a plain Python function (or coroutine) that accepts keyword arguments and returns a string.
1. Register it on `tool_manager`:

```python
from app.tools.builtin import tool_manager

tool_manager.register(
    name="my_tool",
    fn=my_function,           # sync or async — both work
    description="What this tool does.",
    parameters={
        "type": "object",
        "properties": {
            "arg": {"type": "string", "description": "…"},
        },
        "required": ["arg"],
    },
    status_template="Running my_tool with: {arg}",
)
```

1. Import the module as a side-effect in `main.py` (after `load_dotenv()`).

Async tools are awaited directly in the agent loop. Sync tools run in `asyncio.run_in_executor`. For tools that need per-request state (e.g. a callback from `TriggerEvent`), use `contextvars.ContextVar` and set it in `AgentLoop.run()`.

### Add a trigger

1. Subclass `BaseTrigger` from `app/triggers/base.py`.
1. Implement `async def start(self, server: CoreServer)` — produce `TriggerEvent` objects and pass them to `await server.handle_event(event)`.
1. Implement `async def stop(self)`.
1. Register it in `main.py`: `server.register_trigger(MyTrigger(…))`.

Provide all four callbacks (`respond_fn`, `status_fn`, `react_fn`, `deliver_fn`) as appropriate for your channel.

## Conventions

- **No code execution in tools**: the calculator uses `ast` parsing with an explicit allowlist — never use `eval()`.
- **SSRF guard on all outbound requests**: import `_check_url` from `app/tools/fetch.py` and call it before any HTTP request to a user-supplied URL.
- **API keys never in error messages**: catch HTTP error codes explicitly; return plain string error messages.
- **All async**: triggers, agent loop, tool dispatch (sync tools run in `run_in_executor`).
- **No comments explaining what code does** — only for non-obvious constraints, invariants, or workarounds.
- **`.env` and `memory.json` are gitignored and claudeignored** — never commit them.

