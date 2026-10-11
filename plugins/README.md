# Activity Monitor clients

Client-side integrations that feed the Activity Monitor. All of them talk to the
backend over HTTP and read its port from a single environment variable,
**`ACTIVITY_MONITOR_PORT`** (default `8001`), so one setting points every client
at your server.

| Variable | Default | Used by |
| --- | --- | --- |
| `ACTIVITY_MONITOR_PORT` | `8001` | all clients below |
| `ACTIVITY_MONITOR_BASE_URL` | `http://localhost:$ACTIVITY_MONITOR_PORT` | opencode plugin (overrides the port) |
| `ACTIVITY_MONITOR_SOURCE` | `opencode` | opencode plugin |
| `ACTIVITY_MONITOR_ACTOR` | `$USER` / `$USERNAME` | opencode plugin |

The backend must be running, and its `general.api_port` (see `config.yaml`) should
match `ACTIVITY_MONITOR_PORT`. If you change one, change the other.

Set the variable for the session (or persist it):

```powershell
$env:ACTIVITY_MONITOR_PORT = "8001"   # PowerShell
```

```bat
set ACTIVITY_MONITOR_PORT=8001        & rem cmd.exe
```

## OpenCode

`opencode-activity.ts` reports the opencode process itself as a **program** and
records prompts against it (see the main `README.md` for the full event list).

Enable it by adding the plugin to an `opencode.json` — this repo already ships
one:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["./plugins/opencode-activity.ts"]
}
```

To use it from anywhere, point the path at this file (absolute path recommended):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["C:/path/to/activity_monitor/plugins/opencode-activity.ts"]
}
```

Make sure `ACTIVITY_MONITOR_PORT` is set before opencode starts, then **restart
opencode** whenever the plugin changes. The plugin posts to
`POST /api/ingest/opencode` and registers its heartbeat via
`POST /api/programs/start|beat|end`.

Run `/task <id>` (e.g. `/task JIRA-123`) to pin the session to a task: the plugin
records a `task.set` event and tags subsequent `prompt.begin`/`prompt.end` events
with that id (`/task` by itself clears it). The command is intercepted before the
model runs, so it never produces an assistant turn.

## Standalone Windows monitor

`../activity_monitor/windows_watcher.py` samples the foreground window, input-idle
time and lock state, and pushes `focus` / `idle.start` / `idle.end` / `lock` /
`unlock` events to the `windows` Push source. It is `ctypes`-only (no extra
dependencies) and windows-only.

With `ACTIVITY_MONITOR_PORT` set, just run it from the repo root:

```bash
python -m activity_monitor.windows_watcher
```

It defaults to `http://127.0.0.1:$ACTIVITY_MONITOR_PORT`. Override per run if
needed:

```bash
python -m activity_monitor.windows_watcher --base-url http://127.0.0.1:8001 --poll 1.0 --idle-minutes 2.0 --verbose
```

Alternatively, run it **inside** the backend (`python -m activity_monitor.main
--watch-windows`, or `general.watch_windows: true` in `config.yaml`): it then
writes straight to the database and needs no port or ingest call.

## Git hooks

Two hooks register git activity with the monitor, both POSTing to
`POST /api/ingest/git` (the `git` source), both reading `ACTIVITY_MONITOR_PORT`,
and both always exiting `0`, so a stopped monitor never blocks a commit or push.

- **`post-commit`** — on every commit, registers the **repository**, **current
  branch**, **commit id**, author, timestamp and subject.
- **`pre-push`** — on every `git push`, registers each pushed ref as a distinct
  `push` event with the **remote**, **branch**, **commit id** and the previous
  (remote) sha. Git has no post-push hook, so it runs just before the transfer;
  ref deletions and no-op pushes are ignored.

Install both for any repository you want to track:

```bash
# point git at this folder (it runs any hook named like post-commit/pre-push)
git config core.hooksPath /absolute/path/to/activity_monitor/plugins

# ...or copy/symlink individual hooks
cp plugins/post-commit .git/hooks/post-commit
cp plugins/pre-push .git/hooks/pre-push
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
