# bigip-urldb-sync

Push custom URL lists to F5 BIG-IP URLDB categories via the iControl REST API.

Reads a JSON file (local or from a remote HTTP/HTTPS endpoint) containing a
list of URLs and upserts them into one or more BIG-IP `url-category` objects
under `/mgmt/tm/sys/url-db/url-category`.

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Installation](#installation)
3. [Configuration (.env)](#configuration-env)
4. [JSON Input Formats](#json-input-formats)
5. [CLI Usage](#cli-usage)
6. [FastAPI Service](#fastapi-service)
7. [Systemd Install](#systemd-install)
8. [BIG-IP Token Auth Notes](#bigip-token-auth-notes)
9. [SSL Verification Notes](#ssl-verification-notes)

---

## Project Structure

```
bigip-urldb-sync/
├── core/
│   ├── __init__.py
│   ├── loader.py        # JSON source loading (file or HTTP)
│   ├── transformer.py   # Transform URL list into iControl payloads
│   └── bigip.py         # iControl REST client (auth, upsert, update)
├── cli.py               # Click-based CLI entrypoint
├── api.py               # FastAPI web service
├── config.py            # Settings via pydantic-settings + .env
├── status.py            # Read/write last-run status JSON file
├── systemd/
│   ├── urldb-sync.service
│   └── urldb-sync.timer
├── .env.example
├── requirements.txt
└── README.md
```

---

## Installation

### 1. Clone and set up a virtual environment

```bash
git clone https://github.com/your-org/bigip-urldb-sync.git
cd bigip-urldb-sync

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure the environment

```bash
cp .env.example .env
# Edit .env with your BIG-IP credentials and settings
$EDITOR .env
```

---

## Configuration (.env)

All settings are read from environment variables or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `BIGIP_HOST` | *(required)* | BIG-IP management IP or hostname |
| `BIGIP_USER` | `admin` | BIG-IP username |
| `BIGIP_PASSWORD` | *(required)* | BIG-IP password |
| `BIGIP_PARTITION` | `Common` | BIG-IP partition |
| `BIGIP_VERIFY_SSL` | `false` | Verify management TLS certificate |
| `URLDB_CATEGORY` | `custom_block_list` | URLDB category name |
| `URLDB_URL_TYPE` | `exact` | Default URL match type (`exact`, `glob`, `any`) |
| `URLDB_CHUNK_SIZE` | `9000` | Max URLs per category object |
| `JSON_SOURCE` | *(none)* | Local file path or HTTP/HTTPS URL |
| `STATUS_FILE_PATH` | `/var/log/urldb-sync/status.json` | Status file location |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## JSON Input Formats

### Simple format

```json
{
  "urls": [
    "https://malware.example.com",
    "http://phishing.example.net",
    "https://c2.example.org"
  ]
}
```

### Extended format

```json
{
  "urls": [
    { "url": "https://malware.example.com", "type": "exact" },
    { "url": "*.phishing.example.net",      "type": "glob"  },
    { "url": "https://c2.example.org",      "type": "any"   }
  ]
}
```

Both formats can be mixed in the same file. Entries without a `type` field
inherit the default type configured via `--url-type` / `URLDB_URL_TYPE`.

---

## CLI Usage

### `sync` — push a URL list to BIG-IP

```bash
# Minimal — reads BIGIP_PASSWORD from .env or prompts interactively
python cli.py sync \
    --source /etc/urldb-sync/urls.json \
    --host 192.0.2.1

# Full options
python cli.py sync \
    --source https://feeds.example.com/urldb/urls.json \
    --host 192.0.2.1 \
    --user admin \
    --password "$BIGIP_PASSWORD" \
    --category custom_block_list \
    --url-type exact \
    --partition Common \
    --chunk-size 9000 \
    --log-level DEBUG

# Dry run — parse and transform only, no push to BIG-IP
python cli.py sync \
    --source /etc/urldb-sync/urls.json \
    --host 192.0.2.1 \
    --dry-run

# Enable SSL verification (recommended in production)
python cli.py sync \
    --source /etc/urldb-sync/urls.json \
    --host bigip.corp.example.com \
    --verify-ssl
```

### `status` — view last sync result

```bash
# Human-readable output
python cli.py status

# Raw JSON output
python cli.py status --json
```

### Help

```bash
python cli.py --help
python cli.py sync --help
python cli.py status --help
```

---

## FastAPI Service

### Start the service

```bash
uvicorn api:app --host 0.0.0.0 --port 8080
```

The interactive API docs are available at `http://localhost:8080/docs`.

### Endpoints

#### `GET /health` — liveness check

```bash
curl http://localhost:8080/health
# {"status":"ok","service":"bigip-urldb-sync"}
```

#### `GET /status` — last sync result

```bash
curl http://localhost:8080/status
```

```json
{
  "last_run": "2024-01-15T10:30:00Z",
  "status": "success",
  "urls_pushed": 1234,
  "categories_updated": ["custom_block_list"],
  "error_message": null,
  "source": "https://feeds.example.com/urls.json",
  "bigip_host": "192.0.2.1"
}
```

#### `POST /push-urldb` — trigger a sync

```bash
curl -X POST http://localhost:8080/push-urldb \
     -H 'Content-Type: application/json' \
     -d '{
           "source": "https://feeds.example.com/urls.json",
           "host": "192.0.2.1",
           "user": "admin",
           "password": "supersecret",
           "category": "custom_block_list",
           "url_type": "exact",
           "partition": "Common",
           "chunk_size": 9000,
           "verify_ssl": false,
           "dry_run": false
         }'
```

```json
{
  "status": "success",
  "urls_pushed": 1234,
  "categories_updated": ["custom_block_list"],
  "dry_run": false,
  "message": "Sync complete. 1234 URLs pushed to 1 category object(s) on 192.0.2.1.",
  "error_message": null
}
```

#### `POST /schedule/update` — update the sync interval

Writes a new interval to `/etc/urldb-sync/sync_interval.conf`.
The systemd timer picks it up on the next execution cycle.

```bash
curl -X POST http://localhost:8080/schedule/update \
     -H 'Content-Type: application/json' \
     -d '{"interval_minutes": 30}'
```

```json
{
  "interval_minutes": 30,
  "conf_path": "/etc/urldb-sync/sync_interval.conf",
  "message": "Interval set to 30 minute(s). The systemd timer will use this value on its next cycle."
}
```

---

## Systemd Install

### 1. Create the service user and directories

```bash
sudo useradd -r -s /sbin/nologin urldb-sync
sudo install -d -o urldb-sync -g urldb-sync -m 750 /var/log/urldb-sync
sudo install -d -o root -g urldb-sync -m 750 /opt/urldb-sync
sudo install -d -o root -g root -m 755 /etc/urldb-sync
```

### 2. Deploy the application

```bash
sudo cp -r . /opt/urldb-sync/
sudo chown -R root:urldb-sync /opt/urldb-sync
sudo chmod -R o-rwx /opt/urldb-sync

# Set up virtual environment
cd /opt/urldb-sync
sudo -u urldb-sync python3 -m venv venv
sudo -u urldb-sync venv/bin/pip install -r requirements.txt

# Place the .env file
sudo install -o root -g urldb-sync -m 640 .env.example /opt/urldb-sync/.env
sudo $EDITOR /opt/urldb-sync/.env   # fill in credentials
```

### 3. Install the systemd units

```bash
sudo cp systemd/urldb-sync.service /etc/systemd/system/
sudo cp systemd/urldb-sync.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now urldb-sync.timer
```

### 4. Verify

```bash
# Check timer status
systemctl status urldb-sync.timer

# List all timers including next run time
systemctl list-timers urldb-sync.timer

# View logs
journalctl -u urldb-sync.service -f
```

### 5. Adjust the sync interval

Option A — via the REST API:

```bash
curl -X POST http://localhost:8080/schedule/update \
     -H 'Content-Type: application/json' \
     -d '{"interval_minutes": 30}'
sudo systemctl daemon-reload && sudo systemctl restart urldb-sync.timer
```

Option B — manually:

```bash
echo "30" | sudo tee /etc/urldb-sync/sync_interval.conf
sudo systemctl daemon-reload && sudo systemctl restart urldb-sync.timer
```

---

## BIG-IP Token Auth Notes

bigip-urldb-sync uses F5's token-based iControl REST authentication:

1. On first use (or when the cached token approaches expiry), the client
   `POST`s credentials to `/mgmt/shared/authn/login`.
2. BIG-IP returns a token valid for **1200 seconds** (20 minutes).
3. The token is cached in memory and refreshed automatically 60 seconds
   before expiry — no repeated credential round-trips for short-lived syncs.
4. The `X-F5-Auth-Token` header is added to every subsequent API request.
5. On a `401` response mid-session, the client re-authenticates once and
   retries the request transparently.

**Credential security tips:**
- Store `BIGIP_PASSWORD` in the `.env` file with mode `640` and owned by `root:urldb-sync`.
- Rotate BIG-IP credentials regularly and update the `.env` file accordingly.
- Consider using a BIG-IP local user with the minimum required role
  (`Certificate Manager` or a custom role scoped to `/mgmt/tm/sys/url-db`).

---

## SSL Verification Notes

BIG-IP ships with a **self-signed** management certificate. By default,
`BIGIP_VERIFY_SSL=false` is set, which disables certificate verification and
logs a warning:

```
WARNING — SSL certificate verification is DISABLED for BIG-IP host '192.0.2.1'. ...
```

### Enabling verification (recommended for production / federal environments)

1. Export the BIG-IP management certificate:

   ```bash
   openssl s_client -connect 192.0.2.1:443 -showcerts </dev/null 2>/dev/null \
       | openssl x509 -outform PEM > bigip-mgmt.pem
   ```

2. Install it into the system CA bundle or specify it directly:

   ```bash
   # Option A — system CA bundle (Debian/Ubuntu)
   sudo cp bigip-mgmt.pem /usr/local/share/ca-certificates/bigip-mgmt.crt
   sudo update-ca-certificates
   BIGIP_VERIFY_SSL=true

   # Option B — pass the CA file path
   BIGIP_VERIFY_SSL=/path/to/bigip-mgmt.pem  # pydantic-settings reads this as a string
   ```

3. In environments where a trusted CA-signed certificate is installed on the
   BIG-IP management interface, simply set `BIGIP_VERIFY_SSL=true`.

> **Federal / high-assurance environments:** Always enable SSL verification
> and use CA-signed certificates on BIG-IP management interfaces.  Consult
> your organization's PKI team for the appropriate CA bundle.
