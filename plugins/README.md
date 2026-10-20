# Activity Monitor clients

Client-side integrations that feed the Activity Monitor. All of them talk to the
backend over HTTP and read its port from a single environment variable,
**`ACTIVITY_MONITOR_PORT`** (default `8001`), so one setting points every client
at your server.

| Variable | Default | Used by |
| --- | --- | --- |
| `ACTIVITY_MONITOR_URL` | `http://127.0.0.1:8001` | all clients below |
| `ACTIVITY_MONITOR_PORT` | `8001` | legacy fallback for watcher/hooks (if URL not set) |
| `ACTIVITY_MONITOR_HOST` | `127.0.0.1` | legacy fallback |
| `ACTIVITY_MONITOR_BASE_URL` | `http://localhost:$ACTIVITY_MONITOR_PORT` | legacy fallback for opencode plugins |
| `ACTIVITY_MONITOR_SOURCE` | `opencode` | opencode plugin |
| `ACTIVITY_MONITOR_ACTOR` | `$USER` / `$USERNAME` | opencode plugin (event `actor`) |
| `ACTIVITY_MONITOR_DISPLAY_USER` | OS username | all clients (overrides the event `actor`) |
| `ACTIVITY_MONITOR_ORIGIN` | OS hostname | all clients (origin `host` carried on every event) |
| `ACTIVITY_MONITOR_TASK` | — | Windows watcher (task stamped on `focus` events) |
| `ACTIVITY_MONITOR_PROCESS_CONFIG` | — | Windows watcher (YAML file renaming/redacting/deriving focus tasks) |
| `ACTIVITY_MONITOR_SHADOW` | — | opencode plugin (silent mode, `1`/`true`/`yes`/`on`) |
| `ACTIVITY_MONITOR_CA_CERT` | — | all clients (verify server, private CA) |
| `ACTIVITY_MONITOR_CLIENT_CERT` | — | all clients (mTLS client certificate) |
| `ACTIVITY_MONITOR_CLIENT_KEY` | — | all clients (mTLS client key) |
| `ACTIVITY_MONITOR_SSL_INSECURE` | — | all clients (`1` disables verification, dev only) |
| `ACTIVITY_MONITOR_USERNAME` | — | all clients (HTTP Basic auth username) |
| `ACTIVITY_MONITOR_PASSWORD` | — | all clients (HTTP Basic auth password) |

## Display user

Every client stamps each event with the name of the user (`actor`): the watcher uses
`user`, the opencode plugins use `$USER`/`$USERNAME` (or `ACTIVITY_MONITOR_ACTOR`) and the
git hooks use the commit author. Set `ACTIVITY_MONITOR_DISPLAY_USER` to override that name
on every event regardless of source, so one readable handle (e.g. `sildr`) shows up across
the whole timeline. The git author is still kept on the event as `data.author`.

## Origin host

Every client stamps each event with a `host` field naming the machine it ran on: the
watcher, the opencode plugins and the git hooks all send `ACTIVITY_MONITOR_ORIGIN` when
set, otherwise the local hostname. Set it (e.g. `laptop`) when several machines feed one
server, so events from different machines can be told apart.

## Task

The Windows watcher reports each `focus` event's task as `Unknown` — a task is a unit
of work, never a process; the foreground process travels in the event's data instead.
Set `ACTIVITY_MONITOR_TASK` (e.g. a Jira id) to stamp that task on every focus event,
so desktop activity lines up with the matching git commits (`TASK_PATTERN`) and
opencode prompts (the `/task` command).

## TLS / mTLS

Every client can talk to an HTTPS backend and present a client certificate for mTLS.
Set the shared variables before starting the client:

- `ACTIVITY_MONITOR_URL` — use `https://...`.
- `ACTIVITY_MONITOR_CA_CERT` — path to the CA bundle that signed the server cert (so a
  private CA is trusted).
- `ACTIVITY_MONITOR_CLIENT_CERT` / `ACTIVITY_MONITOR_CLIENT_KEY` — the client certificate
  and key to present (mTLS).
