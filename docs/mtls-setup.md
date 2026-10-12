# mTLS Setup Guide

This guide walks you through generating certificates for HTTPS and mutual TLS (mTLS) with the Activity Monitor backend.

All commands below assume you're in the repository root and using a shell with OpenSSL available (Git Bash, WSL, or Linux/macOS).

## Create the certs directory

```bash
mkdir -p certs
cd certs
```

## 1. Create a private Certificate Authority (CA)

The CA signs both the server and client certificates. Clients trust the server if they trust the CA, and the server trusts clients if their cert is signed by this CA.

```bash
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 -out ca.crt -subj "/CN=ActivityMonitor-CA"
```

- `ca.key` — private key for the CA (keep secure, never commit)
- `ca.crt` — public CA certificate (can be distributed/trusted)

## 2. Create the server certificate

The server cert is presented by the backend for HTTPS. It must include the correct hostname (localhost).

```bash
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=localhost"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 -sha256 -extfile <(printf "subjectAltName=DNS:localhost,IP:127.0.0.1")
```

On Windows cmd.exe (not Git Bash), create `server.ext` with:

```text
subjectAltName=DNS:localhost,IP:127.0.0.1
```

Then run:

```cmd
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 -sha256 -extfile server.ext
```

## 3. Create client certificates

Create one client cert per user/device. The Common Name (CN) can identify the client.

```bash
openssl genrsa -out client-alice.key 2048
openssl req -new -key client-alice.key -out client-alice.csr -subj "/CN=alice"
openssl x509 -req -in client-alice.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out client-alice.crt -days 365 -sha256
```

To create more clients, repeat with different filenames and CNs.

## 4. Bundle for browser import (PKCS#12)

Browsers typically import client certificates as `.p12`/`.pfx` files (containing cert + private key).

```bash
openssl pkcs12 -export -out client-alice.p12 -inkey client-alice.key -in client-alice.crt -certfile ca.crt
```

You'll be prompted for a password to protect the bundle.

## 5. Trust the CA in your browser/system

For a private CA to work without warnings:

1. Import `ca.crt` into your OS/browser as a **Trusted Root Certification Authority**.
2. Import `client-alice.p12` into your browser as a **Personal/Client Certificate** (under Certificates > Your Certificates).
3. When connecting to an mTLS-enabled backend, the browser will prompt you to select which client certificate to present.

## 6. Run the backend

### HTTPS only (encryption + server identity)

```bash
python -m activity_monitor.main --ssl \
  --ssl-certfile certs/server.crt \
  --ssl-keyfile certs/server.key
```

Or in `config.yaml`:

```yaml
general:
  ssl_enabled: true
  ssl_certfile: certs/server.crt
  ssl_keyfile: certs/server.key
```

### mTLS (require valid client certificates)

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
  ssl_enabled: true
  ssl_certfile: certs/server.crt
  ssl_keyfile: certs/server.key
  ssl_ca_certs: certs/ca.crt
  ssl_cert_reqs: required
```

`ssl_cert_reqs` values: `none` (no client cert), `optional` (accept if present and valid), `required` (must present valid client cert signed by CA).

## 7. Configure clients

The bundled clients (Windows watcher, opencode plugins, git hooks) read a shared set of
environment variables, so one configuration points them all at the HTTPS/mTLS backend:

| Variable | Purpose |
| --- | --- |
| `ACTIVITY_MONITOR_URL` | Backend root, e.g. `https://localhost:8001` |
| `ACTIVITY_MONITOR_CA_CERT` | CA bundle that signed the server cert |
| `ACTIVITY_MONITOR_CLIENT_CERT` | Client certificate to present (mTLS) |
| `ACTIVITY_MONITOR_CLIENT_KEY` | Client private key (mTLS) |
| `ACTIVITY_MONITOR_SSL_INSECURE` | `1` to skip verification (development only) |

```powershell
$env:ACTIVITY_MONITOR_URL = "https://localhost:8001"
$env:ACTIVITY_MONITOR_CA_CERT = "certs/ca.crt"
$env:ACTIVITY_MONITOR_CLIENT_CERT = "certs/client-alice.crt"
$env:ACTIVITY_MONITOR_CLIENT_KEY = "certs/client-alice.key"
```

The Windows watcher builds an `ssl.SSLContext` from these; the opencode plugins pass them
to `node:https`; the git hooks pass `--cacert` / `--cert` / `--key` to `curl`.

### curl (manual)

**HTTPS only (trusted CA):**
```bash
curl --cacert certs/ca.crt https://localhost:8001/docs
```

**Self-signed/private CA not trusted:** add `-k`/`--insecure` (development only):
```bash
curl -k https://localhost:8001/docs
```

**mTLS:**
```bash
curl --cacert certs/ca.crt \
  --cert certs/client-alice.crt --key certs/client-alice.key \
  https://localhost:8001/docs
```

### Python (httpx)

**mTLS:**
```python
import httpx

client = httpx.Client(verify="certs/ca.crt", cert=("certs/client-alice.crt", "certs/client-alice.key"))
resp = client.get("https://localhost:8001/docs")
```

### Browser

Import `ca.crt` as a trusted root and `client-alice.p12` as a personal/client certificate.
The browser prompts you to select the client certificate when the backend requires mTLS.

## Security notes

- Never commit `.key` files, `.p12` files, or `ca.key` to version control.
- Only commit `.crt` files if they are meant to be shared and safe to do so.
- The `certs/` directory is typically gitignored; keep it local to your machine.
- Rotate certificates before they expire (we used 365/3650 day lifetimes here for dev).
- Revoke client certificates by removing them from trust/rotation if compromised.
