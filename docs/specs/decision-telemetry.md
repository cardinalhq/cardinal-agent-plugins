# Decision telemetry (`cardinal.decision`)

An agent records the decisions it makes while it works: the question the
work raised, the option it chose, why, and what it rejected. Each decision
is tagged so it can be found again by engineer, session, repo, branch, PR,
and the code it concerns. Decisions from several engineers' sessions can
feed one PR; merging them into one decision graph is future work. This
spec covers capture, transport, and storage.

Replaces the removed Semantic DAG skill, which drew a local graph of
seven node types. Only decisions are kept: its outcome nodes were mostly
unverified claims, and nothing left the machine.

## Flow

| Step | Where | What |
| --- | --- | --- |
| Opt in | `cardinal-decision on` | Off by default. `CARDINAL_DECISIONS=1/0` (process env or `~/.claude/settings.json` env) overrides. |
| Prompt | `hooks/decision-prompt.py` (UserPromptSubmit) | When on, tells the agent how to record decisions and lists this session's decisions so new ones can link to them. |
| Record | `bin/cardinal-decision record …` | Validates, tags, and emits one OTLP log record to `/v1/logs`. Also writes the session ledger. |
| Store | lakerunner `agent_session_decisions` | One row per `(org, session, decision_id)`; a newer event for the same id replaces the row. Not swept with raw events. |
| Read | lakerunner `GET /api/v1/agent-sessions/decisions` | Filters: `session_id`, `repo`, `branch`, `pr_number`, `user_email`, `from`, `to`, `limit`. |
| Show | conductor Agent Outcomes | Decisions sections in the session and PR drawers. |

Shared logic lives in `core/cardinal_core/decisions.py`; the Claude
adapter owns the CLI, prompt wording, and emission.

## Event attributes

Resource attributes are the adapter's usual set (`user.email`,
`cardinal.org`, `service.name=claude-code`, …). Log attributes, with
`event_name=cardinal.decision`:

| Attribute | Notes |
| --- | --- |
| `session_id` | Supplied to the CLI by the prompt hook (`--session`). |
| `cardinal.decision.id` | Unique within the session; `[a-z0-9][a-z0-9._-]{0,63}`. Derived from the choice unless `--id` is given; reusing an id revises that decision. |
| `cardinal.decision.question` | ≤300 chars. |
| `cardinal.decision.choice` | Required, ≤200 chars. |
| `cardinal.decision.rationale` | ≤1000 chars. |
| `cardinal.decision.decided_by` | `user` or `agent`. |
| `cardinal.decision.alternatives` | JSON array of rejected options (≤8). |
| `cardinal.decision.links` | JSON array of `{relation, to}`; relation ∈ `follows_from`, `refines`, `supersedes`; `to` is a decision id. |
| `cardinal.decision.anchors` | JSON array in the invariant-harness Anchor shape, `{kind, identifier, path?}`; kind ∈ `symbol`, `interface`, `schema`, `config`, `workflow`, `benchmark`, `directory`, `file`. |
| `cardinal.decision.code_clusters` | JSON array of D18 cluster ids (`d18:<path-prefix>`) for the anchor paths. |
| `cardinal.decision.cluster_scheme` | e.g. `d18-v1:cap=100,min=10`. Cluster ids are only comparable within one scheme. |
| `cardinal.repo`, `cardinal.branch`, `cardinal.head_sha` | Git state when the decision was recorded (lakerunner falls back to the session's `cardinal.git_state`). |
| `cardinal.pr_number`, `cardinal.pr_url` | The branch's most recent PR via `gh pr view`, cached 10 min (2 min for a miss). Absent before a PR exists; the dashboard also matches on repo + branch. |

Empty values are omitted. List-valued fields are JSON strings because
OTLP attributes reach the lakerunner processor as flat strings.

## Code clusters

Clusters are D18 path-prefix domains, ported from
`invariant_harness/clustering_d18.py`: build a trie of the repo's source
files, take subtrees with between `min` and `cap` files as domains, split
larger ones, and sweep small leftovers into `<prefix> (misc)`. The input
is the committed tree at HEAD (`git ls-tree -r HEAD`) filtered by the
harness's source-file rules, so one commit gives the same clusters on
every machine. Domains are cached per repo + commit under
`~/.claude/cardinal/decisions/cache/`. A file anchor maps to at most one
cluster; a directory anchor maps to every cluster inside it.

Cluster ids change as a repo grows (a prefix crossing `cap` splits), so
consumers should compare them within one `cluster_scheme` and prefer
anchors for long-lived joins.

## Local state

`~/.claude/cardinal/decisions/`: `config.json` (the on/off switch),
`sessions/<session>.json` (the ledger the prompt hook renders), and
`cache/` (D18 domains, PR lookups).