- `ACTIVITY_MONITOR_SSL_INSECURE=1` — skip verification (development only; do not use with
  real certificates).

The Windows watcher reads these via a `ssl.SSLContext`; the opencode plugins construct the
same TLS options for their `node:http`/`node:https` requests; the git hooks pass them to
`curl` as `--cacert`, `--cert` and `--key` (plus `-k` when insecure). See the main
`README.md` and `docs/mtls-setup.md` for generating a CA, server and client certificates.

The backend must be running and accessible at the URL configured via
`ACTIVITY_MONITOR_URL` (or legacy variables if still used). For HTTPS/mTLS,
see the main README.

Set the variable for the session (or persist it):

```powershell
$env:ACTIVITY_MONITOR_URL = "https://localhost:8001"   # PowerShell
```

```bat
set ACTIVITY_MONITOR_URL=https://localhost:8001        & rem cmd.exe
```

## Basic auth

If the backend is started with HTTP Basic auth (see `general.basic_auth_username` /
`--basic-auth-username` in the main `README.md`), every client must send credentials.
Set the shared variables before starting the client:

- `ACTIVITY_MONITOR_USERNAME` — the username.
- `ACTIVITY_MONITOR_PASSWORD` — the password (optional; defaults to empty).

```powershell
$env:ACTIVITY_MONITOR_USERNAME = "alice"      # PowerShell
$env:ACTIVITY_MONITOR_PASSWORD = "s3cret"
```

The Windows watcher and the opencode plugins send an
`Authorization: Basic ...` header; the git hooks pass `-u "$USERNAME:$PASSWORD"`
to `curl`. Only the `/api/*` routes are protected — the static UI and `/docs`
stay public.

## OpenCode

Two versions of the opencode plugin report the opencode process itself as a
**program** and record prompts against it (see the main `README.md` for the full
event list):

- **`opencode-v2/`** — the current plugin, for OpenCode 2 (loaded as a local
  plugin directory; the entry point is `index.ts`). It is built on the V2 plugin
  API (`@opencode/plugin`).
- **`opencode-v1/opencode-activity.ts`** — the legacy plugin for OpenCode 1
  (`@opencode-ai/plugin`; the server half), with its TUI indicator in
  `opencode-v1/tui.tsx`. V1 plugin implementations do not run on OpenCode 2.

This repo ships an `opencode.json` that enables the V2 plugin:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["./plugins/opencode-v2"]
}
```

To use it from anywhere, point the path at this directory (absolute path
recommended):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["C:/path/to/activity_monitor/plugins/opencode-v2"]
}
```

