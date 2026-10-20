# AGENTS.md

Activity Monitor — timeline of where attention goes across sources: Python/FastAPI backend +
React/TS/Vite frontend, single server serving the built frontend.

## Quick Reference

| Task | Command |
|------|---------|
| Run backend | `python -m activity_monitor.main` |
| Run backend (supervised) | `python supervisor.py` |
| Run backend + embedded desktop watcher | `general.watcher: true` in `config.yaml`, then `python -m activity_monitor.main` |
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
    `sources.py` (list/ingest), `programs.py` (start/beat/end/consolidate),
    `timelines.py` (presence/user/code/agentic), `events.py` (`/tasks`, `/tasks/summary`,
    `/actors`, `/kinds`, `/stats`), plus `deps.py` (`SessionDep`, `require_auth`,
    `stale_after`)
  - `services/` — `config.py` (`AppConfig.load()`; plain yaml -> dataclass; no config
    service/tree), `db.py` (async SQLite engine + `async_sessionmaker` + `get_session`
    dependency), `sources.py` (`SourceProvider` base, `Push`/`GitHistory`
    providers, built-in `build_sources`), `events.py` (`store_events` single write
    path + focus open/close helpers), `programs.py` (`expire_stale`,
    `find_active_program`), `timelines.py` (lane/segment/span computation),
    `windows_watcher.py` (desktop watcher — see below)
  - `model/` — SQLModel tables + Pydantic DTOs, one module per entity: `event.py`
    (`ActivityEvent`, `EventOut`, `StatsOut`, `TaskSummary`), `program.py`
    (`Program`, program/presence DTOs + `ConsolidateOut`), `timeline.py` (user/code/
    agentic lane DTOs)
  - `util/` — `time.py` (`as_utc`, `parse_timestamp`), `process_config.py`, `tray.py`
- `frontend/` — Vite + React 19 + TS + MUI app
- `plugins/opencode-v2/` — opencode plugin for OpenCode 2 (entry `index.ts`). Reports the
  program lifecycle (`/api/programs/start|beat|end`) and prompts/permissions/questions/task
  pins via `/api/ingest/event` (source `opencode`, with a top-level `program` block so the
  presence timeline can pin events to a lane). `prompt.begin` from the prompt hook,
  `prompt.end` from turn-completion events; `permission.*`/`question.*` only when the user
  is actually prompted; `/task <id>` records a `task.set` and pins later prompts (no model
  turn). A TUI sidebar half (`tui.tsx`, `./tui` entry) shows connection state + pinned task
  over the plugin RPC contract (`rpc.ts`). `plugins/opencode-v1/` is the legacy OpenCode 1
  implementation (separate TUI module over namespaced events instead of RPC). Client setup
  in `plugins/README.md`; typecheck with `npm run typecheck`; restart opencode after edits.
- `plugins/git/` — `post-commit` + `pre-push` git hooks POSTing to `/api/ingest/event`
  (source `git`); commit registers branch + commit id (+ repo/author/subject) plus the
  `git show --stat` diff summary, push registers remote + branch + commit + prior sha as a
  distinct `push` event. Install with `git config core.hooksPath <repo>/plugins/git`.
- Every client (plugins, hooks, standalone watcher) reads `ACTIVITY_MONITOR_URL` (plus
  mTLS/basic-auth/`ORIGIN`/`DISPLAY_USER` variables and the legacy `ACTIVITY_MONITOR_PORT`,
  default 8001); the standalone watcher uses them as overrides for its `watcher:` config
  section. The backend takes the port from `general.api_port` only.
- `pyproject.toml` — metadata, deps, ruff config, hatchling build, scripts `activity-monitor` +
  `activity-monitor-windows`
