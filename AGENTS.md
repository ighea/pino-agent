# Pino — Agent & Contributor Instructions

## Project layout

```text
pino/
├── main.py                      # Entry point — parse args, load .env, start server + scheduler
├── app/
│   ├── server.py                # CoreServer: trigger registry + event dispatch
│   ├── agent.py                 # AgentLoop: LLM ↔ tool call orchestration + quick ack
│   ├── summarizer.py            # Conversation summarization (char-based, preserves tool-call chains)
│   ├── history.py               # Per-session history persistence (load/save to data/history/)
│   ├── embed.py                 # Shared embedding utilities (embed(), cosine()) — used by memory + workspace index
│   ├── workspace_index.py       # On-demand vector index over workspace files + search_files_semantic tool
│   ├── logger.py                # Structured JSONL logging → data/agent_history.jsonl
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
│       ├── builtin.py           # search_web, search_images, get_weather, calculate, get_datetime + shared tool_manager
│       ├── memory.py            # save_memory, recall_memory, delete_memory (JSON + embeddings via app.embed)
│       ├── reactions.py         # react tool (ContextVar-based, async)
│       ├── fetch.py             # fetch_page tool (SSRF-safe web page text extraction)
│       ├── background.py        # start_background_task tool (asyncio tasks, fire_proactive fallback)
│       ├── files.py             # list_files, find_files, read_file, write_file, append_file, patch_file, download_file, search_files
│       ├── share.py             # share_file tool (ContextVar-based, trigger-aware delivery)
│       ├── calendar.py          # get_calendar_events (multi-calendar ICS, RRULE support)
│       ├── reminder.py          # set_reminder, list_reminders, cancel_reminder (APScheduler date jobs)
│       ├── monitor.py           # watch_url, unwatch_url, list_watches (APScheduler interval jobs)
│       ├── scheduled_tasks.py   # create_scheduled_task, list_scheduled_tasks, cancel_scheduled_task (APScheduler interval/cron jobs)
│       ├── subagent.py          # delegate_task (inline sub-agent with depth limit, parallel-friendly)
│       ├── tasks.py             # plan_steps, finish_step (ephemeral per-turn task tracking)
│       ├── code.py              # run_python (sandboxed subprocess execution, workspace-only I/O)
│       └── memory_consolidation.py  # consolidate_memories + scheduled job: learn from history, compact core memories
├── data/                        # Runtime data — gitignored; created automatically on first run
│   ├── agent_history.jsonl      # Structured JSONL event log
│   ├── memory.json              # Persistent memory store
│   ├── reminders.json           # Pending reminders
│   ├── watches.json             # Active URL watches
│   ├── scheduled_tasks.json     # Recurring scheduled task store
│   ├── memory_consolidation_state.json  # Last-run timestamp for incremental consolidation scans
│   ├── workspace_index.json     # Vector index for search_files_semantic
│   ├── history/                 # Per-session conversation history files
│   ├── nio_store/               # Matrix E2EE session keys
│   └── workspace/               # Sandboxed file tool + code execution workspace
├── .env.example                 # Documents all env vars — copy to .env and fill in
├── .claudeignore                # Prevents Claude from reading .env and data/
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
    metadata: dict                                      # trigger-specific extras (room_id, sender, …)
    history: list[dict]                                 # mutable conversation history (updated in-place by AgentLoop)
    respond_fn:  Callable[[str], Awaitable[None]]       # called with the final answer (and quick ack)
    status_fn:   Callable[[str], Awaitable[None]]       # called during tool use
    react_fn:    Callable[[str], Awaitable[None]]       # called to attach an emoji reaction
    deliver_fn:  Callable[[str], Awaitable[str]]        # called to deliver a workspace file
```

All callbacks are optional (can be `None`). The agent never knows or cares which trigger produced the event.

### AgentLoop

`app/agent.py` runs an iterative loop:

