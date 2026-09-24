---
name: migrate-from-grafana
description: Migrate Grafana dashboards and alert rules into Cardinal (Maestro dashboards + lakerunner alert rules). Use whenever someone wants to move, copy, import, port or switch their Grafana (Cloud, OSS or Enterprise) dashboards or alerts to Cardinal / CardinalHQ / lakerunner / Maestro, bring existing Grafana monitoring over when adopting Cardinal, or check what of their Grafana setup would carry over — even if they only say "move my grafana stuff to cardinal". Not for teams starting fresh on Cardinal with nothing in Grafana, and not for moving raw telemetry data.
---

# /cardinal:migrate-from-grafana — migrate Grafana to Cardinal

Moves **dashboards and alert rules** from a Grafana instance into a Cardinal org.
It does not move historical telemetry — Cardinal only shows data that is sent to it,
so the services must already be shipping OTLP to Cardinal (the migration is about
the *views and rules*, which is why metric names get checked against Cardinal's
catalog before anything is converted).

Migration is optional. If the user has no Grafana to migrate from (a fresh start on
Cardinal), say there's nothing to migrate and stop — Cardinal's own dashboard
authoring and `manage_alert_rules` MCP tool are the right path for them.

## What you need from the user

| Item | Why | Notes |
|---|---|---|
| Grafana URL + service account token | read dashboards, alert rules, datasources | **Viewer** role is enough. Administration → Users and access → Service accounts → Add → Viewer → Add token (`glsa_…`). |
| Which dashboards/alerts | scope | a folder, a tag, specific dashboard UIDs, or "everything" |
| Cardinal URL | target | `https://app.cardinalhq.io` or their self-hosted Cardinal UI |
| Cardinal org ID | target org | UUID of the org to migrate into |
| Cardinal credential | write dashboards + alerts | Either the user's **login token** (`CARDINAL_TOKEN`) or an **org API key with `admin:all` scope** (`CARDINAL_API_KEY`, only a Cardinal superadmin can mint one). The login token carries the user's org role: **Member** can create dashboards, **Owner** also alert rules, Viewer neither. The plugin's `/cardinal:connect` key (`mcp:invoke`) cannot write dashboards. |
| Data lake instance | which lakerunner the alerts run on | only if the org has more than one; the default is used otherwise |

Credentials are secrets: have the user put them in env files (below) instead of
pasting them into chat. Create the files for them with empty values, `chmod 600`,
and open them in an editor (`open -e <file>` on macOS). Never echo token values
back — when checking a file, mask them (e.g. `sed -E 's/(TOKEN|KEY)=(.{6}).*/\1=\2…/'`).

```
# .env.grafana-migrate
GRAFANA_URL=https://<stack>.grafana.net
GRAFANA_TOKEN=

# .env.cardinal  (fill ONE of CARDINAL_TOKEN / CARDINAL_API_KEY)
CARDINAL_URL=https://app.cardinalhq.io
CARDINAL_ORG_ID=
CARDINAL_TOKEN=
CARDINAL_API_KEY=
```

To get the login token and org ID: sign in to Cardinal with their own account, switch
to the target org, open browser dev tools → Network, click Dashboards, and pick any
`/api/orgs/<org-id>/...` request — the org ID is in the URL, the token is the value after
`Authorization: Bearer `. It expires in about an hour; a 401 from the scripts means
"copy a fresh one". If the header says `CardinalDemo` instead of `Bearer`, they're in
Cardinal's public demo, which is read-only for everyone — they need a real login.

Keep all migration files in one working directory (e.g. `./grafana-migration/`).
The scripts ship with this skill and need only Python 3.9+ (standard library).
Locate them once and reuse `$SCRIPTS` in every step — portable across plugin and
personal-skill installs:

```bash
SCRIPTS=$(dirname "$(find ~/.claude/plugins ~/.claude/skills . -name convert.py \
  -path '*migrate-from-grafana/scripts*' 2>/dev/null | head -1)")
[ -f "$SCRIPTS/convert.py" ] || { echo "migrate-from-grafana scripts not found"; exit 1; }
```

## Workflow

### 1. Export from Grafana

```bash
python3 $SCRIPTS/grafana_export.py --env-file .env.grafana-migrate --out export \
    [--folder "Team X"] [--tag prod] [--uid <dash-uid>]
```

Writes `export/dashboards/*.json`, `export/alerts.json`, `export/datasources.json`.
Show the user the list of dashboards and the alert count and confirm the scope
before continuing — migrating the wrong folder is the most common mistake. If alert
export fails (older Grafana, or no permission), continue with dashboards and say so.

### 2. Read Cardinal's catalog and build the name mapping

```bash
python3 $SCRIPTS/cardinal_catalog.py --env-file .env.cardinal --export export --out catalog \
    [--instance <slug>]
```

