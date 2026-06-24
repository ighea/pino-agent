# Pino

Pino is a self-hosted AI agent with persistent memory, a pluggable tool system, and support for multiple simultaneous input channels. Input arrives through **triggers** (CLI, HTTP, Matrix), gets processed by an async agent loop backed by an OpenAI-compatible LLM, and results are routed back through the same trigger. A built-in scheduler enables proactive features — reminders, URL monitoring, recurring scheduled prompts, and a daily briefing — that the agent can deliver unprompted.

## Architecture

```text
Trigger → TriggerEvent → CoreServer → AgentLoop → LLM (Ollama / OpenAI-compatible)
                                                 ↕
                                             ToolManager
                         (search, weather, fetch, calculate, memory, react,
                          files, semantic search, code execution, calendar,
                          reminders, URL monitoring, background tasks,
                          scheduled tasks, sub-agents, task planning)

Scheduler → fire_proactive() → MatrixTrigger → room_send()
```

Each trigger produces a `TriggerEvent` with four async callbacks:

- `respond_fn(text)` — called with the final agent answer
- `status_fn(text)` — called during tool use to show progress
- `react_fn(emoji)` — called to attach an emoji reaction to the triggering message
- `deliver_fn(path)` — called to deliver a workspace file to the user

The agent loop and tools are completely decoupled from the trigger source.

## Setup

```bash
cp .env.example .env
# fill in API keys and configuration
pip install -r requirements.txt
```

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_BASE_URL` | `http://host.docker.internal:11434/v1` | LLM endpoint (Ollama or any OpenAI-compatible API) |
| `OPENAI_API_KEY` | `ollama` | API key (`ollama` for local Ollama) |
| `OPENAI_MODEL` | `gemma4:e4b` | Primary model name |
| `FAST_MODEL` | `qwen2.5:1.5b` | Quick-acknowledgment model; set empty to disable |
| `BRAVE_API_KEY` | — | Brave Search API key (required for `search_web` and `search_images`) |
| `OPENWEATHERMAP_API_KEY` | — | OpenWeatherMap API key (required for `get_weather`) |
| `AGENT_LOG_FILE` | `data/agent_history.jsonl` | Structured event log path |
| `MEMORY_FILE` | `data/memory.json` | Path to persistent memory store |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for semantic memory and workspace search embeddings |
| `WORKSPACE_DIR` | `data/workspace` | Sandboxed directory for file tools and code execution |
| `WORKSPACE_INDEX_FILE` | `data/workspace_index.json` | Vector index for `search_files_semantic` |
| `HISTORY_DIR` | `data/history` | Per-session conversation history files |
| `MAX_PERSISTED_HISTORY` | `200` | Maximum messages saved per session history file |
| `WATCHES_FILE` | `data/watches.json` | Persistent URL watch store |
| `SCHEDULED_TASKS_FILE` | `data/scheduled_tasks.json` | Persistent recurring scheduled task store |
| `OLLAMA_NUM_CTX` | `8192` | Context window tokens passed to Ollama per request (Ollama default 2048 is too small for tool use) |
| `MAX_TOOL_RESULT_CHARS` | `3000` | Truncate individual tool results to this length before adding to the message list |
| `MAX_MESSAGES_CHARS` | auto | Character budget for the messages list; defaults to `OLLAMA_NUM_CTX × 4 − 16000` |
| `MAX_HISTORY_CHARS` | `12000` | Trigger conversation summarization when total history character count exceeds this |
| `SUMMARY_KEEP_CHARS` | `4000` | Target chars of recent history to keep verbatim after summarizing |
| `CODE_EXEC_TIMEOUT` | `30` | Default `run_python` timeout in seconds (hard cap: 120) |
| `CODE_MAX_OUTPUT_CHARS` | `3000` | Truncate `run_python` output to this length |
| `HTTP_HOST` | `0.0.0.0` | HTTP trigger bind host |
| `HTTP_PORT` | `8000` | HTTP trigger port |
| `HTTP_API_KEY` | — | Bearer token for HTTP auth; unset = open |
| `HTTP_PUBLIC_URL` | `http://localhost:{port}` | Base URL used in `share_file` download links |
| `MATRIX_HOMESERVER` | — | Matrix homeserver URL, e.g. `https://matrix.org` |
| `MATRIX_USER` | — | Bot Matrix ID, e.g. `@pino:matrix.org` |
| `MATRIX_PASSWORD` | — | Bot account password |
| `MATRIX_ROOM_IDS` | — | Comma-separated list of room IDs to join |
| `MATRIX_STORE_PATH` | `./data/nio_store` | Path for E2EE key storage |
| `MATRIX_MAX_MSG_LEN` | `4000` | Split Matrix messages at this character count |
| `CALENDAR_<name>` | — | ICS URL for a named calendar, e.g. `CALENDAR_WORK=https://…` |
| `REMINDERS_FILE` | `data/reminders.json` | Persistent reminder store path |
| `DAILY_BRIEFING_TIME` | — | `HH:MM` to send the daily briefing (unset = disabled) |
| `DAILY_BRIEFING_TZ` | `UTC` | Timezone for `DAILY_BRIEFING_TIME`, e.g. `Europe/Helsinki` |
| `DAILY_BRIEFING_PROMPT` | see .env.example | Agent prompt used to generate the daily briefing |