1. If history is large (by char count), call `maybe_summarize()` to compress old turns.
2. Set per-request ContextVars: `react_fn`, `deliver_fn`, background context, `reminder_room_id`, `monitor_room_id`.
3. If `fast_llm` is set and `respond_fn` is present, fire `_quick_ack()` as a concurrent asyncio task — it sends a short acknowledgment (with a topic emoji) before the main loop starts.
4. Build `[system, …history, user]` message list. The system prompt includes the current datetime (in `AGENT_TZ`) and core memories so the LLM always has essential context.
5. Call the primary LLM with all registered tool schemas (with exponential-backoff retry).
6. If the response contains `tool_calls`: emit a status message, execute all tools concurrently via `asyncio.gather`, append `tool` result messages, then append a single `role: user` nudge message to anchor weak models that otherwise emit an empty stop token after tool use. The nudge is stripped from `event.history` before it is persisted.
7. If the response is plain text: call `event.respond_fn(text)` and return. Empty responses are retried up to 2 times before returning a user-facing error.
8. If `finish_reason == "length"` (context overflow): strip history, keep only `[system, user, …tool_chain]`, retry once.

At the end of a successful turn `event.history` is updated in-place with all messages from this turn (minus the system prompt). The caller (trigger) then persists this to disk.

Maximum steps default is 10. Tool errors are surfaced immediately via `status_fn`.

### ContextVar isolation

Per-request state (`react_fn`, `deliver_fn`, `llm`, `tools`, `push_fn` for background tasks, `reminder_room_id`, `monitor_room_id`) is stored in `contextvars.ContextVar`. Each asyncio task inherits a copy of the context at creation time, so concurrent requests from different rooms or HTTP sessions cannot interfere with each other.

### Conversation history persistence

`app/history.py` provides two functions:

- `load(session_id) -> list[dict]` — reads `data/history/<safe_id>.json`; returns `[]` if missing
- `save(session_id, history)` — writes up to `MAX_PERSISTED_HISTORY` (default 200) messages

Session IDs are sanitized (non-alphanumeric chars replaced with `_`) before use as filenames. Matrix triggers use `room_id` as the session ID; HTTP triggers use the caller-supplied `session_id`.

Triggers load history from disk at the start of each event and save it after `handle_event` returns. The per-room lock in `MatrixTrigger` and the per-session lock in `HTTPTrigger` ensure only one request per session modifies history at a time.

### Conversation summarization

`app/summarizer.py` exports `maybe_summarize(history, llm)`. Triggered when `sum(chars) > MAX_HISTORY_CHARS` (default 12 000). The split point is found by scanning from the end until `SUMMARY_KEEP_CHARS` (default 4 000) chars are accumulated, then snapping forward to the next **user message** boundary — this ensures tool-call chains (assistant + one-or-more tool result messages) are never split. Tool call names and brief result snippets are included in the summarization prompt for better context retention.

### Embedding utilities

`app/embed.py` provides `embed(text) -> list[float] | None` and `cosine(a, b) -> float`. Both `app/tools/memory.py` and `app/workspace_index.py` import from this shared module. The embedding client is a lazy singleton pointing at `OPENAI_BASE_URL` with model `EMBEDDING_MODEL`.

### Workspace semantic index

`app/workspace_index.py` maintains a JSON vector index at `WORKSPACE_INDEX_FILE`. When `search_files_semantic` is called:

1. All workspace files under the target path are scanned (max 50 files, max 50 KB per file).
2. Files with a stale or missing mtime in the index are re-chunked (400-char chunks, 80-char overlap, max 20 chunks per file) and re-embedded.
3. The updated index is saved to disk.
4. The query is embedded and cosine similarity is computed against all stored chunks.
5. Top-5 chunks with score ≥ 0.35 are returned with file path, relevance score, and a 300-char snippet.

### Code execution sandbox

`app/tools/code.py` implements `run_python(code, timeout)`. Before execution, a preamble is prepended to the user's code that:

- Overrides `builtins.open` with a path-checking wrapper — `realpath` is used so symlinks can't escape; both the exact workspace path and paths under it are allowed
- Replaces `subprocess.run/Popen/call/…` and `os.system/popen/execv/…` with a `PermissionError`-raising stub
- Wraps everything in a function to avoid polluting the user's global namespace

The subprocess runs with `cwd=WORKSPACE_DIR`, a minimal environment (`PATH`, `PYTHONIOENCODING`, `HOME=WORKSPACE_DIR`), and optionally `RLIMIT_CPU` via the `resource` module (Linux only). Output is capped at `CODE_MAX_OUTPUT_CHARS`.

