# Activity Monitor

A self-hosted monitor for where your attention goes during a workday. Producers — git
hooks, the opencode plugins, the Windows desktop watcher, or anything that can POST —
push events into one store, explored through two views: the Timeline (one `User` row
for desktop activity, one row per agent session, one passive `Code` row for git) and
Events (search + filters, grouped by task).

## Quick Start

### 1. Install

```bash
pip install -e .
```

This installs the package with the `activity-monitor` and `activity-monitor-windows`
entry points.

### 2. Configure

Create `config.yaml` in the project root (or point to it with `--config`). A minimal
example:

```yaml
general:
  api_port: 8001
  database: activity_monitor.db
  stale_after_seconds: 300
  watch_windows: false
```

Sources need no configuration — see below.

### 3. Run

From the repo root:

```bash
python -m activity_monitor.main
```

Or restart-supervised: `python supervisor.py`.

Flags: `--config PATH`, `--database PATH`, `--port INT` (or `ACTIVITY_MONITOR_PORT`),
`--watch-windows` (embed the desktop watcher), `--tray` (Windows tray icon with an
`Exit` entry), `--ssl` / `--ssl-certfile` / `--ssl-keyfile` / `--ssl-ca-certs` /
`--ssl-cert-reqs {none,optional,required}`, `--basic-auth-username` /
`--basic-auth-password`.

The UI is served at `http://localhost:8001` (or `https://localhost:8001` if SSL is
enabled). OpenAPI docs at `/docs` — the endpoint contract lives there.

## Sources

Every producer is a push client: it posts its events to `POST /api/ingest/event`
with the source name in the payload. Any name is accepted (unknown names get a
transient provider; `git` and `windows` have built-in ones), and `GET /api/sources`
lists the sources observed in the events table. The built-in clients are the git
hooks, the opencode plugins and the Windows watcher — install them via
`plugins/README.md`.

## HTTPS & mTLS

The backend can serve HTTPS and optionally require client certificates:

```bash
python -m activity_monitor.main --ssl \
  --ssl-certfile certs/server.crt \
  --ssl-keyfile certs/server.key \
  --ssl-ca-certs certs/ca.crt \
  --ssl-cert-reqs required
```

or via `config.yaml` (`general.ssl_enabled`, `ssl_certfile`, `ssl_keyfile`,
`ssl_ca_certs`, `ssl_cert_reqs`). For `required`, only clients presenting a valid
certificate signed by the CA can connect; for `optional`, an unauthenticated client
may still connect. Generate a private CA, server and client certificates with
OpenSSL by following `docs/mtls-setup.md`; import `ca.crt` as a trusted root and the
client `.p12` into the browser for UI access over mTLS.

## HTTP Basic auth

As a transport-independent alternative (or complement) to mTLS, the `/api/*` routes
can require HTTP Basic auth (`general.basic_auth_username` / `--basic-auth-username`,
plus `basic_auth_password`). Requests without valid credentials get `401` with a
`WWW-Authenticate: Basic` challenge; the static UI and `/docs` stay public (the
browser prompts on the first API call). Credentials are compared in constant time.

## Client configuration

All external clients (the Windows watcher, the opencode plugins, the git hooks) read
the same environment variables — backend URL, TLS/mTLS material, basic-auth
credentials, origin `host`, display user, task pinning and shadow mode. The table and
per-client setup live in `plugins/README.md`.

**Security note:** the `certs/` directory contains private keys. Do not commit
`.key` or `.p12` files; `certs/` is gitignored by default.