## Running

```bash
# Interactive CLI
python main.py --mode cli

# One-shot CLI
python main.py --mode cli --message "What is 12 * 7?"

# HTTP server only
python main.py --mode http

# Matrix bot only
python main.py --mode matrix

# All triggers concurrently
python main.py --mode all
```

### CLI output

Status messages and reactions are printed inline during tool use:

```text
> What's the weather in Helsinki?
  🌤 I'll check the weather for you!
  → Fetching weather for: Helsinki

Helsinki, FI: Partly cloudy. Temp: 14°C (feels like 12°C). Humidity: 72%. Wind: 4.5 m/s.
```

The quick acknowledgment line is sent by the fast model before the primary model begins. Errors appear with `✗`.

## Quick acknowledgment (fast model)

When a user sends a message, a small fast model fires concurrently with the main agent loop and immediately sends a short acknowledgment — e.g. `🔍 Let me look into that!` — so the user knows the message was received. The acknowledgment is prefixed with a topic-appropriate emoji (🔍 search, 📁 files, 🌤 weather, ✍️ writing, etc.).

Quick acks are sent only for human-initiated messages. Automated sources (scheduler, background tasks) do not produce them.

Set `FAST_MODEL=` (empty) to disable. Pull the default model with:

```bash
ollama pull qwen2.5:1.5b
```

## Conversation history

Conversation history is persisted per session so it survives process restarts. Each Matrix room and each HTTP `session_id` gets its own file under `HISTORY_DIR` (default: `data/history/`). Up to `MAX_PERSISTED_HISTORY` messages (default 200) are kept per file; older messages are dropped when the file is written.

Concurrent messages to the same room are serialized via a per-room async lock so history is always consistent.

## Conversation summarization

When total history character count exceeds `MAX_HISTORY_CHARS` (default 12 000), the agent automatically summarizes older turns into a single system message using the primary LLM. The most recent `SUMMARY_KEEP_CHARS` (default 4 000) characters of history are kept verbatim, split at a user-message boundary so tool-call chains are never broken mid-sequence. Tool calls and brief tool results are included in the summary prompt so no context is lost.

## Code execution

The agent can run Python code using the `run_python` tool. Code executes in a subprocess with:

- **Workspace-only file I/O** — `open()` is overridden to reject paths outside `WORKSPACE_DIR`; use relative paths or workspace-relative paths freely
- **Subprocess and shell execution blocked** — `subprocess`, `os.system`, `os.popen`, `os.execv`, and related APIs raise `PermissionError`
- **CPU time hard limit** — enforced at the OS level via `RLIMIT_CPU` on Linux, in addition to the `timeout` parameter
- Standard library and all installed packages are available

```text
> Calculate the 10 largest Fibonacci numbers under 1000 and save them to fib.txt

Running Python code...

Saved 10 Fibonacci numbers to fib.txt
```

