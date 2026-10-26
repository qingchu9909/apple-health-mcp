# Daily Health Report routine

This directory holds the prompt source for an Anthropic-hosted *scheduled
remote agent* (a "routine") that generates a daily HEALTH REPORT and
persists it to `/opt/health/reports/` via the `save_health_report` MCP
tool. The Fitness Coach Agent on the user's phone reads the latest report
through `get_health_report`.

For a report that is pushed to your phone (Telegram / WeChat) and does not
depend on the connector being attached to the routine, use
[`daily-report.zh.prompt.md`](./daily-report.zh.prompt.md) instead: it reads
`GET /api/bundle` and submits `POST /api/report` over HTTPS with
`HEALTH_API_BASE` / `HEALTH_API_TOKEN` from the routine's cloud environment.
Setup steps are in [`docs/SETUP.zh.md`](../docs/SETUP.zh.md).

The routine itself is **not infra-as-code** — Anthropic Routines are
created interactively. We commit the prompt source so the routine is
reproducible.

## Set up the schedule (one time, after deploy)

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

## Dry-run before scheduling

Before committing to the cron, verify the prompt produces a clean report:

1. Open a fresh Claude conversation that has the Apple Health MCP
   connector.
2. Paste `health-report.prompt.md` as the user message.
3. Confirm the assistant follows the three steps and ends by calling
   `save_health_report` successfully.
4. Inspect the saved file at `/opt/health/reports/<today>.md`.

## Verify from the Fitness Coach

The Fitness Coach Agent (a custom Claude project on your phone with the
Apple Health MCP connector) should be able to:

- Answer *"What does today's report say?"* by calling
  `get_health_report()` (no arguments).
- Answer *"Show me yesterday's report."* by calling
  `get_health_report(date="<yesterday>")`.

## Updating the prompt later

Edit `health-report.prompt.md` and merge to `main`. Then re-run
`/schedule` with the new prompt body — Anthropic Routines treat the
prompt as immutable per scheduled job, so updating means replacing.
