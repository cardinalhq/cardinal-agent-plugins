# Cardinal for Devin

Devin (Cognition) runs in a cloud sandbox and has no plugin, hook, or MCP
surface. This adapter is a small **server-side poller** instead: it reads
the Devin REST API with an org service-user key and sends Cardinal OTLP
logs. Run one per Devin org. Design: [docs/specs/devin-adapter.md](../../docs/specs/devin-adapter.md).

> **Unvalidated.** Built and tested against Devin's published docs
> (docs.devin.ai, read 2026-09-14) with synthetic fixtures. It has not been
> run against a live Devin org.

Python 3.9+, standard library only. No packaging yet; run it from a checkout.

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
   GitHub Enterprise: `--github-api-url` or `GITHUB_API_URL`.

3. **Connect to Cardinal** (device flow; mints an `ingest:write` key):

   ```sh
   adapters/devin/bin/cardinal-devin connect [--host https://app.cardinalhq.io]
   ```

   Credentials go in `~/.cardinal-devin/` (`--state-dir` or
   `CARDINAL_DEVIN_HOME` to change): `cardinal.json`,
   `cardinal-secrets.json` (0600), and `cardinal/devin-poll-state.json`.

4. **Run it:**

   ```sh
   adapters/devin/bin/cardinal-devin status
   adapters/devin/bin/cardinal-devin poll --once --dry-run   # print, send nothing
   adapters/devin/bin/cardinal-devin poll --interval 300     # run continuously
   ```

   Useful flags: `--lookback-days` (default 7), `--active-ttl-days`
   (default 14), `--api-version auto|v1|v3`, `--devin-base-url`,
   `--no-decisions`, `-v`.

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

When the session reaches a terminal status, each `decisions[]` entry
becomes one `cardinal.decision`, tagged with the session's PR, repo,
branch, and head sha. Invalid entries are skipped and logged with a
reason.

## What is captured

| Event | When | Attributes |
|---|---|---|
| `cardinal.git_state` | A PR first appears on a session; again at terminal only if something changed | `session_id`, `cardinal_repo`, `cardinal_remote_url`, `cardinal_pr_number`, `cardinal_pr_url`; with `GITHUB_TOKEN` also `cardinal_branch`, `cardinal_head_sha`, `cardinal_initiative_name`, `cardinal_initiative_type` |
| `cardinal.decision` | Session terminal, from `structured_output.decisions` | the `cardinal.decision.*` set from `cardinal_core.decisions`, plus `cardinal.repo/branch/head_sha/pr_number/pr_url` |

Resource: `service.name` and `agent.runtime` = `devin`, `user.email` from
the session, `cardinal.org` and `deployment.environment` from `connect`.

### Mapping from the Devin API

| Cardinal | v1 (`/v1/sessions`) | v3 (`/v3/organizations/{org_id}/sessions`) |
|---|---|---|
| session id | `session_id` | `session_id` (used as the `{devin_id}` path value; the docs don't confirm they're the same) |
| terminal | `status_enum` in `finished`, `expired` | `status` in `exit`, `error`, or `status_detail` = `finished` |
| user.email | `requesting_user_email` (list items only; carried to detail) | `user_id` → `GET /v3beta1/organizations/{org_id}/members/users/{user_id}` → `email` (beta; cached 24h, misses 1h) |
| PR | `pull_request.url` | `pull_requests[].pr_url` (one `git_state` per PR; decisions use the first) |
| decisions | `structured_output` | `structured_output` |
| updated time | `updated_at` (ISO) | `updated_at` (integer; read as unix seconds) |
| pagination | `limit`/`offset`, until a short page | `first`/`after`, `has_next_page`/`end_cursor` |
| incremental | none documented: full list each cycle | `updated_after` = last successful cycle − 5 min |

Repo and remote come from the PR URL (GitHub `/{owner}/{repo}/pull/{n}`,
GitLab `/{group}/{repo}/-/merge_requests/{n}`); the GitHub lookup replaces
the remote with the base repo's `clone_url` and adds `head.ref` / `head.sha`.
Initiative is classified from `head.ref` with `cardinal_core.initiative`.
Without a branch, no initiative is sent (classifying "no branch" would
wrongly claim `research`).

The docs show v1 (deprecated) and v3 (current). `auto` picks v3 when an
org id is set. Both are handled; which one a given org's key accepts is
unconfirmed.

### Idempotency

The poll state file (atomic writes) records, per session, a fingerprint of
each PR's `git_state` and each decision's attributes. Re-runs and restarts
send nothing that was already accepted. State is updated only after
ingest returns 2xx, so a failed send is retried next cycle. A corrupt
state file stops the poller rather than resetting (which would re-send
the lookback window). Sessions last updated before `--lookback-days` are
ignored unless already tracked, and finished entries are pruned a day
after that window. HTTP 429 from Devin is retried with `Retry-After`
(seconds or HTTP-date) or exponential backoff, capped at 60s.

## Gaps

- **No cost.** Devin has no API for spend. v3 returns `acus_consumed`, but
  no contract event carries it, so it isn't sent.
- **Polling latency.** No webhooks or streaming; data lags by up to one
  interval.
- **No per-message or tool events.** `messages[]` (v1) and the v3 messages
  route don't map to any existing contract event, and message types aren't
  enumerated, so nothing is sent. No session start/end events either; the
  session appears via `git_state`.
- **Sessions without a PR send no `git_state`.** The session schema has no
  repo or branch fields.
- **Decisions only at session end,** and only for sessions created with
  the schema (Devin has no org-wide playbook auto-attach). A session that
  resumes and finishes again re-sends only decisions whose content changed.
  No D18 code clusters (no checkout to read the tree from).
- **Attribution by email.** v1 gives `requesting_user_email`; v3 needs the
  beta user lookup and `ViewOrgMembership`. Sessions started by a service
  user, or users the lookup can't resolve, go out as `user.email=unknown`.
  The ingest key belongs to whoever ran `connect`.
- **Without `GITHUB_TOKEN`:** no branch, head sha, or initiative; outcomes
  must join on repo + PR number.
- **Active sessions** with no update for `--active-ttl-days` stop being
  polled (v3). On v1 every session is re-listed each cycle, bounded by
  `--max-pages`.

## Backend requirement

Lakerunner's agent-session tap must accept `agent.runtime=devin` before
these sessions appear on Agent Outcomes. This is the same change OpenCode
and Pi need; see [docs/RELEASING-NATIVE.md](../../docs/RELEASING-NATIVE.md).

## Unverified

- Everything against a live org: payload shapes, v1 vs v3 availability,
  whether v3's `qs` query object is sent as flat parameters (assumed),
  `devin_id` = `session_id`, integer timestamps being seconds, and whether
  `updated_at` moves when a PR is added.
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
`tests/goldens/` feed `tests/test_contract.py`.