Set `CODE_EXEC_TIMEOUT` (default 30 s, max 120 s) and `CODE_MAX_OUTPUT_CHARS` (default 3000) to tune behaviour.

## Background tasks

The agent can start long-running tasks that continue after the current response using the `start_background_task` tool. Results are delivered back to the user when the task completes — via the originating channel if still connected, or as a proactive notification otherwise. Tasks can optionally start after a delay.

## Recurring scheduled tasks

Users can ask the agent to set up recurring prompts that fire on a schedule and deliver results proactively, without any future user interaction:

```text
> Every weekday at 8am, check the weather in Helsinki and summarise my calendar for the day.

Scheduled task created (cron '0 8 * * 1-5'): 'Every weekday at 8am, check the weather in…' (id: a1b2c3d4)

[Monday 08:00]
📅 Morning briefing
Helsinki: Partly cloudy, 12°C. Today's calendar: standup at 09:00, team lunch at 12:30.
```

- Specify `interval_minutes` (e.g. 60 for hourly) or a 5-field cron expression (e.g. `0 8 * * 1-5` for weekdays at 08:00)
- Tasks are persisted to `SCHEDULED_TASKS_FILE` and restored on restart
- Use `list_scheduled_tasks` to see active tasks and `cancel_scheduled_task` to remove one

## Task planning

For multi-step requests the agent can optionally build an explicit task plan using `plan_steps`, then tick off each step with `finish_step` as it goes. The final `finish_step` call tells the agent it has everything it needs and prompts it to synthesise a final answer.

```text
> Research current mortgage rates in Finland, compare them to the EU average, and write a short summary.

Planning task...
  Step 0. [○] Search for current Finnish mortgage rates
  Step 1. [○] Search for current EU average mortgage rates
  Step 2. [○] Write a comparison summary

[agent calls search tools and finishes each step in order]

  Step 0. [✓] Finnish average ~3.4 % (April 2026, Nordea data)
  Step 1. [✓] EU average ~3.8 % (ECB data)
  Step 2. [○] Write a comparison summary

Finnish mortgage rates are currently below the EU average...
```

The agent decides whether to use `plan_steps` — it is skipped for simple requests. Task state is ephemeral and cleared at the start of each turn.

## Sub-agents

The `delegate_task` tool lets the agent hand off a complex sub-task to a fresh agent instance and receive the result inline, within its own reasoning loop. This is useful for:

- **Decomposing long work** — each sub-task runs in a fresh loop with its own step budget, so the parent never hits the `max_steps` limit trying to do everything itself
- **Parallel independent work** — calling `delegate_task` multiple times in a single step runs all sub-agents concurrently (the loop executes all tool calls with `asyncio.gather`)

```text
> Research the pros and cons of SQLite vs PostgreSQL for a small web app, then write a recommendation.

[agent delegates "research SQLite strengths/weaknesses" and "research PostgreSQL strengths/weaknesses" in parallel,
then synthesises the two results into a final recommendation]
```

Sub-agent nesting is capped at 2 levels to prevent runaway recursion. For fire-and-forget work, use `start_background_task` instead.

## Reminders

The agent can schedule reminders using the `set_reminder` tool. When a reminder fires, the message is delivered back to the conversation where it was set. Reminders are persisted to `REMINDERS_FILE` and rescheduled automatically on restart so they survive process restarts.

```text
> Remind me in 2 hours to call the dentist.
  📋 I'll set that reminder!

[2 hours later]
⏰ Reminder: Call the dentist
```

Use `list_reminders` to see pending reminders and `cancel_reminder` to remove one by ID.

## URL monitoring

The agent can watch URLs for content changes using `watch_url`. On each check it fetches the URL, hashes the extracted text, and compares it with the stored baseline. When a change is detected a proactive notification is sent with a content excerpt.

```text
> Watch the Hacker News front page every 30 minutes.

Watching 'Hacker News front page' every 30 minutes (id: a3f9b2c1).

[30 minutes later, if the page changes]
🔔 Hacker News front page has changed
https://news.ycombinator.com

Content excerpt:
Ask HN: What tools do you use for self-hosting?...
```