On OpenCode 1 the same `plugins` list enables the V1 server half — point at the
file itself (V1 loads `.ts` paths directly):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["C:/path/to/activity_monitor/plugins/opencode-v1/opencode-activity.ts"]
}
```

Without that entry nothing from OpenCode 1 is reported, and the V1 TUI
indicator (below) has nothing feeding it — it just sits on `Connecting`.

Make sure `ACTIVITY_MONITOR_URL` (or legacy `ACTIVITY_MONITOR_PORT`/`ACTIVITY_MONITOR_BASE_URL`)
is set before opencode starts, then **restart opencode** whenever the plugin changes.
The plugin posts to `POST /api/ingest/opencode` and registers its heartbeat via
`POST /api/programs/start|beat|end`. For mTLS, configure the client certificates
with the shared `ACTIVITY_MONITOR_CA_CERT` / `ACTIVITY_MONITOR_CLIENT_CERT` /
`ACTIVITY_MONITOR_CLIENT_KEY` variables (the plugins pass them to `node:https`);
for HTTP Basic auth, set `ACTIVITY_MONITOR_USERNAME` / `ACTIVITY_MONITOR_PASSWORD`.

Run `/task <id>` (e.g. `/task JIRA-123`) to pin the session to a task: the plugin
records a `task.set` event and tags subsequent `prompt.begin`/`prompt.end` events
with that id (`/task` by itself clears it). Work done before a `/task` is reported
under the default task `Unknown`; on ingest the backend backfills the program's
`Unknown`/untagged events up to the previous `task.set`, so the id you set late
still covers the work you already
did — without touching an earlier task's range. The V2 plugin registers `/task` as a
real command whose executor does not submit a prompt, so the model never runs
(the V1 plugin achieved the same by intercepting and aborting the command).

Run `/shadow <prompt>` to send one prompt without reporting it to the Activity
Monitor: the prompt still runs, but no `prompt.begin`/`prompt.end` (or any other)
event is sent for that turn. The plugin strips the `/shadow` prefix in the prompt
hook (V2) or message hook (V1), so the model receives the real text. The TUI shows
a short toast confirming the prompt is being shadowed.

Run `/shadow` with no argument to toggle a permanent shadow for that session:
while it is on, nothing the session produces is reported (prompts, permission
and question events, task changes) and the TUI indicator shows `shadow`; run
`/shadow` again to turn it off. The bare command submits no prompt, so no model
turn happens. Each toggle also shows a toast (warning when switching on,
success when switching off), so the current state is never a guess.

Set `ACTIVITY_MONITOR_SHADOW=1` (also `true`/`yes`/`on`) to do the same for the
whole session: the plugin becomes completely silent and sends nothing to the
Activity Monitor (no program lifecycle, no prompts). The option is read when
opencode starts, so restart opencode after changing it. This is a process-wide
kill switch; `/shadow` only ever affects its own session — one prompt with an
argument, the whole session without one.

While a prompt is blocked on a tool approval, the plugin reports the wait:
`permission.asked` when the approval prompt appears and `permission.replied`
when you answer (`data.status` is `approved` or `denied`, with the wait in
`duration_ms`). The presence timeline renders the wait as a hatched `waiting`
block, and the lane shows the events as `?` / `✓` / `✗`. Auto-approved calls
(saved "always" rules or config `allow`) are **not** reported — only prompts that
actually need your attention. Both `/shadow` and `ACTIVITY_MONITOR_SHADOW`
suppress permission events too.

When the model asks you a question (the built-in `question` tool), the plugin
reports that wait the same way: `question.asked` while the question is on screen
and `question.replied` when it is settled (`data.status` is `answered` or
`dismissed`, with the wait in `duration_ms`). The timeline renders it as the same
hatched `waiting` block and shows the event as `?` / `✓` / `✗`. Questions are a
V2 feature (the tool is backed by a form), so the V1 plugin does not emit these;
both shadow mechanisms suppress them too.

V2 implementation notes:

- `prompt.begin` is recorded in the `ctx.session.hook("prompt", ...)` hook (it
  runs on incoming prompts; `messageID` is available for the dedupe id).
- `prompt.end` is recorded from the turn-completion event on
  `ctx.event.subscribe(...)`: `session.execution.succeeded` / `.failed` /
  `.interrupted` (the status in `data.status` follows the outcome, so a failed or
  interrupted turn is reported as such). `session.idle` and `session.status`
  with `status.type === "idle"` are accepted as fallbacks for builds that still
  emit them. V2 has no `session.error` event.
- `permission.asked` / `permission.replied` come from the same
  `ctx.event.subscribe(...)` stream; the ask is paired to its reply by request id
  to compute the wait duration.
- `question.asked` / `question.replied` come from the `form.created`,
  `form.replied` and `form.cancelled` events on the same stream; the question
  tool tags its form with `metadata.kind === "question"`, so unrelated forms are
  ignored, and replies are paired to the ask by form id.
- V2 has no toast API in the *server* plugin, so plugin errors only reach
  opencode's logs. Transient *user-facing* messages (the `/shadow` toasts) are
  routed to the TUI half over the plugin RPC contract (`rpc.ts` defines a `toast`
  event, and `tui.tsx` shows it with `context.ui.toast.show`).
- The plugin folder pins `@opencode/plugin` in `package.json` so editors can
  type-check it (`npm install` in the folder); the runtime resolves the package
  itself. The TUI half additionally needs the `@opentui/core` / `@opentui/solid`
  / `solid-js` peer packages at type-check time (listed as devDependencies); at
  runtime the OpenCode CLI provides them.

### TUI status indicator (V2)

The V2 plugin also shows its connection state in the OpenCode TUI sidebar (the
right pane) as a single coloured `Activity Monitor` line, rendered just below
the built-in MCP and Context sections (`sidebar.content`):

- **green** — the last report to the Activity Monitor succeeded.
- **red** — the last report failed (the failure message is in the plugin log).
- **gray** — the plugin is not reporting (`ACTIVITY_MONITOR_SHADOW`, a
  `/shadow` turn in flight, or a session sitting in the permanent shadow a bare
  `/shadow` toggled on) or has not reported yet.

The line also carries the task the open session pinned with `/task`, between the
name and the state:

```
● Activity Monitor (AM-123) Connected
```

The task is left out until one is pinned (`/task` with no id clears it again).

The state and the task live in the server half of the plugin (`index.ts`), which
updates them on every `POST` and on `/task`, and publishes them over the plugin
RPC contract in `rpc.ts` (a `status` and a `task` method, plus `state`, `task`
and `toast` events). `tui.tsx` — the package's `./tui` entry, which the OpenCode
CLI loads automatically because the plugin directory is in `opencode.json` —
subscribes to that RPC and renders the indicator, and shows the `toast` events as
transient toasts (`context.ui.toast.show`). The server half can finish
reporting before the TUI half subscribes, so the TUI also seeds the current
state once with `status`, and each session's task the first time it renders that
session; those calls name the TUI's location and pass the methods' required
input objects, because an RPC domain is registered per location and a call
without one answers `rpc.unavailable`. The sidebar
is only shown for a session and when the terminal is wide enough to fit it. This
is an OpenCode-only affordance: nothing about it is sent to the Activity Monitor
or shown in its web UI.

### TUI status indicator (V1)

The V1 plugin shows the same indicator, but as a **separate file**
(`opencode-v1/tui.tsx`): OpenCode 1 loads a TUI plugin from its own module, and a
module may export `server` or `tui`, never both. It renders the same single
coloured `● Activity Monitor (<task>) <state>` line right after the MCP section
(green = last report succeeded, red = failed, gray = shadow/pending), with the
`(<task>)` part left out until the session pins one with `/task`.

Load it from `tui.json` (the TUI plugin list; paths resolve relative to that
file, so use an absolute path to share one clone):

```json
{
  "$schema": "https://opencode.ai/tui.json",
  "plugin": ["./plugins/opencode-v1/tui.tsx"]
}
```

V1 has no RPC bridge between a plugin's server and TUI halves, so
`opencode-activity.ts` hands both to `tui.tsx` over the one server→TUI channel
the SDK exposes: it publishes namespaced `tui.command.execute` events — the state
as `activity-monitor:ok`, `:error`, `:shadow` or `:pending`, and a session's task
as `activity-monitor:task:<sessionID>:<task>` — and `tui.tsx` subscribes to them
and renders the line. The task is matched against the session the sidebar slot is
rendering for, so each session gets its own label. The state is published on
every change and then mirrored every few seconds (and on each heartbeat), with
every pinned task riding along, so a TUI half that subscribes after the initial
publish — it loads later than the server half, and may restart against a
long-lived server — picks up the current label within seconds instead of
sitting on "Connecting". The publish is best-effort: a headless server (or an
older client without `tui.publish`) simply never updates the indicator, and a
rejected publish — the SDK resolves errors instead of throwing, which used to
swallow them entirely — is logged once per distinct failure as
`[activity-monitor] tui publish ...` in the opencode log so a broken channel is
visible. As with V2, nothing about it is sent to the Activity Monitor.

## Standalone Windows monitor

`../activity_monitor/windows_watcher.py` samples the foreground window, input-idle
time and lock state, and pushes `focus` / `idle.start` / `idle.end` / `lock` /
`unlock` events to the `windows` Push source. It is `ctypes`-only (no extra
dependencies) and windows-only.

Each tick also compares the wall clock against the unbiased interrupt-time count
(`QueryUnbiasedInterruptTime`), which stops while the machine is asleep. When
they diverge by more than `--skew-seconds` (default 30) the watcher enters the
locked state at the last pre-sleep tick; the normal lock handling closes it (at
wake if the session woke unlocked, or at the real unlock if the machine locked
itself to sleep), so a sleeping machine's time is not mistaken for attention and
a lock that preceded the sleep is not reported twice.

Set `ACTIVITY_MONITOR_URL` (or legacy vars) and run it from the repo root:

```bash
$env:ACTIVITY_MONITOR_URL="https://localhost:8001"
python -m activity_monitor.windows_watcher
```

It defaults to `http://127.0.0.1:8001` if no URL is set. Every environment
variable above has a matching flag, and the flag wins when both are set:

