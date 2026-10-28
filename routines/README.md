# Report routines

This directory holds the prompt sources for Claude's scheduled cloud tasks
("routines") that write the health reports. Routines are created
interactively, not as infra-as-code; the prompts are committed so they are
reproducible.

## The four Chinese reports (HTTPS API)

Each routine reads `GET /api/bundle`, writes its report and submits it with
`POST /api/report`; the server saves it as `<REPORTS_DIR>/<date>-<kind>.md`
and pushes it to Telegram / WeChat. They do not need the MCP connector, only
a cloud environment that allows your domain and sets `HEALTH_API_BASE` and
`HEALTH_API_TOKEN` (the `REPORT_API_TOKEN` that `deploy/show-config.sh`
prints). Start every run in a fresh session.

| Prompt | Schedule (your time zone) | Data window | Model |
| --- | --- | --- | --- |
| [`morning-report.zh.prompt.md`](./morning-report.zh.prompt.md) | every hour from 8 to 12, `5 8-12 * * *` | 14 days | Sonnet |
| [`evening-report.zh.prompt.md`](./evening-report.zh.prompt.md) | `55 19 * * *` | 21 days | Sonnet |
| [`weekly-report.zh.prompt.md`](./weekly-report.zh.prompt.md) | Mondays, `50 8 * * 1` | 21 days | Opus |
| [`monthly-report.zh.prompt.md`](./monthly-report.zh.prompt.md) | the 1st, `20 9 1 * *` | 64 days | Opus |

The morning and evening reports mostly put numbers that Python worked out
into words, so Sonnet is enough; the weekly and monthly ones weigh a longer
stretch and last period's suggestions, which is where Opus pays off.

Every prompt first asks `GET /api/report-status?kind=<kind>` and stops when
the report is not due, so a routine may fire more often than it writes:

- The morning report waits for last night's sleep, which the phone uploads
  only once it is unlocked. It runs every hour of `MORNING_REPORT_HOURS`
  (`8-12` by default, in the server's `.env`; change the routine's schedule
  with it) and writes on the first run after the whole night has arrived: the
  sleep-times automation and the metric CSV come in separate uploads, often an
  hour apart. With only one of them in it waits two more hours; with neither,
  it writes at the last hour without the night.
- A report of a kind is posted once per date: a second `POST` answers 409, so
  overlapping runs never push it twice.

Alerts are not a routine: the MCP server checks the data every five minutes
and pushes what it finds on its own (see the main README).

Replace the `## 用户背景` section with the user's own details before pasting
a prompt into a routine, and do not commit the filled-in version. Setup steps
in Chinese: [`docs/SETUP.zh.md`](../docs/SETUP.zh.md).

## The MCP report (English)

[`health-report.prompt.md`](./health-report.prompt.md) is the original daily
HEALTH REPORT: it reads the data through the Apple Health MCP connector and
persists the report to `/opt/health/reports/` via the `save_health_report`
MCP tool. The Fitness Coach Agent on the user's phone reads the latest report
through `get_health_report`.

### Set up the schedule (one time, after deploy)

In any Claude conversation that has the Apple Health MCP connector
enabled, invoke the `/schedule` skill with these settings:

- **Cron expression:** `0 6 * * *` — daily at 06:00 UTC (≈ 07/08:00 CET).
  Choose a different hour if your VPS clock is local.
- **Model:** `claude-opus-4-7` with extended thinking enabled.
- **Connectors enabled:** Apple Health MCP. Add Notion or Gmail later if
  you decide to broadcast the report elsewhere.
- **Prompt body:** start from [`health-report.prompt.md`](./health-report.prompt.md),
  but **fill in the `## User context` block first** with the specifics for
  the user this routine runs for (see the structured comment in the file).
  The committed version intentionally ships with a placeholder so the repo
  contains no PII. Paste the populated version into `/schedule`; do not
  push the populated version back to source control.
- **Description:** "Daily Apple Health triage briefing — sensor-aware
  morning report saved via `save_health_report`."

### Dry-run before scheduling

Before committing to the cron, verify the prompt produces a clean report:

1. Open a fresh Claude conversation that has the Apple Health MCP
   connector.
2. Paste `health-report.prompt.md` as the user message.
3. Confirm the assistant follows the three steps and ends by calling
   `save_health_report` successfully.
4. Inspect the saved file at `/opt/health/reports/<today>.md`.

### Verify from the Fitness Coach

The Fitness Coach Agent (a custom Claude project on your phone with the
Apple Health MCP connector) should be able to:

- Answer *"What does today's report say?"* by calling
  `get_health_report()` (no arguments).
- Answer *"Show me yesterday's report."* by calling
  `get_health_report(date="<yesterday>")`.

### Updating the prompt later

Edit `health-report.prompt.md` and merge to `main`. Then re-run
`/schedule` with the new prompt body — Anthropic Routines treat the
prompt as immutable per scheduled job, so updating means replacing.