- Minimum interval is 5 minutes; maximum is 1 week
- Watches are persisted to `WATCHES_FILE` and rescheduled on startup
- Use `list_watches` to see active watches and `unwatch_url` to stop one

## Calendar

Configure one or more calendars using `CALENDAR_<name>=<ics_url>` environment variables:

```bash
CALENDAR_PERSONAL=https://calendar.google.com/calendar/ical/…
CALENDAR_WORK=https://calendar.google.com/calendar/ical/…
```

Get ICS URLs from Google Calendar → Settings → (calendar) → *Secret address in iCal format*.

The `get_calendar_events` tool fetches upcoming events from all configured calendars (or a named subset), merges them chronologically, and labels each with its calendar name. Recurring events are fully expanded.

## Daily briefing

Set `DAILY_BRIEFING_TIME` to have Pino send a morning briefing to all configured Matrix rooms at a fixed time each day:

```bash
DAILY_BRIEFING_TIME=08:00
DAILY_BRIEFING_TZ=Europe/Helsinki
```

The briefing runs the full agent loop — it can check the calendar, weather, or anything else — and sends the result proactively without any user message. The prompt is configurable via `DAILY_BRIEFING_PROMPT`.

## HTTP API

### POST `/api/v1/run`

```bash
curl -X POST http://localhost:8000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the capital of Finland?"}'
```

The endpoint streams [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events):

| Event type | Description |
| --- | --- |
| `status` | Progress message during tool use |
| `reaction` | Emoji reaction the agent chose to express |
| `output` | Agent response (may be sent multiple times: quick ack + final) |
| `file` | File ready for download; includes `path` and `url` fields |
| `error` | Error message if the agent loop fails |

```json
data: {"type": "output", "text": "📁 On it!"}
data: {"type": "status", "text": "Thinking..."}
data: {"type": "file", "path": "report.pdf", "url": "http://localhost:8000/files/report.pdf"}
data: {"type": "output", "text": "Done — your report is ready to download."}
```

Pass `session_id` to maintain conversation history across requests. History is persisted to disk so it survives server restarts:

```bash
curl -X POST http://localhost:8000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"message": "What about Tampere?", "session_id": "abc123"}'
```

### GET `/files/{path}`

Download a file from the agent workspace. Protected by `HTTP_API_KEY` if set.

### GET `/health`

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

### Authentication

Set `HTTP_API_KEY` to require a bearer token on all requests:

```bash
curl -X POST http://localhost:8000/api/v1/run \
  -H "Authorization: Bearer your_key" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello"}'
```

## Matrix trigger

The Matrix trigger lets people talk to the agent in Matrix rooms, with full E2EE support.

### Matrix setup

1. Create a Matrix account for the bot.
1. Invite it to a room.
1. Set env vars and run with `--mode matrix` or `--mode all`.

### Triggering the agent

**In a group room** — mention the bot by name:

```text
@Pino what's the weather in Helsinki?
```

**In a DM** — every message is sent to the agent automatically.

### Image and file input

Send an image or file to the agent directly in a Matrix room. In group rooms, mention the bot when sending.

- **Images** are passed to the LLM as multimodal input (base64-encoded). E2EE images are decrypted automatically.
- **Text files** (`.txt`, `.md`, `.csv`, `.json`, etc.) have their content extracted and included in the prompt.
- **PDFs** are parsed with `pypdf` and the text is passed to the agent.
- **Unsupported types** get an acknowledgment with the filename and size.

### Reaction lifecycle

| Reaction | Meaning |
| --- | --- |
| 👀 | Message received, agent is processing |
| ✅ | Agent finished and sent a response |
| ❌ | Agent encountered an error |

### File delivery

When the agent calls `share_file`, the file is uploaded to the Matrix homeserver media API and sent as a native attachment (`m.file`, `m.image`, `m.video`, or `m.audio`). It renders as a downloadable file in every Matrix client.

### Multi-user conversation