- `supervisor.py` — restarts `python -m activity_monitor.main`
- `activity_monitor/services/windows_watcher.py` — push watcher for desktop activity: polls the
  foreground window, input-idle time and lock state, emitting `focus` / `idle.start` /
  `idle.end` / `lock` / `unlock` transitions (the `windows` source). Stdlib `ctypes` only;
  each event carries a unique `id` so re-sends dedupe, plus `host`/`actor` stamps. `focus`
  tasks come from `ACTIVITY_MONITOR_TASK` (default `Unknown` — a task is a unit of work, the
  foreground process travels in the event's data). Sleep is detected via
  `QueryUnbiasedInterruptTime` skew and counted as locked, not attention. Standalone it
  POSTs to `/api/ingest/event`, reading the `watcher:` section of `config.yaml` (client env
  vars override it; every other section is ignored); embedded it writes straight to the DB
  via `store_events` when `general.watcher` is set. `--run`/`--once` are testing aids;
  `--config` picks the file. Off Windows both entry points exit silently. `watcher.tray`
  shows a green/red/gray tray icon (`util/tray.py`). Restart after editing.
- `tests/` — pytest (`TestClient`, temp config/db)

## Backend (FastAPI, Python 3.12)

- **Run**: `python -m activity_monitor.main` from repo root. Flags: `--config` (default
  `config.yaml`) and `--openapi FILE` (dev: dump the schema and exit); every other knob —
  port, database, embedded watcher, tray, SSL, basic auth — lives in the config file.
  `supervisor.py` forwards argv.
- **Intentionally minimal**: no services framework, no thread pool, no pipeline engine, no
  statistics. Direct async DB access from handlers. Keep it that way unless there is a real need.
- **Config**: `AppConfig` (`general` + `watcher` dataclasses) loaded with `yaml.safe_load`.
  The file materializes itself: `AppConfig.load` writes a missing file with every default and
  rewrites one that is missing keys, so `config.yaml` always documents all knobs (rewrites
  keep values, drop comments/unknown keys). `general` holds the server (api_port, database,
  stale_after_seconds, `watcher` to embed the desktop watcher, ssl_*, basic_auth_*); the
  `watcher` section holds the desktop watcher's settings (tray, sampling thresholds,
  source/actor/host/task, process_config, standalone connection settings). Sources need no
  configuration (see Ingest).
- **Auth**: optional HTTP Basic auth on every `/api/*` route via the `require_auth` dependency
  (`api/deps.py`), mounted by `create_app` only when `general.basic_auth_username` is set.
  Credentials compared with `secrets.compare_digest`; a mismatch returns `401` +
  `WWW-Authenticate: Basic`. The static UI and the OpenAPI docs stay public.
- **Models**: one table `ActivityEvent` (`source`, `kind`, `actor`, `host`, `task`, `timestamp`,
  `duration_ms`, `title`, `detail`, `tags` JSON, `data` JSON, unique `dedup_key`) + `Program`
  (`program_id`, `source`, `host`, `actor`, `started_at`, `last_seen_at`, `ended_at`).
  Tables are created at startup via `SQLModel.metadata.create_all`; with no migration
  framework, `services.db._ensure_columns` back-fills columns added later (currently
  `activityevent.host`).
- **Timestamps**: store and compare everything in UTC (`datetime.now(UTC)`; `parse_timestamp`
  converts naive inputs assuming local time; `as_utc` re-attaches tz to DB reads). SQLite
  stores datetimes as UTC-wall strings, so naive/aware mixing silently breaks range filters —
  keep tests and calls timezone-consistent.
