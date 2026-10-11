# Activity Monitor

A self-hosted monitor for where your attention goes during a workday. Events from many
sources — shell history, git commits, an editor/agent plugin, desktop activity — are normalized
into a single timeline and explored through two views.

- **Timeline view** — pick a date range and see where attention went in one unified timeline. The
  first row is **User** (all desktop `focus` spans flattened, no per-process rows); each running
  program gets a row named `program@host` (suffixed `#N` when several share the same program+host).
  Every row uses the same theme — **active** (filled), **idle** (outlined), **closed** (plain
  line): for the user, idle comes from input-idle events and closed from lock events; for programs,
  states follow prompt events (`prompt.begin` → active, `prompt.end` → idle, ended → closed) with
  prompt dots overlaid. Programs running on the same host at the same time collapse onto separate
  lanes; a re-started program reuses the lowest free lane. Long stretches (over an hour) with
  nothing active are compressed into axis breaks, keeping 15 minutes of context on each side.
- **Task view** — search and filter events, browse the task list, and inspect a task's timeline.

One server hosts everything: a FastAPI backend that also serves its web UI.

## Sources

Sources are pluggable and deliberately agnostic about how data is obtained. Every source has a
`kind` and is either **pull** (the server fetches on demand) or **push** (an external client posts
events).

| Kind | Shape | What it reads |
| --- | --- | --- |
| `BashHistory` | pull | A shell history file (understands `#<epoch>` timestamp lines) |
| `GitHistory` | pull | `git log` of the configured repositories |
| `Push` | push | Anything that POSTs to the ingest endpoint (e.g. an opencode plugin) |

Add, remove, or disable sources under `sources:` in `config.yaml`. New kinds are added by
subclassing `SourceProvider` in `activity_monitor/sources.py` and registering it in `PROVIDERS`.

Desktop activity is collected by a companion push watcher (`activity_monitor/windows_watcher.py`):
run standalone it POSTs what you are looking at to the `windows` Push source, or embed it in the
backend (`--watch-windows`) to write straight to the database (see below).

### Ingest (push)

```bash
curl -X POST http://localhost:8001/api/ingest/opencode \
  -H "Content-Type: application/json" \
  -d '{"events": [
    {"id": "abc", "kind": "tool_call", "actor": "agent-a",
     "task": "feature-x", "timestamp": "2026-10-10T09:00:00",
     "title": "edited main.py", "tags": ["edit"]}
  ]}'
```

Fields: `timestamp` (ISO-8601 or epoch), `kind`, `actor`, `task`, `title`, `detail`,
`duration_ms`, `tags`, and any extra keys (kept under `data`). An `id` makes ingestion
idempotent; without one, a stable hash of the payload is used. Unknown sources return `404`,
pull sources cannot be pushed to.

### Fetch (pull)

`POST /api/sources/{name}/fetch` asks a pull source for data and stores what is new.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/sources` | Configured sources |
| `POST /api/sources/{name}/fetch` | Pull and store from a pull source |
| `POST /api/ingest/{name}` | Push one or many raw events |
| `GET /api/timelines/agentic` | Events grouped into per-actor lanes (`start`, `end`, `actor`) |
| `GET /api/timelines/presence` | Program lanes with `active`/`idle`/`closed` state segments + events |
| `GET /api/timelines/user` | Desktop activity: focus lanes per process + idle/locked intervals |
| `POST /api/programs/start` | Register a running program (`program_id`, `source`, `host`) |
| `POST /api/programs/beat` | Heartbeat, keeps the program alive |
| `POST /api/programs/end` | Close a program |
| `GET /api/tasks` | Filtered events (`q`, `source`, `actor`, `kind`, `task`, `start`, `end`, `limit`) |
| `GET /api/tasks/summary` | Task list with counts and time span |
| `GET /api/actors`, `GET /api/kinds` | Filter options |
| `GET /api/stats` | Totals |

Lane numbers (`parallel_id`) are assigned at query time: per `(source, host)`, programs are laid
out greedily so that overlapping runs never share a lane and a re-started program reuses the
lowest free lane. A program whose heartbeat went silent for `general.stale_after_seconds`
(default 300) is treated as closed. All timestamps are stored and compared in UTC, so clients
should send ISO-8601 timestamps with a timezone (e.g. `…Z`).

## Architecture

Deliberately small: no services framework, no thread pool, no pipeline engine (the only background
thread is the optional embedded desktop watcher).

```
activity_monitor/
  main.py      FastAPI app, CLI, lifespan (init DB, build sources), serves frontend/dist
  config.py    yaml -> AppConfig (plain dataclass)
  db.py        async SQLite engine + session factory
  models.py    ActivityEvent (SQLModel) + API response models
  sources.py   SourceProvider base + BashHistory/GitHistory/Push providers + registry
  api.py       all endpoints
  windows_watcher.py   desktop focus/idle/lock watcher (ctypes): standalone HTTP or embedded DB sink
