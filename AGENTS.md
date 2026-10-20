# AGENTS.md

Activity Monitor — timeline of where attention goes across sources: Python/FastAPI backend +
React/TS/Vite frontend, single server serving the built frontend.

## Quick Reference

| Task | Command |
|------|---------|
| Run backend | `python -m activity_monitor.main` |
| Run backend (supervised) | `python supervisor.py` |
| Run backend + embedded desktop watcher | `python -m activity_monitor.main --watch-windows` |
| Run Windows watcher (standalone) | `python -m activity_monitor.services.windows_watcher` |
| Build frontend | `cd frontend && npm run build` |
| Lint backend | `python -m ruff check .` |
| Format backend | `python -m ruff format .` |
| Lint frontend | `cd frontend && npm run lint` |
| Run tests | `python -m pytest` |
| Regenerate API types | see "Schema generation" |
| Kill backend | `netstat -ano \| findstr :8001` then `taskkill -PID <pid> -F` |

## Layout

- `activity_monitor/` — FastAPI app
  - `main.py` — CLI, `create_app`, lifespan, serves `frontend/dist` from CWD if present
  - `log.py` — `get_logger` + the uvicorn timestamp formatters
  - `api/` — one router module per resource, each with `router = APIRouter(prefix="/api")`,
    included one by one by `create_app` (auth dependency applied at include time):
    `sources.py` (list/fetch/ingest), `programs.py` (start/beat/end/consolidate),
    `timelines.py` (presence/user/code/agentic), `events.py` (`/tasks`, `/tasks/summary`,
    `/actors`, `/kinds`, `/stats`), plus `deps.py` (`SessionDep`, `require_auth`,
    `stale_after`)
  - `services/` — `config.py` (`AppConfig.load()`; plain yaml -> dataclass; no config
    service/tree), `db.py` (async SQLite engine + `async_sessionmaker` + `get_session`
    dependency), `sources.py` (`SourceProvider` base, `BashHistory`/`GitHistory`/`Push`
    providers, `PROVIDERS`, `build_sources`), `events.py` (`store_events` single write
    path + focus open/close helpers), `programs.py` (`expire_stale`,
    `find_active_program`), `timelines.py` (lane/segment/span computation),
    `windows_watcher.py` (desktop watcher — see its bullet below)
  - `model/` — SQLModel tables + Pydantic DTOs, one module per entity: `event.py`
    (`ActivityEvent`, `EventOut`, `SourceInfo`, `StatsOut`, `TaskSummary`), `program.py`
    (`Program`, program/presence DTOs + `ConsolidateOut`), `timeline.py` (user/code/
    agentic lane DTOs)
  - `util/` — `time.py` (`as_utc`, `parse_timestamp`), `process_config.py`, `tray.py`
