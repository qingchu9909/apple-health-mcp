# Apple Health MCP

A self-hosted pipeline that pushes Apple Health data from your iPhone to a VPS
and serves it to [Claude](https://claude.ai) (or any MCP client) over a remote
[Model Context Protocol](https://modelcontextprotocol.io/) server.

```
iPhone (Health Auto Export, Premium)
        │  HTTPS POST · daily · "Since Last Sync" · CSV
        ▼
https://health.example.com/ingest          (FastAPI)
        │  writes /opt/health/data/health_YYYYMMDD_HHMMSS.csv
        ▼
https://mcp.example.com/mcp/<TOKEN>        (FastMCP, Streamable HTTP)
        │
        ▼
Claude (web · Desktop · iOS)
```

Once deployed, Claude can answer questions like *"how did my HRV trend last
week?"* or *"summarize my sleep over the past 14 days"* against your real
Apple Health data — without uploading anything to a third-party service.

> **About this fork.** It adds a one-command installer (`deploy/install.sh`,
> one domain, Caddy or Nginx, including Nginx in Docker, systemd), workouts, push notifications
> (Telegram, Server酱, PushPlus), an HTTPS API that Claude's scheduled cloud
> routines can use for a daily report, time-zone handling, separate tokens
> for upload / connector / report API, and fixes for duplicate exports and
> unauthenticated uploads. Chinese setup guide: [docs/SETUP.zh.md](docs/SETUP.zh.md);
> Chinese daily-report routine: [routines/daily-report.zh.prompt.md](routines/daily-report.zh.prompt.md).

---

## Features

Fifteen MCP tools — four return compact JSON daily records, two cover
workouts, two read/write a persisted daily HEALTH REPORT, one pushes a
message to your phone, and six raw tools remain as the escape hatch.

**Structured (one record per day per domain, JSON):**

| Tool | Description | Default window |
| --- | --- | --- |
| `get_daily_sleep` | Sleep stages + wrist temperature, one record per night; bedtime and wake time when the sleep automation is set up. | 14 days |
| `get_daily_fitness` | Steps, distance, energy, exercise/stand time, flights, walking speed, walking HR, VO2 max — aggregated per day. | 14 days |
| `get_daily_vitals` | Heart-rate min/max/avg, resting HR, HRV, respiratory rate, blood-oxygen — aggregated per day — plus an overnight CGM glucose profile (mean/min/max, % time in 3.9–7.8 mmol/L, sample count over 22:00–08:00, keyed to the wake date). | 14 days |
| `get_baselines` | p10/p50/p90 + yesterday + 7d-vs-30d trend for every headline metric. | 30 days |

**Workouts, ECG and heart rate notifications** (JSON automations):

| Tool | Description | Default window |
| --- | --- | --- |
| `get_workouts` | One compact record per workout: type, start/end, duration, distance, energy, heart rate, elevation, conditions. | 14 days |
| `get_workout_detail` | Minute-by-minute heart rate, heart-rate recovery, steps and metadata for one workout. | n/a |
| `get_ecg` | ECG recordings: classification (sinus rhythm, AFib, inconclusive…), average heart rate, severity. | 90 days |
| `get_heart_rate_notifications` | High, low and irregular-rhythm notifications with the threshold and the heart rate / HRV around them. | 30 days |

**Push:**

| Tool | Description |
| --- | --- |
| `send_message` | Sends a message to every configured channel (Telegram, Server酱, PushPlus). |

**Daily HEALTH REPORT (read/write):**

| Tool | Description |
| --- | --- |
| `save_health_report` | Persist a markdown report at `<REPORTS_DIR>/<YYYY-MM-DD>.md`. Used by the scheduled report-generation routine. |
| `get_health_report` | Read a report by date, or — with no `date` arg — the most recent report within `max_age_days`. The Fitness Coach Agent uses this to fetch each morning's briefing. |

See [`routines/README.md`](routines/README.md) for how to set up the
scheduled remote agent that calls `save_health_report` every morning.

**Raw / escape hatch:**

| Tool | Description | Default window |
| --- | --- | --- |
| `list_metrics` | Lists every health metric with at least one data point. | n/a |
| `get_latest` | Most recent non-empty snapshot across all metrics. | n/a |
| `get_metric` | Time series for a single metric. | 30 days |
| `get_summary` | avg/min/max/last for the headline metrics. | 7 days |
| `get_sleep` | Raw sleep + wrist-temp rows. | 14 days |
| `get_fitness` | Raw fitness + HR rows. | 14 days |

The CSV exports are daily aggregates with 100+ columns, so any of those
columns can be queried via `get_metric` even if no dedicated tool wraps them.

**HTTP API for scheduled routines.** Claude's scheduled cloud routines cannot
attach a custom connector, so the MCP server also exposes two plain HTTPS
routes, guarded by `Authorization: Bearer $REPORT_API_TOKEN`:

| Route | Description |
| --- | --- |
| `GET /api/bundle?days=14` | Everything a morning briefing needs in one JSON document: freshness, baselines, daily sleep / vitals / fitness, workouts, other metrics, last week's report. |
| `POST /api/report` | `{"content", "title"?, "date"?, "push"?}` — saves the report like `save_health_report` and pushes it to the configured channels. |

---

## Repository layout

```
apple-health-mcp/
├── ingest/main.py             FastAPI ingest service (port 8001)
├── mcp/server.py              FastMCP MCP server     (port 8002)
├── mcp/notify.py              Telegram / Server酱 / PushPlus delivery
├── requirements.txt           Pinned Python dependencies
├── .env.example               Tokens, paths, time zone and push channels
├── deploy/
│   ├── install.sh             One-command installer (Debian/Ubuntu, run as root)
│   ├── update.sh              Pull the latest code and re-run the installer
│   ├── show-config.sh         Print the values for the phone, connector and routine
│   ├── status.sh              Recent uploads from the phone and the data on disk
│   ├── systemd/               Units used by install.sh
│   ├── caddy/Caddyfile.example                 Single-domain Caddy site
│   ├── nginx/health.single-domain.example.conf Single-domain Nginx site
│   ├── nginx/health.docker.example.conf        Same, for Nginx in Docker (kejilion.sh LDNMP)
│   ├── nginx/{ingest,mcp}.example.conf         Original two-subdomain templates
│   └── supervisor/            Original process supervisor units
├── docs/SETUP.zh.md           Chinese step-by-step setup guide
├── routines/
│   ├── health-report.prompt.md   Prompt source for the daily-report routine (MCP)
│   ├── daily-report.zh.prompt.md Chinese routine prompt using the HTTPS API
│   └── README.md                 How to schedule the routine via /schedule
└── tests/
    ├── test_loader.py
    ├── test_smart_tools.py
    └── test_report_tools.py
```

Both Python services share a single virtualenv in production; the file layout
on disk just mirrors the directories above.

---

## Prerequisites

- A VPS with a public IP, root or sudo access, Python 3.12+ available.
- A domain with two A records: one for the ingest endpoint, one for the MCP
  endpoint (e.g. `health.example.com`, `mcp.example.com`).
- TLS certificates for both subdomains (Let's Encrypt via `certbot` or your
  control panel works fine).
- An iPhone running the **[Health Auto Export][hae]** app, **Premium** tier
  (Premium is required for unattended REST API uploads).
- A [Claude](https://claude.ai) account on a plan that supports custom
  connectors (paid plans).

[hae]: https://apps.apple.com/app/id1115567069

---

## Quick start

On a Debian or Ubuntu VPS, as root, with a domain whose A record points at it:

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/apple-health-mcp/main/deploy/install.sh \
  | DOMAIN=health.example.com USER_TZ=Europe/London TELEGRAM_BOT_TOKEN=123:abc bash
```

It installs everything under `/opt/health`, configures HTTPS (Caddy when port 443
is free, leaving port 80 alone; otherwise an existing Caddy or Nginx, or an Nginx
container with host networking such as the kejilion.sh LDNMP stack, where it adds
a site to the container's `conf.d` and gets the certificate with acme.sh), checks every endpoint,
and sends the values to enter on the phone and in Claude to your push channel.
Re-running it is safe; `deploy/update.sh` updates. Settings are documented at
the top of the script; the steps on the phone and in Claude are in
[docs/SETUP.zh.md](docs/SETUP.zh.md).

## Manual install (reference)

The reference deployment uses Ubuntu 22.04 with [aaPanel] for Nginx + Supervisor.
Adapt freely if you prefer plain `systemd` and hand-rolled Nginx.

[aaPanel]: https://www.aapanel.com/

### 1. Lay out files on the VPS

```bash
sudo mkdir -p /opt/health/{data,ingest,mcp,reports}
sudo chown -R www:www /opt/health   # match the user your supervisor unit runs as

# Copy the source files
sudo cp ingest/main.py  /opt/health/ingest/main.py
sudo cp mcp/server.py   /opt/health/mcp/server.py

# Create the shared .env (chmod 600 — it holds the bearer token)
sudo cp .env.example /opt/health/.env
sudo chmod 600 /opt/health/.env
sudo chown www:www  /opt/health/.env
```

Edit `/opt/health/.env` and put in a real token:

```bash
# Generate a 64-char hex token
openssl rand -hex 32
```

### 2. Create the shared virtualenv

```bash
sudo -u www python3.12 -m venv /opt/health/ingest/venv
sudo -u www /opt/health/ingest/venv/bin/pip install -r requirements.txt

# Both services share one venv
sudo ln -s /opt/health/ingest/venv /opt/health/mcp/venv
```

### 3. Configure Supervisor

Copy the unit files and reload:

```bash
sudo cp deploy/supervisor/health-ingest.conf /etc/supervisor/conf.d/
sudo cp deploy/supervisor/health-mcp.conf    /etc/supervisor/conf.d/
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status
```

> **aaPanel users:** the bundled Supervisor binary lives at
> `/www/server/panel/pyenv/bin/supervisorctl` rather than the system path.

Both processes should be in `RUNNING` state and the logs under
`/opt/health/{ingest,mcp}/{access,error}.log` should be quiet.

### 4. Configure Nginx

Copy the templates, replace `example.com` with your actual hostnames, point
`ssl_certificate*` at your real cert files, then reload Nginx:

```bash
sudo cp deploy/nginx/ingest.example.conf /etc/nginx/sites-enabled/health.example.com.conf
sudo cp deploy/nginx/mcp.example.conf    /etc/nginx/sites-enabled/mcp.example.com.conf
# ...edit the two files...
sudo nginx -t && sudo nginx -s reload
```

> **HTTP/2 caveat:** keep the MCP server block on HTTP/1.1. The Streamable-HTTP
> handshake in `mcp` 1.27 returns *421 Misdirected Request* on some HTTP/2
> paths; running the MCP listener as plain `listen 443 ssl` (no `http2`) on
> its own subdomain is the simplest fix. The ingest listener can use HTTP/2
> normally.

Smoke-test:

```bash
curl https://health.example.com/health
# {"status":"ok"}

curl -X POST "https://mcp.example.com/mcp/$TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

The second call should return a JSON list of the six tools.

### 5. iPhone — Health Auto Export

Run the app in English. It names the CSV columns in its own language and the
server only reads the English names, so on a phone set to another language pick
English under **Settings → Apps → Health Auto Export → Language**.

Open the app, go to **Automations**, and add a REST API automation:

| Field | Value |
| --- | --- |
| URL | `https://health.example.com/ingest` |
| HTTP method | `POST` |
| Authorization header | `Bearer <your token>` |
| Format | `CSV` |
| Date range | `Default` (yesterday in full plus today so far; overlapping uploads are merged, newest value wins) |
| Sync cadence | `1 hour` |
| Health metrics | `All Selected` |
| Summarize Data | `ON` |

Add a second automation with the same URL and header for **Workouts**:
format `JSON`, export version `2`, *Include Workout Metrics* on, *Include
Route Data* off, time grouping `Minutes`, date range `Default`.

Optionally add a third automation for **Health Metrics** with only *Sleep
Analysis* selected, format `JSON`, export version `2`, *Summarize Data* on,
time grouping `Days`, date range `Default`: the CSV export only has sleep
durations, the JSON one also says when each night began and ended (`bedtime`
/ `wake_time` in `get_daily_sleep`). Each automation carries one data type,
so **ECG** and **Heart Rate Notifications** take one JSON automation each
(`get_ecg`, `get_heart_rate_notifications`; ECG voltage samples are dropped).

Tap **Run Now** once to push your historical data. The first run can be a few
MB and may take a few seconds.

### 6. Claude — add the connector

In Claude (web or iOS): **Settings → Connectors → Add custom connector**

| Field | Value |
| --- | --- |
| Name | `Apple Health MCP` |
| URL | `https://mcp.example.com/mcp/<MCP_PATH_TOKEN>` (falls back to `HEALTH_TOKEN` when unset) |
| Authentication | None *(token is in the URL path)* |
| Transport | Streamable HTTP |

The connector should resolve and show the six tools.

---

## How the bits fit together

### Why a multipart envelope strip?

Health Auto Export sends CSV uploads as `multipart/form-data` with one part,
not as a raw body. The ingest service detects the `--Boundary…` prefix, slices
out the inner CSV, and writes only that to disk. See `ingest/main.py`.

### Why is the MCP server tolerant of multipart leftovers?

Older deployments that didn't strip the envelope produced CSV files starting
with `--Boundary…`. `mcp/server.py` defends against that case so historical
files keep working alongside newly cleaned ones. See `_read_csv_tolerant`.

### Why is the bearer token in the URL?

Claude's connector UI does not currently let you set custom HTTP headers, so
the only place to put a shared secret is the URL path. The MCP server reads
`HEALTH_TOKEN` and mounts the streamable endpoint at `/mcp/<TOKEN>`. Treat
that URL exactly like a password — anyone who has it can read your health
data. Rotate it by updating `/opt/health/.env`, restarting `health-mcp`, and
updating the URL in both Health Auto Export and the Claude connector.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401 Unauthorized` from `/ingest` | Wrong or missing Authorization header. | `bash /opt/health/app/deploy/status.sh` lists recent uploads and why each refused one failed. |
| `421 Misdirected Request` from MCP | HTTP/2 enabled on the MCP vhost. | Remove `http2` from `listen 443 ssl` for the MCP server block. |
| `Missing column ... 'Date/Time'` | A CSV is wrapped in a multipart envelope. | Already handled by `_read_csv_tolerant`; if you see it, check the file is actually CSV-shaped. |
| Tools return *"No health data available yet."* | No CSVs in `DATA_DIR`. | Confirm Health Auto Export has run successfully. |
| Tools return *"No readable health data"*, or only workouts show up | Health Auto Export named the CSV columns in another language (e.g. `日期/時間` instead of `Date/Time`). | Set the app's language to English (see step 5) and export the missing days again. `status.sh` counts the unreadable files. |
| Connector shows zero tools | Server unreachable, wrong path, or DNS-rebinding rejection. | Check `error.log`; confirm `mcp.settings.transport_security.enable_dns_rebinding_protection = False`. |

Useful commands:

```bash
sudo supervisorctl status
sudo tail -50 /opt/health/mcp/error.log
sudo tail -50 /opt/health/ingest/error.log
ls -lh /opt/health/data/
```

---

## Roadmap

Things on the wishlist (PRs welcome):

- **Loader cache** so `load_all_csv` does not re-read every file on every tool
  call. Cache key on the directory's modification time.
- **Storage rollup** — append each ingested CSV into a single Parquet (or
  DuckDB) file and dedupe by `Date/Time` at write time.
- **More tools** — trends/regressions, period-over-period comparisons, nutrition,
  workouts, anomaly detection, recovery score.
- **Health check** on the MCP server (`GET /healthz`) plus a freshness alert
  when no CSV has arrived in 48 hours.
- **Structured logging** for both services with a request id.

---

## Security notes

- `/opt/health/.env` is the only place the live token should ever appear; keep
  it `chmod 600` and owned by the service user.
- The bearer token doubles as the URL secret on the MCP side. Rotating it is a
  single-step operation: update the env file, restart `health-mcp`, update the
  iPhone automation and Claude connector.
- The ingest endpoint accepts HTTPS posts from anywhere; consider rate-limiting
  it (e.g. with `limit_req` in Nginx) if you expose the URL widely.
- This software does not implement consent management, audit logging, or HIPAA
  controls. Self-host responsibly; do not run it for anyone but yourself
  without first reading the `LICENSE` file.

---

## License

MIT — see [`LICENSE`](LICENSE).
