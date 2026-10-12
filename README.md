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
- `--ssl` — enable HTTPS
- `--ssl-certfile PATH` — server certificate file
- `--ssl-keyfile PATH` — server private key file
- `--ssl-ca-certs PATH` — CA file to verify client certificates (for mTLS)
- `--ssl-cert-reqs {none,optional,required}` — client cert requirement (default `none`)

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

### Client configuration

All external clients read `ACTIVITY_MONITOR_URL` (see `plugins/README.md`).

For HTTPS without client cert:
- **curl**: `curl -k https://localhost:8001/...` for self-signed; if CA is trusted, omit `-k`.
- **httpx** (Python clients): set `verify` to CA path or `True` if system-trusted.
- **Node fetch** (opencode plugins): works if CA is trusted; for private CA set `NODE_EXTRA_CA_CERTS=certs/ca.crt`.

For mTLS (client cert required):
- **curl**: `curl --cert certs/client.crt --key certs/client.key https://localhost:8001/...`
- **httpx**: `httpx.AsyncClient(verify="certs/ca.crt", cert=("certs/client.crt", "certs/client.key"))`
- **Node fetch**: provide a custom HTTPS agent with `ca`, `cert`, `key` (e.g. using `undici` or `https` module).
- **Browser**: import client cert (`client.p12`) and CA if needed; browser will prompt when connecting.

**Security note:** The `certs/` directory contains private keys. Do not commit private
keys to version control. The example above uses `certs/` which is gitignored in typical
setups; only commit `certs/*.crt` if needed, never `.key` or `.p12`.

## Sources

Sources are configured in `config.yaml` under `sources`. Each has a `type` and optional
parameters. The backend polls pull sources on demand via the API or you can push events
to the ingest endpoints.