### Proactive messages (scheduler)

`app/scheduler.py` holds a singleton `AsyncIOScheduler` and a list of registered proactive handlers. Any component that can send unsolicited messages (e.g. `MatrixTrigger`) registers an async `(room_id: str | None, text: str) -> None` handler at startup. Callers use `fire_proactive(room_id, text)` — `room_id=None` broadcasts to all configured rooms.

The scheduler is started in `main.py` before triggers and stopped in the `finally` block. On startup, `app.tools.reminder.load_and_schedule_pending()`, `app.tools.monitor.load_and_schedule_pending()`, and `app.tools.scheduled_tasks.load_and_schedule_pending(server)` restore persisted jobs.

### URL monitoring

`app/tools/monitor.py` stores watches in `WATCHES_FILE` (default: `data/watches.json`). Each watch entry records the URL, interval, room_id, and last content hash. APScheduler `interval` jobs call `_check_watch(watch_id)` which:

1. Fetches the URL (SSRF-protected via `_check_url`)
2. Extracts text (Trafilatura for HTML; raw bytes otherwise)
3. SHA-256 hashes the content
4. If the hash differs from the stored baseline, calls `fire_proactive(room_id, notification)` and updates the stored hash
5. First successful fetch only stores the baseline — no notification

The `monitor_room_id` ContextVar is set in `AgentLoop.run()` alongside `reminder_room_id` and `scheduled_task_room_id`, so watches created from a Matrix room are automatically associated with that room.

### Recurring scheduled tasks

`app/tools/scheduled_tasks.py` stores tasks in `SCHEDULED_TASKS_FILE` (default: `data/scheduled_tasks.json`). Each entry records the prompt, label, schedule (interval minutes or cron expression), room_id, and run statistics.

`load_and_schedule_pending(server)` is called from `main.py` after the scheduler starts and requires a `server` reference (unlike reminders/monitors which only need the scheduler) because tasks execute via `server.handle_event()`, which wires up the full agent loop.

When a scheduled task fires:

1. The run count and `last_run` timestamp are updated in the JSON store.
2. A `TriggerEvent` is created with `source="scheduler"` and a `respond_fn` that calls `fire_proactive(room_id, ...)`.
3. `server.handle_event(event)` runs the full agent loop; the final result is delivered proactively.

### Memory consolidation

`app/tools/memory_consolidation.py` provides a scheduled job and an on-demand `consolidate_memories` tool. On each run:

1. **History scan** — reads `data/history/*.json` files whose `mtime` is newer than the `last_run` timestamp stored in `MEMORY_CONSOLIDATION_STATE_FILE`. Extracts up to `MEMORY_CONSOLIDATION_MAX_HISTORY_CHARS` of user/assistant text (last 40 messages per file, newest sessions first). Tool result messages are excluded.
2. **Prompt construction** — embeds the excerpts in a self-contained prompt together with current core memory count and compaction instructions. `last_run` is written to the state file **before** the agent runs so that a partial failure doesn't cause the same messages to be reprocessed.
3. **Agent run** — fires `server.handle_event()` with `source="scheduler"`, giving the agent full access to `save_memory`, `recall_memory`, and `delete_memory`. The agent extracts learnings and calls `save_memory` for each, then reviews all memories for redundancy/staleness and calls `delete_memory` on any duplicates or outdated entries.
4. **Core compaction** — when core memory count ≥ `MEMORY_CONSOLIDATION_CORE_THRESHOLD` (default 8), the prompt instructs the agent to merge entries, demote non-universal facts to `preference` or `personal`, and re-save consolidated versions.

Scheduling mirrors the daily briefing: `setup_consolidation_schedule(server)` registers either a cron or interval APScheduler job. An on-demand `consolidate_memories` tool is also registered so the agent (or user) can trigger a run at any time.

`set_consolidation_server(server)` — called by `main.py` at startup — stores the server reference so the tool can invoke the agent loop.

### Background task notifications

`app/tools/background.py` now stores the `room_id` alongside the LLM, tools, and push_fn in ContextVars. When a background task completes it first tries `push_fn` (the `respond_fn` from the original request); if that fails or is absent it falls back to `fire_proactive(room_id, ...)`. The room_id is also injected into the background `TriggerEvent`'s metadata so reminders and watches set within the background task are associated with the correct room.