This lists Cardinal's real metric and label names and writes
`catalog/mapping.suggested.json`: for every metric the Grafana queries use, the
Cardinal name it most likely corresponds to (Prometheus-style names in Grafana
carry `_total` / unit suffixes that lakerunner may not). It also probes whether
this Cardinal instance supports `histogram_quantile`.

Then **review the mapping** — this is where migrations go wrong silently, because a
wrong name produces a dashboard that renders but shows "No data":

- Look at every entry in `_review`. For `no Cardinal metric matched`, check
  `similar_in_cardinal` and `catalog/metrics.json`; set the right name, or `null` if
  the metric genuinely isn't sent to Cardinal (those panels/rules get skipped and
  reported, which is better than an empty panel).
- For renamed metrics, sanity-check they're the same thing (same service, same unit).
- If a metric is missing only because the service isn't sending to Cardinal yet,
  tell the user — that's a data-onboarding gap, not a migration problem.
- Copy the reviewed result to `mapping.json` (drop the `_review` key).

Also check `native_histograms` (Cardinal stores these histograms under the base name
only — no `_bucket/_sum/_count`; the converter rewrites the queries) and `drop_labels`
(labels or exact filter values the Grafana queries use that don't exist in Cardinal,
e.g. an environment label whose value differs between the two pipelines; those
filters are removed). Confirm with the user that removing a filter is right — it
widens what the panel shows.

If the catalog comes back empty, the org isn't receiving data yet; stop and say so.

### 3. Convert (offline)

```bash
python3 $SCRIPTS/convert.py --export export --mapping mapping.json --out plan
```

Produces `plan/dashboards/*.json` (Cardinal dashboard specs), `plan/alerts.json`
(lakerunner rule specs) and `plan/report.json` (every panel and rule marked
`migrated`, `adapted` or `skipped`, with the reason). See
`references/translation-rules.md` for exactly what gets converted, adapted or
dropped — read it when a result looks surprising or the user asks why something
changed.

Summarise the report for the user before writing anything: counts per status, and
every **adapted** item whose meaning changed (the important one: `histogram_quantile`
p95/p99 panels become *averages* when Cardinal doesn't support quantiles — the title
still says "p95", so call these out and offer to rename them) and every **skipped**
item with its reason.

### 4. Dry run, then apply

```bash
python3 $SCRIPTS/cardinal_apply.py --env-file .env.cardinal --plan plan --catalog catalog
python3 $SCRIPTS/cardinal_apply.py --env-file .env.cardinal --plan plan --catalog catalog --apply
```

The dry run shows what would be created vs updated (same-name objects are updated in
place, so re-running is safe). Get an explicit go-ahead before `--apply`: it writes
into the customer's Cardinal org, and alert rules start evaluating and can notify
people immediately. If the org already has objects with the same names that are not
from a previous migration run, use `--name-prefix "Grafana - "` to avoid overwriting
them. Use `--only dashboards` / `--only alerts` to do one side.

A 403 on dashboards means the credential can't write (Viewer role, or an API key without `admin:all`); a 403 on alerts with a login token means the user is a Member, not Owner; a 422 on alerts
saying *"Alerting is not connected for this integration (status='disabled')"* means
alerting is switched off for that data-lake instance — an org admin has to enable it
(it's not a migration error); dashboards can still be migrated with `--only dashboards`.
Report such messages verbatim and don't retry blindly.

Login tokens expire after roughly an hour, and a migration can outlast one. Run the
Cardinal steps back to back once the token is fresh, and if a script says the token
expired, ask for a new one and resume from that step — every step is re-runnable.

### 5. Verify

```bash
python3 $SCRIPTS/cardinal_verify.py --env-file .env.cardinal --plan plan --catalog catalog
```

Runs every migrated panel and alert query through Cardinal's query API (variables
set to "All") and prints, per dashboard, how many queries return data. An **ERROR**
is a translation problem — fix the query or mapping, re-run convert + apply (it
updates in place) and verify again. **No data** while Grafana has data usually means
a wrong metric/label mapping or data that isn't flowing to Cardinal.

Note: Cardinal's query API is not the Prometheus HTTP API. Queries go to
`POST /api/lakerunner/<instance>/query/{metrics|logs}/query` with `{q, s, e, step}`
(times in ms, as strings) and stream back as SSE; `cardinal_catalog.NativeQuery`
wraps this if you need to test a query by hand.

### 6. Report

Give the user a short migration report:

- Dashboards: name → Cardinal link (`{CARDINAL_URL}/dashboards/{id}`), panels migrated/adapted/skipped
- Alerts: name → created/updated, with any meaning changes
- Everything skipped, and why
- Panels that returned no data in verification
- Follow-ups: rotate/delete the Grafana token if it was created for this; alert
  notification routing (Grafana contact points / policies do **not** carry over —
  the user sets up Cardinal notification groups, then attaches them to the rules)

Offer to write the report into a file (`migration-report.md`) in the working directory.