- **Sources** (`services/sources.py`): producers are push clients; a provider only implements
  `parse(raw) -> dict`, turning a source-native record into canonical fields via
  `make_event(...)` (host stamped from `local_host()` unless the payload supplies one; dedup
  with `dedup(*parts)`). Two providers are built in (`build_sources`, config-free):
  `PushSource` (the generic parser, also the fallback for unknown names) and
  `GitHistorySource` (the git hooks' payloads). `derive_task(...)` extracts a task key from
  the first candidate matching the `TASK_PATTERN` env regex (git: commit subject, then
  branch). Producers that cannot attribute work send the default `Unknown` task instead of
  leaving it empty, so the backend can backfill.
- **Ingest**: `POST /api/ingest/event` takes a single event or `{events: [...]}` batch with
  the source name in the payload. Any name is accepted (no built-in provider -> transient
  `PushSource`); built-ins (`git`, `windows`) are reused so their parsing and heartbeats
  apply. `GET /api/sources` lists the sources observed in the events table (distinct,
  alphabetical). Idempotent on `dedup_key` (external `id` or payload hash): new events stored,
  duplicates ignored. `store_events` in `services/events.py` is the single write path —
  except `heartbeat` records, which are liveness pings: never stored (the latest is kept on
  the source provider for the user timeline's staleness check). A top-level `host` in the
  payload is stored on the event (and removed from `data.raw`). A `task.set` event backtags
  the program's untagged events back to the previous `task.set` (`_backfill_task`), so a late
  `/task` still attributes earlier work without touching earlier task ranges.
- **Programs / presence**: `POST /api/programs/start|beat|end` manage live programs. `beat`
  bumps `last_seen_at`; `end` and `expire_stale` (stale after `stale_after_seconds`) close the
  program at `last_seen_at` — its last activity — so a late exit does not add a silent tail.
  `GET /api/timelines/presence` groups programs by `(source, host)` and assigns lanes at query
  time with `assign_lanes` (greedy non-overlap, so restarted instances reuse the lowest lane).
  Each lane renders `active`/`idle`/`closed`/`waiting` segments (`active_ranges` +
  `compose_segments`). States are driven by event kinds, not matched pairs: `prompt.begin` →
  `active`, `permission.asked`/`question.asked` → `waiting`, `permission.replied`/
  `question.replied` → `active`, `prompt.end` → `idle` (`_EVENT_STATE`); a run is `idle` by
  default and ends `closed`. Events attach to a lane via `data.raw.program.id`
  (`distribute_events`; each event goes to the lane whose block covers its timestamp, since
  consolidation can split one program across lanes).
- **Consolidate**: `POST /api/programs/consolidate?idle_seconds=` tidies stored history of
  finished programs: empty program ids deleted, programs trimmed to first/last non-idle
  boundary, idle gaps longer than `idle_seconds` (default 3600) split into separate `Program`
  rows sharing the id. Also drops every `windows` `focus` open whose span already has a close
  (the close carries `duration_ms`; the open survives only as the duplicate
  `merge_focus_spans` collapses away). Idempotent; returns
  `{removed, trimmed, split, dropped_focus_opens}`.
- **User timeline**: `GET /api/timelines/user` reconstructs desktop activity from the `windows`
  source. `focus_spans` turns `focus` events into spans (a close carries `duration_ms`; a
  trailing open runs to the last thing heard from the watcher — last stored event or latest
  in-memory `heartbeat` — plus `stale_after_seconds`, so a stopped watcher does not leave a
  multi-day "active" bar), grouped into one lane per `task`. `_pair_intervals` pairs
  `idle.start`/`idle.end` and `lock`/`unlock`; intervals are clamped to the query range.
- **Code timeline**: `GET /api/timelines/code` returns the `git` source's `commit`/`push`
  events in range; the frontend draws them as one passive `Code` row that is always `closed`,
  with event dots.
- **Style**: ruff from root `pyproject.toml` (line-length 120). Run `ruff format .` before finishing.

## Frontend (Vite + React 19 + TS + MUI)

- **Pages**: `TimelinePage` (date range -> one `UnifiedTimeline` merging the `User` row with
  `Agent N` lanes; an `Active task` select filters to one task — pinning the range to that
  task's first/last activity and shadowing everything else; a `Consolidate` button calls
  `POST /api/programs/consolidate`, reloads, and reports the result in a `Snackbar`),
  `TaskPage` (the "Events" tab — search + filters (source/actor/kind from `/api/sources`,
  `/api/actors`, `/api/kinds`) + task list -> `Timeline` + event list). `App.tsx` is a MUI
  `Tabs` shell; page selected by `?page=`.
- **Timeline**: `src/components/Timeline.tsx` renders lanes as responsive HTML (percentage
  positions), dots colored by `kind`, optional duration bars, MUI tooltips.
  `src/components/UnifiedTimeline.tsx` renders one timeline: a single `User` row (all desktop
  `focus` spans flattened) followed by one row per program lane labelled `program@host` (with
  a `#N` suffix when several lanes share the same program+host), and a passive `Code` row
  below. All rows share one theme — active = filled bar (user: `focus` spans; agent: `active`
  segments), idle = outlined bar (user: `idle` input intervals; agent: `idle` segments),
  waiting = hatched bar (agent permission/question prompts), closed = plain line (user: `lock`
  intervals; agent: `closed` segments) — with event dots overlaid on agent lanes. Permission
  and question events render as `?`/`✓`/`✗` characters instead of dots. The timeline is
  cropped to the first/last activity; interior dead spans (> 1 h) compress to an axis-break
  mark (15 min of context each side), hiding dots inside them (see `buildCompression`).
  Helpers in `src/lib/timeline.ts`; date/duration formatting in `src/lib/format.ts`.
- **Data loading**: effects fetch with `.then(...)` and set state in the callback; avoid
  calling `setState` synchronously inside an effect and avoid impure calls (`Date.now()`,
  `new Date()`) during render — the strict `react-hooks` rules reject them. Compute such
  defaults at module scope or inside async callbacks.
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
- **End-to-end**: run the backend and exercise pushes via `POST /api/ingest/event`.

### Change Types

| Change | Action |
|--------|--------|
| Backend | Restart the backend (or let the supervisor restart it) |
| API | Regenerate the frontend schema (above) |
| Frontend | `cd frontend && npm run build` |
| opencode plugin | Restart opencode |
| Windows watcher | Restart it standalone (`python -m activity_monitor.services.windows_watcher`), or restart the backend if embedded |