### Task planning

`app/tools/tasks.py` implements two ephemeral tools for optional per-turn task tracking:

- **`plan_steps(steps)`** — the agent calls this at the start of a complex multi-step request. It stores an ordered checklist in a module-level dict keyed by `room_id` and returns the formatted list along with an instruction to work through it sequentially.
- **`finish_step(index, notes)`** — marks a step done, records a brief outcome note, and returns the updated checklist. The response names the next pending step (or tells the agent to synthesise if all are done).

State is scoped per room and cleared at the start of every turn via `set_task_context(room_id)` in `AgentLoop.run()`. State never persists across turns. The agent chooses whether to use these tools — they add overhead and the system prompt instructs the agent to skip them for simple requests.

### Sub-agents

`app/tools/subagent.py` implements `delegate_task`. It reads the LLM and tools from the ContextVars set by `set_background_context` (shared with background tasks), creates a fresh `TriggerEvent` with `source="subagent"`, and awaits a new `AgentLoop.run()` call directly — not as a separate asyncio task. The result string is returned to the calling agent's tool chain.

A `_depth_var` ContextVar tracks nesting depth. Each `delegate_task` call increments it (using `ContextVar.reset` in a `finally` block) and rejects calls when depth ≥ `_MAX_DEPTH` (2). Because the agent loop runs all tool calls via `asyncio.gather` (which wraps each in a task that inherits the current context), multiple `delegate_task` calls in a single step execute concurrently while each independently tracks its own depth.

### ToolManager

`app/tools/manager.py` holds the tool registry. Each entry stores the callable `fn`, an OpenAI-format JSON schema, and a `status_template` string. Key methods:

- `register(name, fn, description, parameters, status_template)` — add a tool
- `call(name, **kwargs)` — synchronous dispatch (used via `run_in_executor`)
- `async_call(name, **kwargs)` — async dispatch for coroutine tools
- `is_async(name)` — returns `True` if the registered function is a coroutine function
- `get_openai_schemas()` — returns the list of tool schemas for the LLM
- `get_status(name, args)` — formats the status message from the template

The shared `tool_manager` instance lives in `app/tools/builtin.py` and is imported by all tool modules.

### Workspace file tools

