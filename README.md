# Pino

Pino is a self-hosted AI agent with persistent memory, a pluggable tool system, and support for multiple simultaneous input channels. Input arrives through **triggers** (CLI, HTTP, Matrix), gets processed by an async agent loop backed by an OpenAI-compatible LLM, and results are routed back through the same trigger. A built-in scheduler enables proactive features — reminders and a daily briefing — that the agent can deliver unprompted.

## Architecture

```text
Trigger → TriggerEvent → CoreServer → AgentLoop → LLM (Ollama / OpenAI-compatible)
                                                 ↕
                                             ToolManager
                         (search, weather, fetch, calculate, memory,
                          react, files, calendar, reminders, background)

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
| `OPENAI_MODEL` | `gemma4:latest` | Primary model name |
| `FAST_MODEL` | `qwen2.5:1.5b` | Quick-acknowledgment model; set empty to disable |
| `BRAVE_API_KEY` | — | Brave Search API key (required for `search_web`) |
| `OPENWEATHERMAP_API_KEY` | — | OpenWeatherMap API key (required for `get_weather`) |
| `MEMORY_FILE` | `memory.json` | Path to persistent memory store |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for semantic memory embeddings |
| `MAX_HISTORY_TURNS` | `20` | Summarize conversation when history exceeds this many turns |
| `SUMMARY_KEEP_RECENT` | `6` | Keep this many recent turns verbatim after summarization |
| `WORKSPACE_DIR` | `<project_root>/workspace` | Sandboxed directory for file tools |
| `HTTP_HOST` | `0.0.0.0` | HTTP trigger bind host |
| `HTTP_PORT` | `8000` | HTTP trigger port |
| `HTTP_API_KEY` | — | Bearer token for HTTP auth; unset = open |
| `HTTP_PUBLIC_URL` | `http://localhost:{port}` | Base URL used in `share_file` download links |
| `MATRIX_HOMESERVER` | — | Matrix homeserver URL, e.g. `https://matrix.org` |
| `MATRIX_USER` | — | Bot Matrix ID, e.g. `@pino:matrix.org` |
| `MATRIX_PASSWORD` | — | Bot account password |
| `MATRIX_ROOM_IDS` | — | Comma-separated list of room IDs to join |
| `MATRIX_STORE_PATH` | `./nio_store` | Path for E2EE key storage |
| `MATRIX_MAX_MSG_LEN` | `4000` | Split Matrix messages at this character count |
| `CALENDAR_<name>` | — | ICS URL for a named calendar, e.g. `CALENDAR_WORK=https://…` |
| `REMINDERS_FILE` | `reminders.json` | Persistent reminder store path |
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

## Conversation summarization

When conversation history exceeds `MAX_HISTORY_TURNS` (default 20), the agent automatically summarizes older turns into a single system message using the primary LLM. The most recent `SUMMARY_KEEP_RECENT` (default 6) turns are kept verbatim so context is never lost mid-exchange.

## Background tasks

The agent can start long-running tasks that continue after the current response using the `start_background_task` tool. Results are delivered back to the user via the same `respond_fn` channel when the task completes. Tasks can optionally start after a delay.

## Reminders

The agent can schedule reminders using the `set_reminder` tool. When a reminder fires, the message is delivered back to the conversation where it was set. Reminders are persisted to `REMINDERS_FILE` and rescheduled automatically on restart so they survive process restarts.

```text
> Remind me in 2 hours to call the dentist.
  📋 I'll set that reminder!

[2 hours later]
⏰ Reminder: Call the dentist
```

Use `list_reminders` to see pending reminders and `cancel_reminder` to remove one by ID.

## Calendar

Configure one or more calendars using `CALENDAR_<name>=<ics_url>` environment variables:

```bash
CALENDAR_PERSONAL=https://calendar.google.com/calendar/ical/…
CALENDAR_WORK=https://calendar.google.com/calendar/ical/…
CALENDAR_WIFE=https://calendar.google.com/calendar/ical/…
```

Get ICS URLs from Google Calendar → Settings → (calendar) → *Secret address in iCal format*.

The `get_calendar_events` tool fetches upcoming events from all configured calendars (or a named subset), merges them chronologically, and labels each with its calendar name. Recurring events are fully expanded.

```text
> What's on my calendar this week?

Upcoming events (next 7 days):
- 2026-06-24 09:00 UTC: Team standup [work] @ Conference room B
- 2026-06-25 (all day): Midsummer [personal]
- 2026-06-26 14:00 UTC: Dentist [personal] @ Dental clinic
```

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

Pass `session_id` to maintain conversation history across requests:

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
| `get_weather(location, units?)` | Current conditions from OpenWeatherMap | `OPENWEATHERMAP_API_KEY` |
| `fetch_page(url)` | Fetch and extract readable text from a web page; blocks private addresses | — |
| `calculate(expression)` | Safe AST-based arithmetic evaluator | — |
| `get_datetime()` | Current local date, time, and timezone | — |
| `react(emoji)` | Attach an emoji reaction to the triggering message | — |
| `save_memory(key, value, category?, ttl_days?)` | Persist a fact to `memory.json`; optional TTL auto-expires it | — |
| `recall_memory(query?)` | Semantic + keyword search over stored memories | `EMBEDDING_MODEL` (optional) |
| `delete_memory(key)` | Remove a stored memory by key | — |
| `start_background_task(task, delay_seconds?)` | Run a task asynchronously; delivers result to user when done | — |
| `list_files(path?)` | List files and directories in the workspace | — |
| `find_files(pattern)` | Glob search in the workspace, e.g. `**/*.md` | — |
| `read_file(path)` | Read a text file from the workspace (100 KB limit) | — |
| `write_file(path, content)` | Write or overwrite a file in the workspace | — |
| `download_file(url, path)` | Download a file from a URL into the workspace (50 MB limit) | — |
| `share_file(path)` | Deliver a workspace file to the user via the appropriate channel | — |
| `get_calendar_events(days_ahead?, calendars?)` | Upcoming events from configured ICS calendars | `CALENDAR_*` |
| `set_reminder(when, message)` | Schedule a reminder for a specific ISO 8601 datetime | — |
| `list_reminders()` | Show all pending reminders with IDs and times | — |
| `cancel_reminder(reminder_id)` | Cancel a pending reminder by ID | — |

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

## Logs

All agent events are written to `agent_history.jsonl` in JSONL format: inputs, tool calls, tool responses, and final outputs. The file is gitignored.
