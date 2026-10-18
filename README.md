# Activity Monitor

A self-hosted monitor for where your attention goes during a workday. Events from many
sources — shell history, git commits, an editor/agent plugin, desktop activity — are normalized
into a single timeline and explored through two views.

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
sources:
  - type: bash_history
    path: ~/.bash_history
  - type: git_history
    repo: C:/path/to/repo
```

See `src/activity_monitor/sources.py` for available source types.

### 3. Run

From the repo root:

```bash
python -m activity_monitor.main
```

Flags:
- `--config PATH` — path to config.yaml (default `config.yaml`)
- `--database PATH` — override database location
- `--port INT` — override API port (or set `ACTIVITY_MONITOR_PORT`)
- `--watch-windows` — embed the Windows desktop watcher
- `--tray` — tray icon (green once the server has started) with an `Exit` entry that stops it; Windows only
- `--ssl` — enable HTTPS
- `--ssl-certfile PATH` — server certificate file
- `--ssl-keyfile PATH` — server private key file
- `--ssl-ca-certs PATH` — CA file to verify client certificates (for mTLS)
- `--ssl-cert-reqs {none,optional,required}` — client cert requirement (default `none`)
- `--basic-auth-username NAME` — require HTTP Basic auth with this username
- `--basic-auth-password PASS` — HTTP Basic auth password

Or run with the supervisor:

```bash
python supervisor.py
```

The UI is served at `http://localhost:8001` (or `https://localhost:8001` if SSL is enabled).
OpenAPI docs at `/docs`.

## HTTPS & mTLS (Client Certificates)

You can run the backend over HTTPS and optionally require client certificates for
authentication.

### Generate a private CA, server cert, and client certs

From the repo root, create a `certs/` directory:

```bash
mkdir -p certs
cd certs
```

#### 1. Create a private CA

```bash
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 -out ca.crt -subj "/CN=ActivityMonitor-CA"
```

#### 2. Create a server certificate (for localhost)

```bash
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=localhost"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 -sha256 -extfile <(printf "subjectAltName=DNS:localhost,IP:127.0.0.1")
```

On Windows (Git Bash or WSL), the `extfile` with inline command works. If using cmd,
create a file `server.ext` containing `subjectAltName=DNS:localhost,IP:127.0.0.1` and use
`-extfile server.ext`.

#### 3. Create a client certificate

```bash
openssl genrsa -out client.key 2048
openssl req -new -key client.key -out client.csr -subj "/CN=alice"
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out client.crt -days 365 -sha256
```

#### 4. Bundle for browser use (optional)

```bash
openssl pkcs12 -export -out client.p12 -inkey client.key -in client.crt -certfile ca.crt
```

Import `ca.crt` into your OS/browser as a Trusted Root CA, and `client.p12` into your
browser as a personal/client certificate. When connecting with mTLS required, the
browser will prompt you to select the client cert.

### Run the backend with HTTPS

Using CLI flags:

```bash
python -m activity_monitor.main --ssl \
  --ssl-certfile certs/server.crt \
  --ssl-keyfile certs/server.key
```

Using `config.yaml`:

```yaml
general:
  api_port: 8001
  ssl_enabled: true
  ssl_certfile: certs/server.crt
  ssl_keyfile: certs/server.key
```

### Run the backend with mTLS (require client certificates)

To require valid client certs signed by your CA:

```bash
python -m activity_monitor.main --ssl \
  --ssl-certfile certs/server.crt \
  --ssl-keyfile certs/server.key \
  --ssl-ca-certs certs/ca.crt \
  --ssl-cert-reqs required
```

Or in `config.yaml`:

```yaml
general:
  api_port: 8001
  ssl_enabled: true
  ssl_certfile: certs/server.crt
  ssl_keyfile: certs/server.key
  ssl_ca_certs: certs/ca.crt
  ssl_cert_reqs: required
```

**Note:** For `required`, only clients presenting a valid certificate signed by the CA
can connect. For `optional`, clients may connect without a cert (unauthenticated) or
with a valid cert.

### HTTP Basic auth

As a transport-independent alternative (or complement) to mTLS, the `/api/*` routes can
require HTTP Basic auth. When `basic_auth_username` is set, requests without valid
credentials get `401 Unauthorized` with a `WWW-Authenticate: Basic` challenge; the
static UI and the OpenAPI docs stay public (the browser prompts on the first API call).