All users in a room share the same conversation history. The agent sees messages tagged with the sender's display name (`[Mika]: ...`). A per-room lock queues simultaneous messages to keep history consistent. Responses are split at `MATRIX_MAX_MSG_LEN` characters on paragraph boundaries and rendered as HTML for bold, links, lists, and code.

## Built-in tools

| Tool | What it does | Requires |
| --- | --- | --- |
| `search_web(query)` | Brave Search — returns titles, URLs, descriptions | `BRAVE_API_KEY` |
| `search_images(query)` | Brave image search — returns image URLs and thumbnails | `BRAVE_API_KEY` |
| `get_weather(location, units?)` | Current conditions from OpenWeatherMap | `OPENWEATHERMAP_API_KEY` |
| `fetch_page(url)` | Fetch and extract readable text from a web page; blocks private addresses | — |
| `calculate(expression)` | Safe AST-based arithmetic evaluator | — |
| `get_datetime()` | Current local date, time, and timezone | — |
| `react(emoji)` | Attach an emoji reaction to the triggering message | — |
| `save_memory(key, value, category?, ttl_days?)` | Persist a fact to `memory.json`; optional TTL auto-expires it | — |
| `recall_memory(query?)` | Semantic + keyword search over stored memories | `EMBEDDING_MODEL` (optional) |
| `delete_memory(key)` | Remove a stored memory by key | — |
| `run_python(code, timeout?)` | Execute Python in a sandboxed subprocess; workspace-only file I/O, subprocess blocked | — |
| `start_background_task(task, delay_seconds?)` | Run a task asynchronously; delivers result to user when done (proactive fallback if original channel is gone) | — |
| `delegate_task(prompt)` | Delegate a sub-task to a fresh agent instance; returns result inline for chaining | — |
| `plan_steps(steps)` | Create an ordered task checklist for the current request; agent works through it with `finish_step` | — |
| `finish_step(index, notes?)` | Mark a step done with an outcome note; returns updated checklist and next action | — |
| `create_scheduled_task(prompt, label?, interval_minutes?, cron_expr?)` | Set up a recurring prompt that runs on a schedule and delivers results proactively | — |
| `list_scheduled_tasks()` | Show all recurring scheduled tasks with IDs, schedules, and last run info | — |
| `cancel_scheduled_task(task_id)` | Cancel a recurring scheduled task by ID | — |
| `list_files(path?)` | List files and directories in the workspace | — |
| `find_files(pattern)` | Glob search in the workspace, e.g. `**/*.md` | — |
| `read_file(path, start_line?, end_line?)` | Read a text file from the workspace (100 KB limit; range supported) | — |
| `write_file(path, content)` | Write or overwrite a file in the workspace | — |
| `append_file(path, content)` | Append text to a workspace file without overwriting | — |
| `patch_file(path, start_line, end_line, content)` | Replace a line range in a workspace file | — |
| `download_file(url, path)` | Download a file from a URL into the workspace (50 MB limit) | — |
| `search_files(query, path?, case_sensitive?)` | Keyword search across workspace files with line snippets | — |
| `search_files_semantic(query, path?)` | Semantic search across workspace files using vector similarity | `EMBEDDING_MODEL` |
| `share_file(path)` | Deliver a workspace file to the user via the appropriate channel | — |
| `get_calendar_events(days_ahead?, calendars?)` | Upcoming events from configured ICS calendars | `CALENDAR_*` |
| `set_reminder(when, message)` | Schedule a reminder for a specific ISO 8601 datetime | — |
| `list_reminders()` | Show all pending reminders with IDs and times | — |
| `cancel_reminder(reminder_id)` | Cancel a pending reminder by ID | — |
| `watch_url(url, interval_minutes?, label?)` | Monitor a URL for content changes; notify when it changes | — |
| `unwatch_url(watch_id)` | Stop monitoring a URL by watch ID | — |
| `list_watches()` | Show all active URL watches with intervals and last-check times | — |

### Memory categories

| Category | Behaviour |
| --- | --- |
| `core` | Always injected into the system prompt — use for facts that should never be forgotten (name, language, home city) |
| `location` | Geographic information |
| `appointment` | Time-sensitive; pair with `ttl_days` to auto-expire |
| `personal` | Personal details |
| `preference` | User preferences |
| `general` | Anything else |