All file tools operate inside `WORKSPACE_DIR` (default: `data/workspace`). `_safe_path()` in `app/tools/files.py` resolves every user-supplied path and rejects anything that escapes the workspace root via `Path.relative_to()`. The workspace directory is created on first use.

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
| `OPENAI_MODEL` | `gemma4:e4b` | Primary model name |
| `FAST_MODEL` | `qwen2.5:1.5b` | Quick-ack model; set empty to disable |
| `LLM_TEXT_TOOL_CALLING` | `1` | Wrap LLM in TextToolCallingLLM for models that don't support native tool calls |
| `BRAVE_API_KEY` | — | Brave Search (required for web and image search) |
| `OPENWEATHERMAP_API_KEY` | — | OpenWeatherMap (required for weather) |
| `AGENT_LOG_FILE` | `data/agent_history.jsonl` | Structured event log path |
| `AGENT_TZ` | `UTC` | IANA timezone for current-time in prompts and naive reminder datetimes |
| `AGENT_PERSONA` | — | Optional persona string appended to the system prompt |
| `MEMORY_FILE` | `data/memory.json` | Persistent memory store path |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for semantic memory and workspace search |
| `WORKSPACE_DIR` | `data/workspace` | Sandboxed directory for file tools and code execution |
| `WORKSPACE_INDEX_FILE` | `data/workspace_index.json` | Vector index for `search_files_semantic` |
| `HISTORY_DIR` | `data/history` | Directory for per-session history files |
| `MAX_PERSISTED_HISTORY` | `200` | Maximum messages kept per session history file |
| `WATCHES_FILE` | `data/watches.json` | Persistent URL watch store |
| `REMINDERS_FILE` | `data/reminders.json` | Persistent reminder store path |
| `OLLAMA_NUM_CTX` | `8192` | Context window size passed to Ollama |
| `MAX_TOOL_RESULT_CHARS` | `3000` | Truncate individual tool results to this length |
| `MAX_MESSAGES_CHARS` | auto | Character budget for messages list |
| `MAX_HISTORY_CHARS` | `12000` | Trigger summarization at this total history char count |
| `SUMMARY_KEEP_CHARS` | `4000` | Target chars of recent history to keep verbatim after summarizing |
| `CODE_EXEC_TIMEOUT` | `30` | Default `run_python` timeout in seconds (hard cap: 120) |
| `CODE_MAX_OUTPUT_CHARS` | `3000` | Truncate `run_python` output to this length |
| `HTTP_HOST` | `0.0.0.0` | HTTP trigger bind host |
| `HTTP_PORT` | `8000` | HTTP trigger port |
| `HTTP_API_KEY` | — | Bearer token for HTTP auth; unset = open |
| `HTTP_PUBLIC_URL` | `http://localhost:{port}` | Base URL for `share_file` download links |
| `MATRIX_HOMESERVER` | — | Matrix homeserver URL |
| `MATRIX_USER` | — | Bot Matrix ID |
| `MATRIX_PASSWORD` | — | Bot account password |
| `MATRIX_ROOM_IDS` | — | Comma-separated room IDs |
| `MATRIX_STORE_PATH` | `./data/nio_store` | E2EE key storage path |
| `MATRIX_MAX_MSG_LEN` | `4000` | Split Matrix messages at this character count |
| `CALENDAR_<name>` | — | ICS URL for a named calendar (e.g. `CALENDAR_WORK=…`) |
| `DAILY_BRIEFING_TIME` | — | `HH:MM` to fire the daily briefing (unset = disabled) |
| `DAILY_BRIEFING_TZ` | `UTC` | Timezone for `DAILY_BRIEFING_TIME` (e.g. `Europe/Helsinki`) |
| `DAILY_BRIEFING_PROMPT` | see .env.example | Agent prompt for the daily briefing |
| `MEMORY_CONSOLIDATION_CRON` | — | 5-field cron for memory consolidation (e.g. `0 3 * * *`); unset = disabled |
| `MEMORY_CONSOLIDATION_INTERVAL_HOURS` | — | Alternative: run consolidation every N hours |
| `MEMORY_CONSOLIDATION_CORE_THRESHOLD` | `8` | Compact core memories when count reaches this number |
| `MEMORY_CONSOLIDATION_STATE_FILE` | `data/memory_consolidation_state.json` | Tracks last-run timestamp for incremental history scans |
| `MEMORY_CONSOLIDATION_MAX_HISTORY_CHARS` | `8000` | Max chars of conversation history embedded in each consolidation prompt |

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

For tools that use APScheduler (reminders, URL watches): call `load_and_schedule_pending()` from `main.py` after `_scheduler.start()` to restore persisted jobs on restart.

### Add a trigger

1. Subclass `BaseTrigger` from `app/triggers/base.py`.
1. Implement `async def start(self, server: CoreServer)` — load history with `history.load(session_id)`, produce `TriggerEvent` objects, pass them to `await server.handle_event(event)`, then save history with `history.save(session_id, event.history)`.
1. Implement `async def stop(self)`.
1. Register it in `main.py`: `server.register_trigger(MyTrigger(…))`.

Provide all four callbacks (`respond_fn`, `status_fn`, `react_fn`, `deliver_fn`) as appropriate for your channel.

## Conventions

- **SSRF guard on all outbound requests**: import `_check_url` from `app/tools/fetch.py` and call it before any HTTP request to a user-supplied URL. This blocks private, loopback, link-local, and reserved IP ranges.
- **Sandbox preamble for code execution**: use `app/tools/code.py`'s preamble pattern — override `builtins.open` inside a helper function so intermediate variables don't leak into the user's namespace; use `realpath` + prefix check (with trailing `os.sep`) to block path-confusion attacks.
- **No `eval()` in tools**: the calculator uses `ast` parsing with an explicit allowlist.
- **API keys never in error messages**: catch HTTP error codes explicitly; return plain string error messages.
- **All async**: triggers, agent loop, tool dispatch (sync tools run in `run_in_executor`).
- **No comments explaining what code does** — only for non-obvious constraints, invariants, or workarounds.
- **`.env` and `data/` are gitignored** — never commit them.
