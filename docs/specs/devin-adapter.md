# Devin adapter (design)

Status: design, not built. Research done 2026-09-14 against Devin's public
docs; nothing below has been checked against a live Devin org.

## Why

Every other agent Cardinal supports emits PR linkage on `cardinal.git_state`
and, where the host allows, opt-in `cardinal.decision` capture
(`docs/specs/adapter-parity.md`). Devin (Cognition) is a cloud agent that
teams run alongside those CLIs, so its sessions are missing from Agent
Outcomes today.

## What makes Devin different

Devin runs in Cognition's cloud sandbox. There is no plugin, hook, or
extension surface an engineer installs, so none of the mechanisms the other
adapters use apply: no in-process emitter, no prompt hook, no session files,
no MCP server we can hand it. An integration has to observe Devin from
outside through its API.

## API surface (from docs.devin.ai, 2026-09-14)

| Need | What the docs show | Source |
|---|---|---|
| Auth | Bearer token. Service user keys (`cog_…`) are org-scoped with RBAC and `create_as_user_id`; personal access tokens are user-scoped. No OAuth. | [API overview](https://docs.devin.ai/api-reference/overview) |
| List sessions | `GET /v1/sessions` with `tags`, `user_email`, `limit`, `offset`. The index also references `/v3/...` forms; confirm which is current. A service user key can list all org sessions. | [List sessions](https://docs.devin.ai/api-reference/sessions/list-sessions) |
| Session detail | `GET /v1/sessions/{id}` returns `status_enum`, `title`, `tags`, `playbook_id`, `structured_output`, `pull_request.url`, and `messages[]` with `type`, `event_id`, `message`, `timestamp`, `origin`, `user_id`, `username`. | [Retrieve session](https://docs.devin.ai/api-reference/sessions/retrieve-details-about-an-existing-session) |
| Create session | `POST /v3/organizations/{org_id}/sessions` accepts `playbook_id`, `knowledge_ids`, and `structured_output_schema` (JSON Schema draft 7, 64 KB max). | [Create session](https://docs.devin.ai/api-reference/sessions/create-a-new-devin-session) |
| Webhooks | None outgoing. The "webhook" pages describe incoming triggers (PagerDuty, Datadog, Sentry → Devin). | [Automations](https://docs.devin.ai/product-guides/automations) |
| Streaming | None documented (no SSE or websocket). | — |
| Usage / cost | UI only (Consumption Analytics). No API for ACUs or cost. | [Usage](https://docs.devin.ai/admin/billing/usage) |
| Playbooks | Attached per session at create time. No org-wide auto-attach. | [Creating playbooks](https://docs.devin.ai/product-guides/creating-playbooks) |
| Slack origin | Not in the session schema; Slack users map to Devin users by email. | [Slack](https://docs.devin.ai/integrations/slack) |

## Proposed shape

`adapters/devin/` holds a small server-side poller, not a per-user plugin.
One deployment per Cardinal org, configured by an org admin with a Devin
service user key and the org's Cardinal ingest credentials.

1. **Poll** `list sessions` on an interval, tracking a high-water mark per
   org. Fetch detail for new sessions and for any session whose status is
   still active.
2. **Emit** OTLP with `service.name` / `agent.runtime` = `devin`, reusing
   `cardinal_core.otlp`. Emission is idempotent: key each record on
   `session_id` + `event_id` and keep a local cursor so restarts don't
   duplicate.
3. **Finish** a session when `status_enum` reaches a terminal state; emit
   the final `git_state` and any decisions then.

Where it runs (a standalone container, or a job inside maestro/conductor)
is open; the adapter code shouldn't depend on it.

## Signal mapping

| Cardinal event | Source | Confidence |
|---|---|---|
| session start/end, engineer | session `created`/terminal status, `user_email` | documented |
| `cardinal.git_state` with `cardinal_pr_number` / `cardinal_pr_url` | `pull_request.url` on session detail; repo and branch parsed from the PR | documented |
| `cardinal.turn` | one per `messages[]` entry | documented |
| `cardinal.turn_tool` | only if `messages[].type` distinguishes tool calls | **unconfirmed** — types aren't enumerated |
| `cardinal.decision` | `structured_output` (see below) | documented, opt-in |
| spend / cost | none | **gap** |

Initiative attribution follows the usual rule once the branch is known:
`<type>/<kebab-name>` parsed from the PR's head branch.

## Decision capture

Devin can't run `cardinal-decision` or be prompted by a hook. The closest
equivalent is structured output:

- Cardinal ships a playbook that tells Devin to record material choices,
  plus a `structured_output_schema` whose top level is a `decisions` array
  using the `cardinal.decision` fields (question, choice, rationale,
  alternatives, anchors, decided_by).
- The poller reads `structured_output` when the session finishes and emits
  one `cardinal.decision` per entry, with the PR and branch from the session.

Limits: decisions arrive once at the end, not as they happen. It only works
for sessions created with the playbook and schema (API-created sessions, or
teams that standardise on the playbook), because Devin has no org-wide
auto-attach. That makes the opt-in explicit, the way `cardinal-decision on`
is for the CLIs.

## Gaps and risks

- **No push path.** Polling trades freshness against API load; rate limits
  aren't documented beyond HTTP 429.
- **No cost.** Sessions land on the dashboard without dollars unless
  Cognition exposes ACU usage by API.
- **Tool-level detail unknown** until a real session payload is inspected.
- **Attribution by email.** Joining Devin users to Cardinal users needs the
  enterprise users endpoint, which the docs index references but doesn't
  document.
- **Backend.** Lakerunner's agent-session tap must accept the `devin`
  runtime before sessions appear (same change OpenCode and Pi need; see
  `docs/RELEASING-NATIVE.md`).

## Before building

1. With a service user key, capture one real `GET /sessions/{id}` payload:
   enumerate `messages[].type`, confirm the API version, and check whether
   any usage fields exist.
2. Confirm list-sessions pagination and rate limits on a busy org.
3. Decide where the poller runs and how its credentials are provisioned.
4. Get product sign-off that cost-less Devin sessions are useful on Agent
   Outcomes.