- `frontend/` — Vite + React 19 + TS + MUI app
- `plugins/opencode-v2/` — opencode 2 plugin (entry `index.ts`, `@opencode/plugin`) reporting program
  lifecycle + prompts: on load it POSTs `/api/programs/start` with `program_id` = the OS PID
  (+ `host`), heartbeats `/api/programs/beat` every ~60s, and `/api/programs/end` on unload (the
  `setup` cleanup function). Prompts are posted as `prompt.begin`/`prompt.end` to
  `/api/ingest/event` (source `opencode`) with a top-level `program: { id, source, host }` so the presence timeline
  can pin them to a lane (enabled via `opencode.json`; deterministic SHA-1 ids so re-sends dedupe).
  `prompt.begin` is recorded in the `ctx.session.hook("prompt", ...)` hook; `prompt.end` comes from
  the turn-completion event on `ctx.event.subscribe(...)` — `session.execution.succeeded` /
  `.failed` / `.interrupted` (V2 emits no `session.idle`/`session.status` idle for it; those are
  only kept as fallbacks). The
  `/task <id>` command is registered by the plugin via `ctx.command.transform` — its executor never
  submits a prompt, so no model turn happens; it records a `task.set` event and pins subsequent
  prompts of that session to the id (a `sessionTasks` map; `/task` alone clears it). Before any
  `/task` an event carries the default task `Unknown` (the old project-name default was dropped so
  the backend can backfill); on ingest the backend retags the program's `Unknown`/untagged events up
  to the previous
  `task.set` (see Ingest). `opencode-v1/`
  holds the legacy V1 implementation (`@opencode-ai/plugin`, `command.execute.before` + throw to
  abort) for OpenCode 1 — V1 plugins do not run on V2. Its sidebar indicator is a separate,
  target-exclusive TUI module (`tui.tsx`, loaded from `tui.json`) that subscribes to namespaced
  `tui.command.execute` events the server half publishes — the connection state as
  `activity-monitor:<pending|ok|error|shadow>`, a session's task as
  `activity-monitor:task:<sessionID>:<task>` — re-publishing the state (and every pinned task along
  with it) every few seconds on a short sync timer (decoupled from the 60s heartbeat, which only
  mirrors it) so a TUI that subscribes after the initial publish — or restarts against a long-lived
  server — converges within seconds; rejected publishes log once each as
  `[activity-monitor] tui publish ...` instead of vanishing (the SDK resolves errors rather than
  throwing), since V1 has no server↔TUI RPC bridge.
  `index.ts` also tracks the connection state
  (`pending`/`ok`/`error`, or `shadow` while silent) and each session's `/task` id, publishing both
  over the plugin RPC contract in `rpc.ts` (`status`/`task` methods + `state`/`task`/`toast`
  events);
  `tui.tsx` (the package's `./tui` entry, loaded by the CLI automatically) subscribes to them and
  renders a single coloured `● Activity Monitor (<task>) <state>` row in the TUI sidebar
  (`sidebar.content`, below the built-in MCP and Context sections) — green = last request succeeded,
  red = failed, gray = shadow/pending, with the `(<task>)` part omitted until the session pins one
  with `/task`. The TUI seeds the state once and each session's task on first render
  (`rpc.status({}, { location })` / `rpc.task({ sessionID }, { location })`): the RPC domain is
  registered per location, so the calls must name the TUI's location (else `rpc.unavailable`) and
  carry the method's required input. This is an OpenCode-only affordance, never sent to the Activity
  Monitor. Typecheck
  with `npm run typecheck` in the plugin folder. Restart opencode after editing the plugin.
- `plugins/git/` — `post-commit` + `pre-push` git hooks POSTing to `/api/ingest/event` (source `git`); commit
  registers branch + commit id (+ repo/author/subject) plus the commit's `git show --stat` diff
  summary (`parse_stat` keeps the block, the file list and the counts in
  `data.stat`/`data.files`/`data.insertions`/`data.deletions`, and appends a compact
  `N files, +x/-y` to the detail text), push registers remote + branch + commit + prior sha as a
  distinct `push` event (`GitHistorySource.parse` branches on `kind`). Install with
  `git config core.hooksPath <repo>/plugins/git`; `plugins/README.md` documents client setup. Every
  client (plugin, standalone watcher, hooks) reads `ACTIVITY_MONITOR_URL` (plus the optional
  `ACTIVITY_MONITOR_CA_CERT`/`_CLIENT_CERT`/`_CLIENT_KEY` mTLS,
  `ACTIVITY_MONITOR_USERNAME`/`_PASSWORD` basic-auth variables, `ACTIVITY_MONITOR_ORIGIN` to
  override the origin `host`, and `ACTIVITY_MONITOR_DISPLAY_USER` to override the event `actor`)
  and legacy
  `ACTIVITY_MONITOR_PORT` (default 8001); the
  backend also uses it as the port default when `--port` is not passed.
- `pyproject.toml` — metadata, deps, ruff config, hatchling build, scripts `activity-monitor` +
  `activity-monitor-windows`
- `supervisor.py` — restarts `python -m activity_monitor.main`
- `activity_monitor/services/windows_watcher.py` — push watcher for desktop activity: polls the foreground
  window (`GetForegroundWindow`), input-idle time (`GetLastInputInfo`) and lock state
  (`OpenInputDesktop`), emitting `focus` / `idle.start` / `idle.end` / `lock` / `unlock` transitions
  (the `windows` Push source). Stdlib `ctypes` only; each event carries a unique `id` so re-sends
  dedupe, plus a `host` (`ACTIVITY_MONITOR_ORIGIN` or the OS hostname) and an `actor`
  (`ACTIVITY_MONITOR_DISPLAY_USER` or `--actor`, else `user`). Each `focus` event's `task`
  is `ACTIVITY_MONITOR_TASK` when set (the desktop counterpart of the git `TASK_PATTERN` /
  opencode `/task`, so one task ties the sources together), else the default `Unknown` — a task
  is a unit of work, never a process; the foreground process travels in the event's `process`
  field (`data.raw.process` server-side), and it is the only program identity reported — the
  image path and pid are never sent (focus has no `detail`; it is left to the `lock` sleep marker),
  so an untitled window is labelled from that name in the Events list. Each tick also
  compares the wall clock against the unbiased interrupt-time count
  (`QueryUnbiasedInterruptTime`, which stops during sleep) and, on a divergence over `skew_seconds`
  (default 30), enters the locked state at the last pre-sleep tick — closed by the normal lock
  handling at wake (session woke unlocked) or at the real unlock (machine locked to sleep) — so
  sleeping time is not counted as attention. Standalone (`python -m activity_monitor.services.windows_watcher`)
  it POSTs to `/api/ingest/event` (the payload's `source` is `--source`, default `windows`); embedded (`--watch-windows` / `general.watch_windows`) the backend
  starts it in-process and it writes straight to the DB via `store_events`. While it runs it also
  emits a `heartbeat` every `heartbeat_seconds` (default 60) so the server can tell the watcher is
  alive; pings are liveness only and never stored (`store_events` keeps just the latest on the
  source provider), and an open `focus`/`idle`/`lock` span is only trusted while the watcher keeps
  reporting (see User timeline). The standalone
  watcher also takes flags mirroring every client variable (`--base-url`, `--source`, `--actor`,
  `--host`, `--task`, `--process-config`, `--username`, `--password`, `--ca-cert`, `--client-cert`,
  `--client-key`, `--insecure`) plus `--poll`/`--idle-minutes`/`--skew-seconds`/`--heartbeat-seconds`;
  a flag wins over its env var (`ACTIVITY_MONITOR_PROCESS_CONFIG`). `--tray` puts a tray icon in
  the notification area while the standalone watcher runs (`activity_monitor/util/tray.py`, ctypes
  `Shell_NotifyIcon` + a message-loop thread): green = the last event delivery succeeded, red = it
  failed, gray = nothing sent yet; the tooltip/menu show the state and an `Exit` entry stops the
  watcher. The backend takes `--tray` too (see **Run**), reusing the same icon: green = it finished
  starting, `Exit` asks uvicorn for a graceful shutdown. Restart it after editing.
- `tests/` — pytest (`TestClient`, temp config/db)

## Backend (FastAPI, Python 3.12)

- **Run**: `python -m activity_monitor.main` from repo root. Flags: `--config` (default
  `config.yaml`), `--database`, `--port` (default from config, 8001), `--openapi FILE`,
  `--watch-windows` (embed the desktop watcher; or `general.watch_windows` in config),
  `--tray` (the same tray icon as the watcher's, green = the app finished starting, `Exit` stops the
  server; Windows only — `supervisor.py` forwards argv, so `python supervisor.py --tray` works too),
  `--basic-auth-username`/`--basic-auth-password` (require HTTP Basic auth on `/api/*`).
- **Intentionally minimal**: no services framework, no thread pool, no pipeline engine, no
  statistics. Direct async DB access from handlers. Keep it that way unless there is a real need.
- **Config**: `AppConfig` dataclass loaded with `yaml.safe_load`; `config.yaml` is committed and
  holds `general` (api_port, database, optional stale_after_seconds, watch_windows,
  basic_auth_username/basic_auth_password) and `sources`.
- **Auth**: optional HTTP Basic auth on every `/api/*` route via the `require_auth` dependency
  (`api/deps.py`), mounted by `create_app` only when `general.basic_auth_username` is set. Credentials
  are compared with `secrets.compare_digest`; a mismatch returns `401` + `WWW-Authenticate: Basic`.
  The static UI and the OpenAPI docs stay public. All external clients send the
  `ACTIVITY_MONITOR_USERNAME`/`ACTIVITY_MONITOR_PASSWORD` variables.
- **Models**: one table `ActivityEvent` (`source`, `kind`, `actor`, `host`, `task`, `timestamp`,
  `duration_ms`, `title`, `detail`, `tags` JSON, `data` JSON, unique `dedup_key`) + `Program`
  (`program_id`, `source`, `host`, `actor`, `started_at`, `last_seen_at`,
  `ended_at`) for running programs. Tables are created at startup via
  `SQLModel.metadata.create_all`; because there is no migration framework,
  `services.db._ensure_columns` back-fills columns added later to existing databases (currently
  `activityevent.host`).
- **Timestamps**: store and compare everything in UTC (`datetime.now(UTC)`; `parse_timestamp`
  (`util/time.py`) converts naive inputs assuming local time; `as_utc` re-attaches tz to DB reads).
  SQLite stores
  datetimes as UTC-wall strings, so naive/aware mixing silently breaks range filters — keep tests
  and calls timezone-consistent.
- **Sources** (`services/sources.py`): subclass `SourceProvider`; implement `parse(raw) -> dict`. Set
  `is_pull = True` and override `fetch()` for pull sources. Register the class in `PROVIDERS`.
  `parse` returns canonical fields via `make_event(...)`, which stamps each event's `host` from
  `local_host()` (`ACTIVITY_MONITOR_ORIGIN` or the OS hostname) unless the payload supplies one;
  dedup with the `dedup(*parts)` helper.
  `derive_task(...)` extracts a task key from the first candidate matching the `TASK_PATTERN` env
  regex (git: commit subject, then branch; used by `GitHistorySource.parse`). Producers that cannot
  attribute work (a git subject without a key falls back to the repo name; opencode before `/task`
  and watcher `focus` events use the default `UNKNOWN_TASK`, `"Unknown"`) always send a task
  instead of leaving it empty.
  Pull fetches run off the event loop (`asyncio.to_thread`) because `fetch()` is blocking.
- **Ingest**: `POST /api/ingest/event` takes a single event or `{events: [...]}` batch with the
  source name in the payload. A name not in the config gets a transient push provider — events are
  accepted under any name, while `GET /api/sources` keeps listing only the configured ones; a
  configured provider is reused (git events still parse through `GitHistorySource`, `windows`
  heartbeats still feed the staleness check). Idempotent on `dedup_key` (external `id` or payload
  hash). New events are stored,
  duplicates ignored. `store_events` in `services/events.py` is the single write path — except `heartbeat`
  records, which are liveness pings: they are never stored (the latest is kept on the source
  provider for the user timeline's staleness check). A top-level `host` in the
  payload is stored on the event (and removed from `data.raw`). A `task.set` event backs the
  program's untagged events (`data.raw.program.id`/`host` match; untagged = no task or the
  producers' `Unknown` default) with the new task, walking back to
  the previous `task.set` (`_backfill_task`) — the `/task` command only pins later events, so this
  retroactively attributes work done before the id was set while leaving earlier task ranges alone.
- **Programs / presence**: `POST /api/programs/start|beat|end` manage live programs. `beat` bumps
  `last_seen_at`; `end` and `expire_stale` (stale after `stale_after_seconds`) close the program at
  `last_seen_at` — its last activity — so a late exit request does not add a silent tail.
  `GET /api/timelines/presence` groups programs by `(source, host)` and assigns lane numbers at
  query time with `assign_lanes` — a greedy non-overlap schedule, so restarted instances reuse
  the lowest lane and merge into one line. Each lane renders `active`/`idle`/`closed`/`waiting`
  segments (`active_ranges` + `compose_segments`). States are driven by
  event kinds, not matched pairs: `prompt.begin` → `active`, `permission.asked` → `waiting`,
  `permission.replied` → `active`, `question.asked` → `waiting`, `question.replied` → `active`,
  `prompt.end` → `idle` (`_EVENT_STATE`); a run is `idle` by
  default and ends `closed`. Events attach to a lane via `data.raw.program.id`
  (`distribute_events`; because consolidation can split one program across blocks that land in
  different lanes, each event goes to the lane whose block covers its timestamp so it is never drawn
  twice).
  The opencode plugins emit `permission.asked`/`permission.replied` only when the user is actually
  prompted (auto-allowed calls are skipped) so the timeline can show where attention was needed.
  The V2 plugin emits `question.asked`/`question.replied` from the `form.created`/`form.replied`/
  `form.cancelled` events of the built-in `question` tool (`metadata.kind === "question"`); V1 has
  no question tool.
- **Consolidate**: `POST /api/programs/consolidate?idle_seconds=` tidies the stored history of
  finished programs (running ones are untouched): a program id with no events is deleted, each
  program is trimmed to its first/last non-idle boundary, and an idle gap longer than
  `idle_seconds` (default 3600) splits it into separate `Program` rows that share the program id, so
  the dead time is not counted as presence. It also drops every `windows` `focus` open whose span
  already has a close — the close carries `duration_ms`, so the open row only survives as the
  duplicate `merge_focus_spans` collapses away (pairs are matched in `(timestamp, id)` order, the
  same order the merge walks; a trailing open with no close is the live span and is kept, and the
  merged event stays dated at the span start because `merge_focus_spans` back-dates a lone close).
  Idempotent; returns `{removed, trimmed, split, dropped_focus_opens}`.
- **User timeline**: `GET /api/timelines/user` reconstructs desktop activity from the `windows`
  source. `focus_spans` turns `focus` events into spans (a close carries `duration_ms` and ends at
  `timestamp`; a trailing open runs to the last thing heard from the watcher — its last stored
  event or the latest in-memory `heartbeat` ping — plus `stale_after_seconds` (heartbeats are not
  stored, so a ping keeps it live across long stretches without state changes while a stopped
  watcher does not leave a multi-day "active"
  bar), grouping them into one lane per `task` (usually a single `Unknown` lane unless
  `ACTIVITY_MONITOR_TASK` pins the watcher to one). `_pair_intervals` pairs `idle.start`/`idle.end`
  and `lock`/`unlock` into intervals;
  both are clamped to the query range and their open pair is clipped the same way.
- **Code timeline**: `GET /api/timelines/code` returns the `git` source's `commit`/`push` events in
  range; the frontend draws them as one passive `Code` row that is always `closed` (no active/idle
  segments), with event dots.
- **Style**: ruff from root `pyproject.toml` (line-length 120). Run `ruff format .` before finishing.

## Frontend (Vite + React 19 + TS + MUI)

- **Pages**: `TimelinePage` (date range -> one `UnifiedTimeline` merging the `User` row with
  `Agent N` lanes; an `Active task` select filters the timeline to one task — pinning the range to
  that task's first/last activity and shadowing everything else; a `Consolidate` button calls
  `POST /api/programs/consolidate` (server-default idle gap), reloads, and reports the result in a
  `Snackbar`), `TaskPage` (the "Events" tab — search + filters +
  task list -> `Timeline` + event list; tasks are grouping keys, e.g. a Jira id), `SourcesPage`
  (list + fetch + ingest example). `App.tsx` is a MUI `Tabs` shell; page selected by `?page=`.
- **Timeline**: `src/components/Timeline.tsx` renders lanes as responsive HTML (percentage
  positions), dots colored by `kind`, optional duration bars, MUI tooltips.
  `src/components/UnifiedTimeline.tsx` renders one timeline: a single `User` row (all desktop
  `focus` spans flattened, no per-process rows) followed by one row per program lane labelled
  `program@host` (with a `#N` suffix when several lanes share the same program+host), and a passive
  `Code` row (git `commit`/`push` dots on a line that is always closed) below them. All rows share one theme — active = filled bar (user: `focus` spans; agent: `active`
  segments), idle = outlined bar (user: `idle` input intervals; agent: `idle` segments), waiting =
  hatched bar (agent: `waiting` segments, i.e. a permission or question prompt), closed = plain line (user:
  `lock` intervals; agent: `closed` segments) — with event dots overlaid on agent lanes. Permission
  events render as `?` (asked) / `✓` (approved) / `✗` (denied) characters instead of dots, and question
  events as `?` (asked) / `✓` (answered) / `✗` (dismissed) characters, each coloured by state with a
  paper-coloured halo. The timeline
  is cropped to the first/last activity
  (leading/trailing dead time is dropped). Interior dead spans (> 1 h) are compressed to an
  axis-break mark (15 min of context kept on each side); dots falling inside a compressed span are
  hidden (see `buildCompression`). Helpers in `src/lib/timeline.ts`; date/duration formatting in
  `src/lib/format.ts`.
- **Data loading**: effects fetch with `.then(...)` and set state in the callback; avoid calling
  `setState` synchronously inside an effect and avoid impure calls (`Date.now()`, `new Date()`)
  during render — the strict `react-hooks` rules reject them. Compute such defaults at module scope
  or inside async callbacks.
- **Commands**: `npm run lint`, `npm run build`, `npm run dev` (dev proxy `/api` -> 8001).

### Schema generation

`src/api/generated/schema.ts` is generated by `openapi-typescript` — never hand-edit. After
changing endpoints, from the repo root:

```bash
python -m activity_monitor.main --openapi openapi_local.json
cd frontend && npx openapi-typescript ../openapi_local.json -o src/api/generated/schema.ts
```

Delete `openapi_local.json` afterwards (gitignored).

## Verification

- **Tests**: `python -m pytest` from root (`pytest` + `httpx`; `TestClient` drives the full
  lifespan). Add endpoint tests beside `tests/test_api.py`.
- **End-to-end**: run the backend and exercise pull sources via `POST /api/sources/{name}/fetch`
  and pushes via `POST /api/ingest/event`.

### Change Types

| Change | Action |
|--------|--------|
| Backend | Restart the backend (or let the supervisor restart it) |
| API | Regenerate the frontend schema (above) |
| Frontend | `cd frontend && npm run build` |
| opencode plugin | Restart opencode |
| Windows watcher | Restart it standalone (`python -m activity_monitor.services.windows_watcher`), or restart the backend if embedded |
