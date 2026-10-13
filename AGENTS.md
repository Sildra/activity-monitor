# AGENTS.md

Activity Monitor — timeline of where attention goes across sources: Python/FastAPI backend +
React/TS/Vite frontend, single server serving the built frontend.

## Quick Reference

| Task | Command |
|------|---------|
| Run backend | `python -m activity_monitor.main` |
| Run backend (supervised) | `python supervisor.py` |
| Run backend + embedded desktop watcher | `python -m activity_monitor.main --watch-windows` |
| Run Windows watcher (standalone) | `python -m activity_monitor.windows_watcher` |
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
  - `config.py` — `AppConfig.load()` (plain yaml -> dataclass; no config service/tree)
  - `db.py` — async SQLite engine + `async_sessionmaker` + `get_session` dependency
  - `models.py` — `ActivityEvent` (SQLModel) + response models (`EventOut`, `ActorLane`, …)
  - `sources.py` — `SourceProvider` base, `BashHistory`/`GitHistory`/`Push` providers, `PROVIDERS`, `build_sources`
  - `api.py` — all endpoints (one router) + optional HTTP Basic auth dependency (`require_auth`)
- `frontend/` — Vite + React 19 + TS + MUI app
- `plugins/opencode-v2/` — opencode 2 plugin (entry `index.ts`, `@opencode/plugin`) reporting program
  lifecycle + prompts: on load it POSTs `/api/programs/start` with `program_id` = the OS PID
  (+ `host`), heartbeats `/api/programs/beat` every ~60s, and `/api/programs/end` on unload (the
  `setup` cleanup function). Prompts are posted as `prompt.begin`/`prompt.end` to
  `/api/ingest/opencode` with a top-level `program: { id, source, host }` so the presence timeline
  can pin them to a lane (enabled via `opencode.json`; deterministic SHA-1 ids so re-sends dedupe).
  `prompt.begin` is recorded in the `ctx.session.hook("prompt", ...)` hook; `prompt.end` comes from
  the `session.idle` event on `ctx.event.subscribe(...)` (V2 has no `session.error` event). The
  `/task <id>` command is registered by the plugin via `ctx.command.transform` — its executor never
  submits a prompt, so no model turn happens; it records a `task.set` event and pins subsequent
  prompts of that session to the id (a `sessionTasks` map; `/task` alone clears it). Before any
  `/task` an event carries no task (the old project-name default was dropped so the backend can
  backfill); on ingest the backend retags the program's earlier untagged events up to the previous
  `task.set` (see Ingest). `opencode-v1/`
  holds the legacy V1 implementation (`@opencode-ai/plugin`, `command.execute.before` + throw to
  abort) for OpenCode 1 — V1 plugins do not run on V2. Its sidebar indicator is a separate,
  target-exclusive TUI module (`tui.tsx`, loaded from `tui.json`) that subscribes to a namespaced
  `tui.command.execute` event the server half publishes
  (`activity-monitor:<pending|ok|error|shadow>`), since V1 has no server↔TUI RPC bridge.
  `index.ts` also tracks the connection state
  (`pending`/`ok`/`error`, or `shadow` while silent) and publishes it over the plugin RPC contract
  in `rpc.ts`; `tui.tsx` (the package's `./tui` entry, loaded by the CLI automatically) subscribes to
  it and renders a single coloured `● Activity Monitor` row in the TUI sidebar (`sidebar.content`,
  below the built-in MCP and Context sections) — green = last request succeeded, red = failed, gray =
  shadow/pending. This is an OpenCode-only affordance, never sent to the Activity Monitor. Typecheck
  with `npm run typecheck` in the plugin folder. Restart opencode after editing the plugin.
- `plugins/git/` — `post-commit` + `pre-push` git hooks POSTing to `/api/ingest/git`; commit
  registers branch + commit id (+ repo/author/subject), push registers remote + branch + commit +
  prior sha as a distinct `push` event (`GitHistorySource.parse` branches on `kind`). Install with
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
- `activity_monitor/windows_watcher.py` — push watcher for desktop activity: polls the foreground
  window (`GetForegroundWindow`), input-idle time (`GetLastInputInfo`) and lock state
  (`OpenInputDesktop`), emitting `focus` / `idle.start` / `idle.end` / `lock` / `unlock` transitions
  (the `windows` Push source). Stdlib `ctypes` only; each event carries a unique `id` so re-sends
  dedupe, plus a `host` (`ACTIVITY_MONITOR_ORIGIN` or the OS hostname) and an `actor`
  (`ACTIVITY_MONITOR_DISPLAY_USER` or `--actor`, else `user`). Each `focus` event's `task`
  is the foreground process name, or `ACTIVITY_MONITOR_TASK` when set — the desktop counterpart of
  the git `TASK_PATTERN` / opencode `/task`, so one task ties the sources together. Each tick also
  compares the wall clock against the unbiased interrupt-time count
  (`QueryUnbiasedInterruptTime`, which stops during sleep) and, on a divergence over `skew_seconds`
  (default 30), enters the locked state at the last pre-sleep tick — closed by the normal lock
  handling at wake (session woke unlocked) or at the real unlock (machine locked to sleep) — so
  sleeping time is not counted as attention. Standalone (`python -m activity_monitor.windows_watcher`)
  it POSTs to `/api/ingest/windows`; embedded (`--watch-windows` / `general.watch_windows`) the backend
  starts it in-process and it writes straight to the DB via `store_events`. The standalone
  watcher also takes flags mirroring every client variable (`--base-url`, `--source`, `--actor`,
  `--host`, `--task`, `--username`, `--password`, `--ca-cert`, `--client-cert`, `--client-key`,
  `--insecure`) plus `--poll`/`--idle-minutes`/`--skew-seconds`; a flag wins over its env var.
  Restart it after editing.
- `tests/` — pytest (`TestClient`, temp config/db)

## Backend (FastAPI, Python 3.12)

- **Run**: `python -m activity_monitor.main` from repo root. Flags: `--config` (default
  `config.yaml`), `--database`, `--port` (default from config, 8001), `--openapi FILE`,
  `--watch-windows` (embed the desktop watcher; or `general.watch_windows` in config),
  `--basic-auth-username`/`--basic-auth-password` (require HTTP Basic auth on `/api/*`).
- **Intentionally minimal**: no services framework, no thread pool, no pipeline engine, no
  statistics. Direct async DB access from handlers. Keep it that way unless there is a real need.
- **Config**: `AppConfig` dataclass loaded with `yaml.safe_load`; `config.yaml` is committed and
  holds `general` (api_port, database, optional stale_after_seconds, watch_windows,
  basic_auth_username/basic_auth_password) and `sources`.
- **Auth**: optional HTTP Basic auth on every `/api/*` route via the `require_auth` dependency
  (`api.py`), mounted by `create_app` only when `general.basic_auth_username` is set. Credentials
  are compared with `secrets.compare_digest`; a mismatch returns `401` + `WWW-Authenticate: Basic`.
  The static UI and the OpenAPI docs stay public. All external clients send the
  `ACTIVITY_MONITOR_USERNAME`/`ACTIVITY_MONITOR_PASSWORD` variables.
- **Models**: one table `ActivityEvent` (`source`, `kind`, `actor`, `host`, `task`, `timestamp`,
  `duration_ms`, `title`, `detail`, `tags` JSON, `data` JSON, unique `dedup_key`) + `Program`
  (`program_id`, `source`, `host`, `actor`, `started_at`, `last_seen_at`,
  `ended_at`) for running programs. Tables are created at startup via
  `SQLModel.metadata.create_all`; because there is no migration framework, `db._ensure_columns`
  back-fills columns added later to existing databases (currently `activityevent.host`).
- **Timestamps**: store and compare everything in UTC (`datetime.now(UTC)`; `parse_timestamp`
  converts naive inputs assuming local time; `as_utc` re-attaches tz to DB reads). SQLite stores
  datetimes as UTC-wall strings, so naive/aware mixing silently breaks range filters — keep tests
  and calls timezone-consistent.
- **Sources** (`sources.py`): subclass `SourceProvider`; implement `parse(raw) -> dict`. Set
  `is_pull = True` and override `fetch()` for pull sources. Register the class in `PROVIDERS`.
  `parse` returns canonical fields via `make_event(...)`, which stamps each event's `host` from
  `local_host()` (`ACTIVITY_MONITOR_ORIGIN` or the OS hostname) unless the payload supplies one;
  dedup with the `dedup(*parts)` helper.
  `derive_task(...)` extracts a task key from the first candidate matching the `TASK_PATTERN` env
  regex (git: commit subject, then branch; used by `GitHistorySource.parse`).
  Pull fetches run off the event loop (`asyncio.to_thread`) because `fetch()` is blocking.
- **Ingest**: idempotent on `dedup_key` (external `id` or payload hash). New events are stored,
  duplicates ignored. `store_events` in `api.py` is the single write path. A top-level `host` in the
  payload is stored on the event (and removed from `data.raw`). A `task.set` event backs the
  program's untagged events (`data.raw.program.id`/`host` match) with the new task, walking back to
  the previous `task.set` (`_backfill_task`) — the `/task` command only pins later events, so this
  retroactively attributes work done before the id was set while leaving earlier task ranges alone.
- **Programs / presence**: `POST /api/programs/start|beat|end` manage live programs. Stale programs
  (silent for `stale_after_seconds`) are closed via `_expire_stale`, which persists `ended_at`.
  `GET /api/timelines/presence` groups programs by `(source, host)` and assigns lane numbers at
  query time with `_assign_lanes` — a greedy non-overlap schedule, so restarted instances reuse
  the lowest lane and merge into one line. Each lane renders `active`/`idle`/`closed`/`waiting`
  segments (`_active_ranges` + `_compose_segments`). States are driven by
  event kinds, not matched pairs: `prompt.begin` → `active`, `permission.asked` → `waiting`,
  `permission.replied` → `active`, `question.asked` → `waiting`, `question.replied` → `active`,
  `prompt.end` → `idle` (`_EVENT_STATE`); a run is `idle` by
  default and ends `closed`. Events attach to a lane via `data.raw.program.id` (`_lane_events`).
  The opencode plugins emit `permission.asked`/`permission.replied` only when the user is actually
  prompted (auto-allowed calls are skipped) so the timeline can show where attention was needed.
  The V2 plugin emits `question.asked`/`question.replied` from the `form.created`/`form.replied`/
  `form.cancelled` events of the built-in `question` tool (`metadata.kind === "question"`); V1 has
  no question tool.
- **User timeline**: `GET /api/timelines/user` reconstructs desktop activity from the `windows`
  source. `_focus_spans` turns `focus` events into spans (a close carries `duration_ms` and ends at
  `timestamp`; a trailing open runs to the query end), grouping them into one lane per process
  (`task`). `_pair_intervals` pairs `idle.start`/`idle.end` and `lock`/`unlock` into intervals;
  both are clamped to the query range.
- **Code timeline**: `GET /api/timelines/code` returns the `git` source's `commit`/`push` events in
  range; the frontend draws them as one passive `Code` row that is always `closed` (no active/idle
  segments), with event dots.
- **Style**: ruff from root `pyproject.toml` (line-length 120). Run `ruff format .` before finishing.

## Frontend (Vite + React 19 + TS + MUI)

- **Pages**: `TimelinePage` (date range -> one `UnifiedTimeline` merging the `User` row with
  `Agent N` lanes), `TaskPage` (the "Events" tab — search + filters + task list -> `Timeline` +
  event list; tasks are grouping keys, e.g. a Jira id), `SourcesPage` (list + fetch + ingest
  example). `App.tsx` is a MUI `Tabs` shell; page selected by `?page=`.
- **Timeline**: `src/components/Timeline.tsx` renders lanes as responsive HTML (percentage
  positions), dots colored by `kind`, optional duration bars, MUI tooltips.
  `src/components/UnifiedTimeline.tsx` renders one timeline: a single `User` row (all desktop
  `focus` spans flattened, no per-process rows) followed by one row per program lane labelled
  `program@host` (with a `#N` suffix when several lanes share the same program+host), and a passive
  `Code` row (git `commit`/`push` dots on a line that is always closed) below them. All rows share one theme — active = filled bar (user: `focus` spans; agent: `active`
  segments), idle = outlined bar (user: `idle` input intervals; agent: `idle` segments), waiting =
  hatched bar (agent: `waiting` segments, i.e. a permission or question prompt), closed = plain line (user:
  `lock` intervals; agent: `closed` segments) — with event dots overlaid on agent lanes. Permission
  events render as `?` (asked) / `✓` (approved) / `✗` (denied) badges instead of dots, and question
  events as `?` (asked) / `✓` (answered) / `✗` (dismissed) badges. The timeline
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
  and pushes via `POST /api/ingest/{name}`.

### Change Types

| Change | Action |
|--------|--------|
| Backend | Restart the backend (or let the supervisor restart it) |
| API | Regenerate the frontend schema (above) |
| Frontend | `cd frontend && npm run build` |
| opencode plugin | Restart opencode |
| Windows watcher | Restart it standalone (`python -m activity_monitor.windows_watcher`), or restart the backend if embedded |