plugins/opencode-activity.ts   opencode plugin reporting program lifecycle + prompts
plugins/post-commit            git hook registering branch + commit id to /api/ingest/git
plugins/pre-push               git hook registering pushes (remote + branch + commit id)
supervisor.py   restarts the backend on exit
```

The UI is a Vite + React 19 + MUI app (`frontend/`) whose API types are generated from the
backend's OpenAPI schema with `openapi-typescript`.

## Install & run

```bash
python -m activity_monitor.main                        # http://localhost:8001
python -m activity_monitor.main --config config.yaml --database activity.db --port 8001
python -m activity_monitor.main --watch-windows        # backend + embedded desktop watcher
python supervisor.py                                   # restarts the backend on exit
python -m activity_monitor.windows_watcher             # standalone: pushes desktop focus/idle/lock
```

### From source

```bash
cd frontend && npm install && npm run build   # build the web UI once
python supervisor.py                          # from the repo root
```

### opencode plugin

`plugins/opencode-activity.ts` is an opencode plugin that reports the opencode process itself as a
**program** and records activity against it:

- On load it announces the process via `POST /api/programs/start` with `program_id` = the OS PID
  (or an explicit `programId` option) and `host` = machine hostname. The backend tracks it as a
  running program and lays it out on a presence lane.
- It sends a heartbeat (`POST /api/programs/beat`) every `heartbeatMs` (60s) and closes the
  program (`POST /api/programs/end`) when opencode exits (the `dispose` hook).
- Each prompt is recorded as a `prompt.begin` event (the prompt is stored in `detail`) and a
  `prompt.end` event once the turn finishes (with `duration_ms` and a `completed`/`error` status).
  Both carry a top-level `program: { id, source, host }`, which is how the presence timeline
  pins events to a lane.

Event ids are deterministic SHA-1 hashes of `prompt · begin|end · sessionID · messageID`, so the
backend dedupes re-sends — the same prompt is never recorded twice.

It is enabled via the repo's `opencode.json` and posts to `POST /api/ingest/opencode`
(the `opencode` push source in `config.yaml`). Configure with plugin options or env vars:

| Option / env | Default |
| --- | --- |
| `baseUrl` / `ACTIVITY_MONITOR_BASE_URL` | `http://localhost:$ACTIVITY_MONITOR_PORT` |
| `ACTIVITY_MONITOR_PORT` | `8001` (builds the default base URL) |
| `source` / `ACTIVITY_MONITOR_SOURCE` | `opencode` |
| `actor` / `ACTIVITY_MONITOR_ACTOR` | `$USER`/`$USERNAME` |
| `truncate` | `120` |
| `programId` | the current process PID |
| `heartbeatMs` | `60000` |

Restart opencode after changing the plugin.

### Windows watcher

`activity_monitor/windows_watcher.py` is a small, dependency-free (`ctypes` only) foreground
watcher: every `--poll` seconds it samples the foreground window (`GetForegroundWindow` + process
image), the input-idle time (`GetLastInputInfo`) and the input desktop (`OpenInputDesktop`), and
reports transition events to the `windows` Push source in `config.yaml`:

| Event | When |
| --- | --- |
| `focus` | Foreground window (title + process) changes; the previous window's span is closed with its `duration_ms` |
| `idle.start` | No keyboard/mouse input for `--idle-minutes`; timestamp backdated to the true last-input moment |
| `idle.end` | Input resumes; `duration_ms` is the idle span |
| `lock` / `unlock` | Workstation locked/unlocked (`unlock` carries the lock span's `duration_ms`) |

It runs in two modes:

- **Standalone** — its own process, POSTing each event to the backend ingest endpoint. It targets
  `http://127.0.0.1:$ACTIVITY_MONITOR_PORT` (default `8001`), so one env var configures it:
  ```bash
  python -m activity_monitor.windows_watcher                                  # base from ACTIVITY_MONITOR_PORT, poll 1s, idle after 2m
  python -m activity_monitor.windows_watcher --base-url http://localhost:8001 --poll 1.0 --idle-minutes 2.0
  ```
- **Embedded** — started by the backend (`python -m activity_monitor.main --watch-windows`, or
  `general.watch_windows: true` in `config.yaml`), it runs in-process and writes events straight to
  the database, so no ingest request is needed. Per-source options (`poll`, `idle_minutes`,
  `actor`, `verbose`) can be set under `sources.windows`.

Each event carries a unique `id`, so restarts and retries dedupe on the server. Lock events cannot
be exercised from a test run (it watches your real desktop), but focus/idle transitions are logged
to stdout with `--verbose`. Restart the watcher after editing it.

### Git hooks

Two hooks report git activity to `POST /api/ingest/git`, each reading `ACTIVITY_MONITOR_PORT` and
never blocking the operation:

- `plugins/post-commit` registers each **commit** — at least the **current branch** and **commit
  id** (plus repository, author, timestamp and subject).
- `plugins/pre-push` registers each **push** as a distinct `push` event — remote, branch, commit id
  and the previous (remote) sha.

Install them with `git config core.hooksPath <repo>/plugins` (or copy/symlink into `.git/hooks`).
See `plugins/README.md` for all client setup.

## Development

```bash
ruff check . && ruff format .                 # backend
python -m pytest                              # tests
cd frontend && npm run lint && npm run build  # frontend

# regenerate API types after changing endpoints
python -m activity_monitor.main --openapi openapi_local.json
cd frontend && npx openapi-typescript ../openapi_local.json -o src/api/generated/schema.ts
```
