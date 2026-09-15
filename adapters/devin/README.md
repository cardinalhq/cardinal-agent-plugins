# Cardinal for Devin

Devin (Cognition) runs in a cloud sandbox and has no plugin, hook, or MCP
surface. This adapter is a small **server-side poller** instead: it reads
the Devin REST API with an org service-user key and sends Cardinal OTLP
logs. Run one per Devin org. Design: [docs/specs/devin-adapter.md](../../docs/specs/devin-adapter.md).

Python 3.9+, standard library only. Ships as a release tarball and a
container image (see [Install](#install) and [Run in a container](#run-in-a-container));
it also runs straight from a checkout as `adapters/devin/bin/cardinal-devin`.

## Install

Each `devin-vX.Y.Z` GitHub release carries a self-contained tarball
(`cardinal_core` vendored, no pip):

```sh
VERSION=0.1.0
gh release download "devin-v${VERSION}" --repo cardinalhq/cardinal-agent-plugins \
  --pattern "cardinal-devin-${VERSION}.tar.gz"
tar -xzf "cardinal-devin-${VERSION}.tar.gz"
"./cardinal-devin-${VERSION}/bin/cardinal-devin" --version
```

Keep the extracted directory intact: `bin/cardinal-devin` loads
`cardinal_devin/` and `cardinal_core/` from next to it. Put `bin/` on
`PATH` or call it by path. Build the same tarball from a checkout with
`python3 build/devin.py` (writes `dist/devin/`).

## Setup

1. **Devin key.** Create a service user in Devin with `ViewOrgSessions`
   (and `ViewOrgMembership` for email attribution on v3, see below). Export
   its key:

   ```sh
   export DEVIN_API_KEY=cog_...
   export DEVIN_ORG_ID=...        # selects the v3 API; omit to use v1
   ```

   Or pass `--devin-key-file PATH`.

2. **GitHub token (recommended).** Devin reports only a PR URL. With
   `GITHUB_TOKEN` (read access to pull requests) the poller looks up the
   PR's head branch and head sha, which initiative attribution needs.
   Lookups only go to PRs on the web host the API serves:
   `https://api.github.com` serves `github.com`; for GitHub Enterprise set
   `GITHUB_API_URL` (or `--github-api-url`) to e.g.
   `https://ghe.corp/api/v3`, which serves `ghe.corp`. PRs on any other
   host get repo and PR keys from the URL only. One API host per poller.

3. **Connect to Cardinal** (device flow; mints an `ingest:write` key):

   ```sh
   adapters/devin/bin/cardinal-devin connect [--host https://app.cardinalhq.io]
   ```

   Credentials go in `~/.cardinal-devin/` (`--state-dir` or
   `CARDINAL_DEVIN_HOME` to change): `cardinal.json`,
   `cardinal-secrets.json` (0600), and `cardinal/devin-poll-state.json`.

   **Or, non-interactively** (containers, Kubernetes), set the ingest
   credential in the environment instead:

   | Env | |
   |---|---|
   | `CARDINAL_INGEST_ENDPOINT` | OTLP/HTTP base URL, without `/v1/logs` (the `ingest_endpoint` that `connect` stores in `cardinal.json`) |
   | `CARDINAL_INGEST_API_KEY` | an `ingest:write` key (the `ingest_api_key` in `cardinal-secrets.json`), sent as `x-cardinalhq-api-key` |
   | `CARDINAL_ORG` | optional: `cardinal.org` resource attribute (`connect` stores the org slug); default `unknown` |
   | `CARDINAL_DEPLOYMENT_ENV` | optional: `deployment.environment` resource attribute; default `unknown` |

   When both `CARDINAL_INGEST_ENDPOINT` and `CARDINAL_INGEST_API_KEY` are
   set they take precedence over any connect state, and `CARDINAL_ORG` /
   `CARDINAL_DEPLOYMENT_ENV` are the only source of those attributes.
   Setting just one of the pair is an error (`poll` exits 2). `status` says
   which source is in use; the key is never printed or logged. The poll
   state still lives in the state dir.

4. **Run it:**

   ```sh
   adapters/devin/bin/cardinal-devin status
   adapters/devin/bin/cardinal-devin poll --once --dry-run   # print, send nothing
   adapters/devin/bin/cardinal-devin poll --interval 300     # run continuously
   ```

   | Flag | Env | Default | |
   |---|---|---|---|
   | `--lookback-days` | `CARDINAL_DEVIN_LOOKBACK_DAYS` | 7 | untracked sessions last updated earlier are ignored |
   | `--max-pages` | `CARDINAL_DEVIN_MAX_PAGES` | 50 | list pages per cycle |
   | `--page-size` | | 100 | sessions per page (v3 max 200) |
   | `--active-ttl-days` | | 14 | stop polling an idle active session |
   | `--state-retention-days` | | 90 | keep finished session state |
   | `--max-state-sessions` | | 20000 | cap on tracked sessions |
   | `--api-version` | | auto | `v3` when an org id is set, else `v1` |

   Also `--devin-base-url` (`DEVIN_API_BASE_URL`), `--retry-updated-after-filter`,
   `--no-decisions`, `-v`.

   `poll` logs to stderr (`--dry-run` bodies go to stdout). It exits 1 when
   there is no Cardinal credential and 2 on other configuration errors (no
   Devin key, half-set `CARDINAL_INGEST_*`, unreadable poll state). A failed
   cycle is logged and retried next interval; SIGTERM stops it cleanly.

## Run in a container

Image: `ghcr.io/cardinalhq/cardinal-devin-poller` (`linux/amd64`,
`linux/arm64`), tagged `vX.Y.Z` and `latest`. It runs as uid 10001, with
`CARDINAL_DEVIN_HOME=/data`; mount a volume at `/data` so poll state
survives restarts. Default command: `poll --interval 300`.

```sh
export DEVIN_API_KEY=cog_... DEVIN_ORG_ID=... GITHUB_TOKEN=...
export CARDINAL_INGEST_ENDPOINT=https://... CARDINAL_INGEST_API_KEY=...
export CARDINAL_ORG=your-org CARDINAL_DEPLOYMENT_ENV=prod

docker volume create cardinal-devin-data
docker run -d --name cardinal-devin-poller --restart unless-stopped \
  -e DEVIN_API_KEY -e DEVIN_ORG_ID -e GITHUB_TOKEN \
  -e CARDINAL_INGEST_ENDPOINT -e CARDINAL_INGEST_API_KEY \
  -e CARDINAL_ORG -e CARDINAL_DEPLOYMENT_ENV \
  -v cardinal-devin-data:/data \
  ghcr.io/cardinalhq/cardinal-devin-poller:v0.1.0
docker logs -f cardinal-devin-poller
```

`-e NAME` without a value passes the variable through from your shell, so
keys stay out of the command line. Other commands work the same way, e.g.
`docker run --rm -e DEVIN_API_KEY ... -v cardinal-devin-data:/data
ghcr.io/cardinalhq/cardinal-devin-poller:v0.1.0 status`. To use the device
flow instead of env credentials, run `connect` once with `-it` and the same
volume.

Build it locally from the repository root:
`docker build -f adapters/devin/Dockerfile -t cardinal-devin-poller .`

### Kubernetes

Run **exactly one replica per Devin org**, with its state on a
PersistentVolumeClaim. Two pollers must never share one state file (they
would overwrite each other's progress and re-send), so use
`strategy: Recreate` rather than a rolling update, and don't scale the
Deployment.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cardinal-devin-poller
type: Opaque
stringData:
  DEVIN_API_KEY: cog_...
  DEVIN_ORG_ID: "..."
  GITHUB_TOKEN: ghp_...
  CARDINAL_INGEST_ENDPOINT: https://...
  CARDINAL_INGEST_API_KEY: "..."
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cardinal-devin-poller-state
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cardinal-devin-poller
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: cardinal-devin-poller
  template:
    metadata:
      labels:
        app: cardinal-devin-poller
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
      containers:
        - name: poller
          image: ghcr.io/cardinalhq/cardinal-devin-poller:v0.1.0
          args: ["poll", "--interval", "300"]
          envFrom:
            - secretRef:
                name: cardinal-devin-poller
          env:
            - name: CARDINAL_ORG
              value: your-org
            - name: CARDINAL_DEPLOYMENT_ENV
              value: prod
          volumeMounts:
            - name: state
              mountPath: /data
          resources:
            requests: {cpu: 10m, memory: 64Mi}
            limits: {memory: 256Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
      volumes:
        - name: state
          persistentVolumeClaim:
            claimName: cardinal-devin-poller-state
```

One Deployment, Secret and PVC per Devin org. Resource figures are a
starting point, not measured.

## Releasing

Bump `__version__` in `cardinal_devin/__init__.py`, merge, then push a
matching tag:

```sh
git tag devin-v0.1.0 && git push origin devin-v0.1.0
```

[`.github/workflows/devin-release.yml`](../../.github/workflows/devin-release.yml)
tests, publishes the GitHub release with the tarball, and pushes the image.
See [docs/RELEASING.md](../../docs/RELEASING.md#3-devin-poller).

### Decision capture (opt-in per session)

Devin can't run `cardinal-decision`. Instead, create sessions with the
Cardinal playbook and structured-output schema:

- Playbook text: [`playbook/cardinal-decisions.md`](playbook/cardinal-decisions.md).
  Create a Devin playbook from it and pass its id as `playbook_id`.
- Schema: `cardinal-devin decision-schema` prints
  [`playbook/decision-output.schema.json`](playbook/decision-output.schema.json)
  (JSON Schema draft 7, well under Devin's 64 KB limit). Pass it as
  `structured_output_schema` on `POST /v3/organizations/{org_id}/sessions`
  (or `POST /v1/sessions`).

Each `decisions[]` entry becomes one `cardinal.decision` as soon as the
poller sees it in `structured_output`, whatever the session's status
(working, blocked, suspended, finished). A decision is re-sent only if its
content changes (same `id`) or the PR it attaches to changes. Invalid
entries are skipped and logged with a reason.

The schema requires `id` and the playbook tells Devin to set one and never
change it. Entries that arrive without one anyway (structured output not
validated against this schema) get an id derived from `choice`
(`retry`, `retry-2`, ...). Those ids stay stable across polls when entries
are reordered or new ones inserted: a choice keeps the id it was first sent
under, and a new choice never takes an id already sent. Editing the
`choice` text of an id-less entry still creates a new decision, and the old
one is not marked superseded.

## What is captured

| Event | When | Attributes |
|---|---|---|
| `cardinal.git_state` | A PR first appears on a session; again at terminal only if something changed | `session_id`, `cardinal_repo`, `cardinal_remote_url`, `cardinal_pr_number`, `cardinal_pr_url`; with a matching GitHub API also `cardinal_branch`, `cardinal_head_sha`, `cardinal_initiative_name`, `cardinal_initiative_type` |
| `cardinal.decision` | A new or changed entry in `structured_output.decisions` | the `cardinal.decision.*` set from `cardinal_core.decisions`, plus `cardinal.repo/branch/head_sha/pr_number/pr_url` of the primary PR |

Resource: `service.name` and `agent.runtime` = `devin`, `user.email` from
the session, `cardinal.org` and `deployment.environment` from `connect`.

### Mapping from the Devin API

| Cardinal | v1 (`/v1/sessions`) | v3 (`/v3/organizations/{org_id}/sessions`) |
|---|---|---|
| session id | `session_id` | `session_id`; the detail path takes `devin_id`, which the docs define as "the session ID prefixed with `devin-`", so the prefix is added when missing |
| terminal | `status_enum` in `finished`, `expired` | `status` in `exit`, `error`, or `status_detail` = `finished` |
| user.email | `requesting_user_email` (list items only; carried to detail) | `user_id` → `GET /v3beta1/organizations/{org_id}/members/users/{user_id}` → `email` (beta; cached 24h, misses 1h) |
| PR | `pull_request.url` | `pull_requests[].pr_url`: one `git_state` per PR |
| primary PR (decisions) | the one PR | highest PR number, then greatest URL (`pull_requests[]` has no documented order) |
| decisions | `structured_output` | `structured_output` |
| updated time | `updated_at` (ISO) | `updated_at` (integer, no documented unit; values ≥ 10^11 read as ms, else seconds) |
| pagination | `limit`/`offset`, until a short page | `first`/`after`, `has_next_page`/`end_cursor` |
| incremental | none documented: full list each cycle | `updated_after` = last verified cycle − 5 min, checked every cycle (below) |
| not found | detail documents only 422 (`HTTPValidationError`, no not-found shape): logged as a warning with the body and counted as a miss | 404 |

**The v3 `updated_after` filter is an optimisation, not trusted.** Its unit
isn't documented, and a wrong unit either hides every session (value sent
too large) or lets everything through. Each cycle the poller first reads
one unfiltered page, then the filtered listing, and disables the filter if
the filtered listing returns a session last updated before the value sent,
or misses a session from the unfiltered page updated after it. When
disabled (persisted in state, shown by `status`, logged as a warning every
cycle) the poller lists without the filter, bounded by `--max-pages`, in
that same cycle, and the high-water mark stops advancing. The check can
only catch hidden sessions that appear on the unfiltered first page; the
list order isn't documented. Re-enable with `--retry-updated-after-filter`.

v3's list operation declares one query parameter, `qs`
(`SessionsQueryParams`), with no `style`/`explode`; OpenAPI's defaults
for query objects (form, explode) serialise it as flat parameters
(`first=…&after=…&updated_after=…`), which is what is sent.

Repo and remote come from the PR URL (GitHub `/{owner}/{repo}/pull/{n}`,
GitLab `/{group}/{repo}/-/merge_requests/{n}`). A GitHub lookup adds
`head.ref` / `head.sha` and may replace the remote with the base repo's
`clone_url`, but only when that URL is on the PR's own host. Initiative is
classified from `head.ref` with `cardinal_core.initiative`. Without a
branch, no initiative is sent (classifying "no branch" would wrongly claim
`research`).

The docs show v1 (deprecated) and v3 (current). Both are handled; which
one a given org's key accepts is unconfirmed.

### Idempotency and its limits

The poll state file (atomic writes, compact JSON) records, per session,
short fingerprints of each PR's `git_state`, each decision, and the last
`structured_output`, plus email and user id while the session is active.
Re-runs and restarts send nothing already accepted. State is updated only
after ingest returns 2xx, so a failed send is retried next cycle. A corrupt
state file stops the poller rather than resetting (which would re-send the
lookback window).

- **Size.** Finished sessions that never sent anything are dropped once
  past the lookback window. Above `--max-state-sessions`, the oldest
  finished entries outside the lookback window are evicted first; active
  entries and entries inside the window are never evicted (a warning is
  logged if that leaves the state above the cap). The user email cache
  keeps entries for a day.
- **Crash window.** State is saved after each session whose records ingest
  accepted, and once per cycle. A crash between an accepted POST and that
  save re-sends that one session's records once on restart.
- **Downtime.** Untracked sessions last updated before `--lookback-days`
  are ignored, so downtime longer than the lookback skips sessions that
  finished during the gap. Raise the lookback before restarting after a
  long outage.
- **Resumed sessions.** Finished, stale, and gone entries are kept for the
  longest of `--state-retention-days`, lookback + 1 day, and active TTL +
  1 day, unless evicted by the cap. A session resumed within that time
  re-sends nothing; one resumed later is treated as new and re-sends its
  `git_state` and decisions.
- **Not found.** An active session whose detail returns not-found is only
  dropped after 3 cycles in a row. Its decisions were already sent when
  first seen.
- **Rate limits.** HTTP 429 from Devin is retried with `Retry-After`
  (seconds or HTTP-date) or exponential backoff, capped at 60s.

## Gaps

- **v1 listing cap.** v1 has no documented order or `updated_after`
  filter, so every cycle lists from offset 0 up to `--max-pages` ×
  `--page-size` sessions (5,000 by default). Sessions past the cap are not
  seen, and a warning is logged every cycle it happens. Use v3, or raise
  the cap.
- **No cost.** Devin has no API for spend. v3 returns `acus_consumed`, but
  no contract event carries it, so it isn't sent.
- **Polling latency.** No webhooks or streaming; data lags by up to one
  interval.
- **No per-message or tool events.** `messages[]` (v1) and the v3 messages
  route don't map to any existing contract event, and message types aren't
  enumerated, so nothing is sent. No session start/end events either; the
  session appears via `git_state`.
- **Sessions without a PR send no `git_state`.** The session schema has no
  repo or branch fields. Their decisions go out without repo/PR keys.
- **Decisions need the schema.** Only sessions created with it (Devin has
  no org-wide playbook auto-attach). No D18 code clusters (no checkout).
- **Attribution by email.** v1 gives `requesting_user_email`; v3 needs the
  beta user lookup and `ViewOrgMembership`. Sessions started by a service
  user, or users the lookup can't resolve, go out as `user.email=unknown`.
  The ingest key belongs to whoever ran `connect`.
- **Without a GitHub token, or for PRs on other hosts:** no branch, head
  sha, or initiative; outcomes must join on repo + PR number.
- **Idle active sessions** stop being polled after `--active-ttl-days` (v3).
- **One poller per state.** State is a local JSON file with no locking;
  run a single instance per state directory.

## Backend requirement

Lakerunner's agent-session tap must accept `agent.runtime=devin` before
these sessions appear on Agent Outcomes. This is the same change OpenCode
and Pi need; see [docs/RELEASING-NATIVE.md](../../docs/RELEASING-NATIVE.md).

## Unverified

- Everything against a live org: payload shapes, v1 vs v3 availability,
  the v3 timestamp unit, and whether `updated_at` moves when a PR is added
  or `structured_output` changes.
- Rate limits: the docs list 429 with no numbers or headers.
- Whether Cardinal's device flow accepts client id `cardinal-devin-poller`.
- Terminal semantics: `suspended` with `status_detail` other than
  `finished` is treated as active.

## Tests

```sh
PYTHONPATH=core python3 -m unittest discover adapters/devin/tests
PYTHONPATH=core python3 adapters/devin/tests/capture_goldens.py   # after an intended output change
```

Fake Devin, GitHub, Cardinal, and OTLP servers run on localhost; fixtures
in `tests/fixtures/` are synthetic, written from the docs. Goldens in
`tests/goldens/` feed `tests/test_contract.py`. `tests/test_build.py`
builds the release tarball, extracts it outside the repo, and runs it with a
clean environment. CI: `.github/workflows/devin-adapter.yml` (Python 3.9 and
3.12); releases: `.github/workflows/devin-release.yml`.