### Semantic memory

`recall_memory` computes cosine similarity between the query embedding and stored memory embeddings (using `nomic-embed-text` via Ollama). Entries with similarity ≥ 0.35 are returned. If embeddings are unavailable, it falls back to keyword matching.

### Semantic workspace search

`search_files_semantic` builds a vector index of workspace files on demand (stored in `WORKSPACE_INDEX_FILE`). Files are split into 400-character overlapping chunks, each embedded and cached with the file's modification time. Stale chunks are re-embedded the next time the tool is called. Use this for concept and topic queries; use `search_files` for exact keyword matches.

### Web page fetching safety

`fetch_page` and `download_file` apply SSRF protection:

- Resolves hostnames and rejects private, loopback, link-local, and reserved IP ranges (including after redirects)
- `fetch_page` only processes `text/*` responses
- `fetch_page` reads at most 50 KB of HTML, truncates extracted text to 8 000 characters; `download_file` stops at 50 MB
- `trafilatura` strips navigation, ads, and boilerplate; falls back to stdlib tag-stripping
- Fetched content is labelled `[UNTRUSTED EXTERNAL CONTENT]` and the system prompt instructs the agent never to follow instructions found in fetched pages

### Workspace file tools

All file operations are sandboxed to `WORKSPACE_DIR`. Path traversal attempts (`../`) are rejected. `share_file` delivers files differently per trigger:

| Trigger | Delivery |
| --- | --- |
| HTTP | Returns a `GET /files/{path}` download URL |
| Matrix | Uploads to the homeserver media API, sends a native file attachment |
| CLI | Prints the absolute workspace path |

## Extending

### Add a tool

Register on the shared `tool_manager` in a new module:

```python
from app.tools.builtin import tool_manager

tool_manager.register(
    name="my_tool",
    fn=my_function,           # sync or async — both work
    description="What this tool does.",
    parameters={
        "type": "object",
        "properties": {
            "arg": {"type": "string", "description": "..."},
        },
        "required": ["arg"],
    },
    status_template="Running my_tool with: {arg}",
)
```

Import the module as a side effect in `main.py`. Async functions are awaited directly; sync functions run in `run_in_executor`. For tools that need per-request state (e.g. a callback from `TriggerEvent`), use `contextvars.ContextVar` and set it in `AgentLoop.run()`.

### Add a trigger

Subclass `BaseTrigger` from `app/triggers/base.py`, implement `start(server)` and `stop()`, and call `server.register_trigger(MyTrigger(...))` in `main.py`. Provide `respond_fn`, `status_fn`, `react_fn`, and `deliver_fn` callbacks as appropriate for the channel.

## Docker

```bash
docker compose up --build
```

The compose file passes `.env` variables through to the container. The default mode is `all` (CLI + HTTP + Matrix).

## Data directory

All generated runtime files are kept under `data/` and gitignored as a single entry:

| Path | Contents |
| --- | --- |
| `data/agent_history.jsonl` | Structured JSONL event log (inputs, tool calls, outputs, errors) |
| `data/memory.json` | Persistent agent memory store |
| `data/reminders.json` | Pending reminders (rescheduled on restart) |
| `data/watches.json` | Active URL watches (rescheduled on restart) |
| `data/scheduled_tasks.json` | Recurring scheduled tasks (rescheduled on restart) |
| `data/history/` | Per-session conversation history (one JSON file per Matrix room / HTTP session) |
| `data/workspace_index.json` | Vector index for semantic workspace search |
| `data/nio_store/` | Matrix E2EE session keys |
| `data/workspace/` | Sandboxed workspace for file tools and code execution |

Override individual paths via their respective env vars (`AGENT_LOG_FILE`, `MEMORY_FILE`, `REMINDERS_FILE`, `WATCHES_FILE`, `HISTORY_DIR`, `WORKSPACE_INDEX_FILE`, `MATRIX_STORE_PATH`, `WORKSPACE_DIR`).