```bash
python -m activity_monitor.windows_watcher \
    --base-url https://localhost:8001 --source windows \
    --actor sildr --host laptop --task JIRA-1 \
    --username alice --password s3cret \
    --ca-cert certs/ca.crt --client-cert certs/client.crt --client-key certs/client.key \
    --poll 1.0 --idle-minutes 2.0 --skew-seconds 30.0 --verbose
```

- `--base-url` / `--source` — backend root and `Push` source name.
- `--actor` / `--host` / `--task` — event `actor`, origin `host` and `focus` task.
- `--username` / `--password` — HTTP Basic auth.
- `--ca-cert` / `--client-cert` / `--client-key` / `--insecure` — TLS / mTLS.
- `--poll` / `--idle-minutes` / `--skew-seconds` — sampling thresholds.
- `--run SECONDS` / `--once` / `--verbose` — testing aids.

Alternatively, run it **inside** the backend (`python -m activity_monitor.main
--watch-windows`, or `general.watch_windows: true` in `config.yaml`): it then
writes straight to the database and needs no port or ingest call.

## Git hooks

Two hooks register git activity with the monitor, both POSTing to
`POST /api/ingest/git` (the `git` source), both reading `ACTIVITY_MONITOR_PORT`,
and both always exiting `0`, so a stopped monitor never blocks a commit or push.