Using CLI flags:

```bash
python -m activity_monitor.main --basic-auth-username alice --basic-auth-password s3cret
```

Or in `config.yaml`:

```yaml
general:
  api_port: 8001
  basic_auth_username: alice
  basic_auth_password: s3cret
```

Credentials are compared in constant time. If only `basic_auth_username` is set, the
password is treated as empty.

### Client configuration

All external clients (the Windows watcher, the opencode plugins and the git hooks) read the
same variables and support HTTPS + mTLS + basic auth (see `plugins/README.md`):

| Variable | Purpose |
| --- | --- |
| `ACTIVITY_MONITOR_URL` | Backend root, e.g. `https://localhost:8001` |
| `ACTIVITY_MONITOR_CA_CERT` | CA bundle that signed the server cert (private CA) |
| `ACTIVITY_MONITOR_CLIENT_CERT` | Client certificate to present (mTLS) |
| `ACTIVITY_MONITOR_CLIENT_KEY` | Client private key (mTLS) |
| `ACTIVITY_MONITOR_SSL_INSECURE` | `1` to skip verification (development only) |
| `ACTIVITY_MONITOR_USERNAME` | HTTP Basic auth username |
| `ACTIVITY_MONITOR_PASSWORD` | HTTP Basic auth password |
| `ACTIVITY_MONITOR_ORIGIN` | Origin `host` sent with every event (default: the machine hostname) |
| `ACTIVITY_MONITOR_TASK` | Windows watcher: task stamped on desktop `focus` events (default: `Unknown` — a task is a unit of work, not a process; the foreground process is stored in the event's data) |
| `ACTIVITY_MONITOR_PROCESS_CONFIG` | Windows watcher: path to a YAML file of input/output couples to rename, redact and extract tasks from focus windows (overrides `--process-config`) |
| `ACTIVITY_MONITOR_DISPLAY_USER` | Event `actor` sent by every client (default: `user` / `$USER` / `$USERNAME` / git author) |

```powershell
$env:ACTIVITY_MONITOR_URL = "https://localhost:8001"
$env:ACTIVITY_MONITOR_CA_CERT = "certs/ca.crt"
$env:ACTIVITY_MONITOR_CLIENT_CERT = "certs/client-alice.crt"
$env:ACTIVITY_MONITOR_CLIENT_KEY = "certs/client-alice.key"
$env:ACTIVITY_MONITOR_USERNAME = "alice"
$env:ACTIVITY_MONITOR_PASSWORD = "s3cret"
```

The clients apply these directly:

- **Windows watcher** — builds an `ssl.SSLContext` from the variables above and sends an
  `Authorization: Basic ...` header. The standalone watcher accepts every variable above as
  a matching flag (`--base-url`, `--actor`, `--host`, `--task`, `--username`, `--password`,
  `--ca-cert`, `--client-cert`, `--client-key`, `--insecure`); a flag wins over its
  environment variable.
- **opencode plugins** — use `node:http`/`node:https` with `ca`, `cert` and `key` options
  and send the `Authorization` header.
- **git hooks** — pass `--cacert`, `--cert`, `--key` and `-u "$USERNAME:$PASSWORD"` to
  `curl` (`-k` when insecure).
- **Browser** — import `ca.crt` as a trusted root and `client-alice.p12` as a personal
  certificate; the browser prompts for the client cert when the server requires mTLS, and
  for basic auth credentials on the first API call.

For a browser-only session (no client cert), simply trust `certs/ca.crt`.

The opencode plugins also support a silent mode: `ACTIVITY_MONITOR_SHADOW=1` sends nothing to
the monitor, `/shadow <prompt>` skips reporting for a single prompt, and a bare `/shadow`
toggles a permanent per-session shadow (see `plugins/README.md`).

**Security note:** The `certs/` directory contains private keys. Do not commit private
keys to version control. The example above uses `certs/` which is gitignored in typical
setups; only commit `certs/*.crt` if needed, never `.key` or `.p12`.

## Sources

Sources are configured in `config.yaml` under `sources`. Each has a `type` and optional
parameters. The backend polls pull sources on demand via the API or you can push events
to the ingest endpoints.
