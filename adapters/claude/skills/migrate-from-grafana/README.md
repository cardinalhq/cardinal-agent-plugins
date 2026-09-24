# migrate-from-grafana

Moves your Grafana dashboards and alert rules into Cardinal. Claude does the
work; you provide credentials and approve each step. See SKILL.md for the flow
Claude follows.

## Before you start

- **Your services already send telemetry to Cardinal.** This migrates
  *dashboards and alert rules*, not historical data. A dashboard for a service
  that isn't sending data to Cardinal will be empty.
- **Claude Code** is installed, with the **Cardinal plugin** (install
  instructions: [cardinal-claude-plugin](https://github.com/cardinalhq/cardinal-claude-plugin)).
- **Python 3.9+** is available (`python3 --version`). Nothing else to install.

## 1. Get your credentials ready

**Grafana:** a read-only token.
Administration → Users and access → Service accounts → **Add service account**
(role: **Viewer**) → **Add token**. Copy the `glsa_…` value.

**Cardinal:** one of these.

- **Your login token** (easiest). Sign in to Cardinal and switch to the target
  org. Open browser dev tools → Network, click **Dashboards**, and pick any
  request to `/api/orgs/<org-id>/…`:
  - the `<org-id>` in the URL is your **org ID**
  - the value after `Authorization: Bearer ` is your **token** (expires in
    about an hour)
  - you need the **Member** role for dashboards, **Owner** to also migrate alerts
- **An org API key with `admin:all` scope.** Ask your Cardinal admin for one.
  It doesn't expire mid-migration.

Also decide **what** to migrate: a Grafana folder, a tag, specific dashboards,
or everything.

## 2. Start the migration

```bash
mkdir grafana-migration && cd grafana-migration
claude
```

Then type `/cardinal:migrate-from-grafana`, or just ask:
*"Migrate my Grafana dashboards and alerts in the 'Production' folder to Cardinal."*

## 3. Fill in the files Claude creates

Claude creates two files and opens them in your editor:

```
.env.grafana-migrate   → GRAFANA_URL, GRAFANA_TOKEN
.env.cardinal          → CARDINAL_URL, CARDINAL_ORG_ID, CARDINAL_TOKEN (or CARDINAL_API_KEY)
```

Fill them in and save. Don't paste tokens into the chat.

## 4. Answer Claude's questions as it works

| Step | Claude does | You do |
|---|---|---|
| Export | Pulls dashboards and alerts from Grafana, lists them | Confirm it's the right set |
| Name mapping | Matches Grafana metric names to Cardinal's (they often differ, e.g. `_total` suffixes) | Confirm or correct names it isn't sure about |
| Convert | Converts offline; reports what was migrated as-is, adapted, or skipped | Review changes, e.g. p95/p99 panels that become averages |
| Dry run | Shows exactly what it will create or update in Cardinal | Say **"go ahead"**. Nothing is written until you do. |
| Apply | Creates the dashboards and alert rules | — |
| Verify | Runs every migrated query and checks it returns data | Look at any panel flagged "no data" |

## 5. Get your report

Claude gives you links to each new Cardinal dashboard, the status of each alert
rule, everything skipped and why, and follow-ups. It can save this as
`migration-report.md`.

## 6. After the migration

- **Set up notifications.** Grafana contact points and notification policies
  don't carry over. Create notification groups in Cardinal and attach them to
  the migrated alert rules.
- **Delete the Grafana service account token** you created for the migration.
- Run Grafana and Cardinal side by side for a while and compare before
  switching Grafana off.

## Troubleshooting

| You see | Meaning |
|---|---|
| `401` / "token expired" | Cardinal login token expired. Copy a fresh one into `.env.cardinal` and tell Claude to continue; every step is safe to re-run. |
| `403` on dashboards | Your Cardinal role is Viewer, or the API key isn't `admin:all` |
| `403` on alerts | You're a Member; alerts need **Owner** |
| "Alerting is not connected…" | Alerting is turned off for your Cardinal data lake; ask your admin to turn it on. Dashboards can still be migrated. |
| Panels show "No data" | The metric isn't reaching Cardinal yet, or its name was mapped wrong |