- **`post-commit`** — on every commit, registers the **repository**, **current
  branch**, **commit id**, author, timestamp, subject and the commit's **diff
  summary** (`git show --stat`, capped in width and file count). The backend
  keeps the block in `data.stat`, the paths in `data.files` and the parsed
  counts in `data.insertions` / `data.deletions`, and appends a compact
  `N files, +x/-y` to the event's detail. A commit with no summary (e.g. a
  merge) stores none of it.
- **`pre-push`** — on every `git push`, registers each pushed ref as a distinct
  `push` event with the **remote**, **branch**, **commit id** and the previous
  (remote) sha. Git has no post-push hook, so it runs just before the transfer;
  ref deletions and no-op pushes are ignored. For HTTPS with self-signed certs or mTLS,
  pass `-k` (insecure) to curl or ensure CA/client certs are configured in the shell
  environment. The hooks use `curl`, respect `ACTIVITY_MONITOR_URL`, and pass
  `-u "$ACTIVITY_MONITOR_USERNAME:$ACTIVITY_MONITOR_PASSWORD"` when basic auth is
  configured.

Install both for any repository you want to track:

```bash
# point git at this folder (it runs any hook named like post-commit/pre-push)
git config core.hooksPath /absolute/path/to/activity_monitor/plugins/git

# ...or copy/symlink individual hooks
cp plugins/git/post-commit .git/hooks/post-commit
cp plugins/git/pre-push .git/hooks/pre-push
chmod +x .git/hooks/post-commit .git/hooks/pre-push
```

The hooks need `git` and `curl` on `PATH` (both ship with Git for Windows).

> The `git` source in `config.yaml` is a pull `GitHistory` source that also accepts
> these pushes: commit events dedupe against the same commit seen by `git log`,
> while `push` events are keyed by remote + ref + commit so they stay distinct.
> Enable the source (`sources.git.enabled: true`).
> Git event tasks are derived by the backend from the `TASK_PATTERN` environment
> variable (set on the backend): the commit subject is searched first, then the
> branch, and the first match becomes the task (falling back to the repo name).
