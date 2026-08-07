# Rationale — statuspage-service-health-check 0.1.0

**Source session:** `0a8b2e89` (Claude Code, CWD `cardinal-agent-plugins`, 2026-08-06)

## Investigation summary

One-turn user request: *"check github status, to check gh service health."*

The session's substantive investigation is exactly one evidence-producing tool
call — `WebFetch` against `https://www.githubstatus.com/api/v2/status.json`
with an LLM-projecting prompt asking for overall status and description — and
one conclusion assistant turn reporting *"All Systems Operational"*. That is a
`service-health-assessment` conclusion (a classification of state), so per
CORE.md Stage 3 the session IS compilable — but the DAG's total load-bearing
content is one tool call and one classification, so the ratio of scaffolding to
substance in the output artifact is unavoidably high. See the "Judgment calls"
and "Unresolved" sections below.

## Stage 2 — tool-call classification

| Ord | Tool call                                         | Class       | Reason |
|-----|---------------------------------------------------|-------------|--------|
| 1   | `ToolSearch(select:WebFetch)`                     | INCIDENTAL  | Cardinal harness on-demand tool-schema loader; not investigation evidence. Loading a tool schema is plumbing, not an investigative step. |
| 2   | `WebFetch(githubstatus.com/api/v2/status.json, "Extract overall GitHub service status and description.")` | REQUIRED | Sole evidence-producing call; its projection *is* the conclusion. |

No spill-to-disk pairs (session too small). No `bash.*` shell-shaped calls.
Everything from message 27 onward (the `/mechanize` invocation itself) is
INCIDENTAL meta-work and excluded per CORE.md Stage 2.

## Stage 2.5 — code-reading option

Not applicable. No grep/read of source code in the session.

## Stage 3 — procedure signature

```
objectiveClass: service-health-status-check
evidencePattern:
  - public-status-endpoint
transformations:
  - project-status-indicator
  - classify-severity
judgments:
  - service-health-classification
outputClass: service-health-finding
```

**Reusable across:** any service exposing a statuspage.io-shaped status.json
endpoint (GitHub, GitLab, Cloudflare, Slack, Stripe, Atlassian, …). The
`variationPoints` on `service` and `statusEndpoint` are how a future operator
retargets.

## Stage 3.5 — mechanization scan

No M-pattern matches.

- **M1 series-statistic-reduction** — no time-series in the session.
- **M2 cross-source-quantity-reconciliation** — only one evidence source.
- **M3 json-field-extract-and-carry** — the operator did extract two fields
  (Overall Status, Indicator) from the fetched response, but the extraction
  happened *inside the WebFetch call's LLM projection*, not in operator-side
  code between two tool calls. M3 targets the between-tool-calls glue pattern,
  and there is no "next tool call" here. The classification into
  operational/degraded/incident that logically follows is an operator judgment
  step, not an M3 extraction.

## Stage 4 — DAG synthesis

Two nodes (v0.2.0; the v0.1.0 tool node was withdrawn — see JC-1):

- **`classify-status`** (`kind: function`) — parses the two anchor lines
  (`**Overall Status:**` and `**Indicator:**`) out of `${inputs.statusPayload}`
  and maps the indicator token to a level in `{operational, degraded,
  incident, unknown}`. §32 says function (deterministic transformation over
  declared inputs) beats llm here. Network-free, so it trials hermetically.

- **`emit-health-status`** (`kind: emit`) — always fires (no `when:` gate);
  severity is computed from `level` via `severityExpression`
  (`operational→info`, `incident→critical`, else `warning`). `dedupeKey` is
  `${inputs.service}:${nodes.classify-status.output.indicator}` — stable
  across identical runs, changes only when the underlying indicator changes.
  Evidence is one `{nodeRef, field}` mapping per §14.

**Node-ID choices:**

- `classify-status` — says what the function decides.
- `emit-health-status` — says what side effect and about what.

## Stage 4.5 — attachment handling

Not applicable. The session contains no image or document blocks.

## Judgment calls

**JC-1: no capability at all (v0.2.0).** This Sentinel declares
`capabilities.required: []`. The source session's one evidence step was
Claude Code's built-in `WebFetch` — **not an MCP tool** — and CORE.md Stage 2.1
is explicit that only MCP-backed tool calls become `kind: tool` nodes. Anything
else is generated function code, and no capability id is minted for it.

This took a long detour worth recording, because the detour is the reason
Stage 2.1 now exists. The compile originally modelled the fetch as a `tool`
node and invented a capability for it. R2 at the time checked only that an id
carried an *abstract prefix*, so the honest name `web.fetch-with-summary`
FAILED ratification and the compile renamed it to
`observability.fetch-status-summary` to get through — filing an HTTP GET of a
third party's public status page under the family that means "query our own
telemetry backend." No registry contained that id and no provider implemented
it, yet prefix-only R2 passed it and the trial then died with
`UnknownProviderError`. Three separate fixes came out of that single wrong
turn: R2 now checks registry membership rather than prefix; CORE.md Stage 2.1
states the MCP rule the compiler was never given; and the `web.*` registry
entry was withdrawn, since an outbound call no MCP tool serves is not a
capability.

**Why the fetch is an input.** With the fetch necessarily a `function` node,
it would have to reach the network — and a network-reaching function cannot
be trial-executed hermetically, which is the one thing `trial.py` promises.
Function nodes have no fixture mechanism (`load_function` always runs the real
body), so there is no way to stub the call. Rather than ship a Sentinel that
either fails its own trial or quietly dials out during one, the payload is a
required input and the DAG covers the part that is deterministic: the
classification.

**The fidelity loss is real and worth stating plainly.** As compiled, this
Sentinel does not fetch anything, so it is not autonomously schedulable —
something must hand it a payload. The reusable procedure it captures is
"classify a status-page payload and emit a graded finding", which is narrower
than what the session did. The honest fix is not a workaround in this
artifact; it is function-node fixtures in the trial harness, which would let
the fetch live in the DAG and still be trialed. That is filed as the gap.
`inputs.json` carries the payload the session actually captured, verbatim —
not a synthesized one.

**JC-2: parsing a markdown projection, not JSON.** The endpoint returns JSON
whose `status.indicator` / `status.description` would parse far more cleanly
than markdown anchor lines. But the session never captured that JSON — Claude
Code's `WebFetch` projected the response through an LLM before returning it,
so the projected markdown is the only payload the session actually observed.
Parsing what was captured keeps `inputs.json` honest; reaching for the raw
JSON shape would mean inventing a payload nobody recorded, which is the same
prohibition as "do NOT synthesize fixtures." The cost is a parser keyed to
markdown anchors, and therefore to the projection prompt's output format —
noted in the rubric's reusability item.

**JC-3: `classify-status` body is generated, not a stub.** CORE.md Stage 7
reserves generated bodies for M1/M2/M3 patterns and stubs everything else.
This node matched no M-pattern, so it was first emitted as a
`NotImplementedError` stub and Stage 10 duly failed T3. Stage 12's
iterate-once then filled it from the sketch in its own stub comment: the
transformation is a small deterministic regex-and-map, pattern-like even
though it is not one of the three enumerated patterns. That is the "compiler
bug with an obvious fix" case Stage 12 describes. Worth flagging as a gap in
the pattern list rather than a one-off: markdown-anchor extraction is common
enough to deserve an M-pattern.

## Function bodies

- `classify-status` — **generated** (see JC-3), deterministic and network-free.

## Unresolved

- **Stage 5.5 cold subagent skipped for budget** — Stage 5.5 was run by
  invoking `common/mechanize/ratification.py` directly from the compiler
  context, not via a cold subagent. Ratification is deterministic Python
  driven by the shared module, so the degradation here is small (no
  independent judgment to lose), but the "cold read of the YAML by an agent
  that didn't compile it" property is not present. This is noted for the R6
  bar per CORE.md Stage 5.5.
- **Stage 9b cold review skipped for budget.** Stage 9a's rubric was
  generated inline (warm) and Stage 9b's cold grade was skipped entirely
  per CORE.md Stage 9b's "prefer to skip Stage 9b entirely" guidance when a
  cold-subagent pass is unavailable. `review.md` is intentionally absent.
- **Trial PASSED (T1–T9)** — 2 nodes SUCCEEDED, one finding emitted of type
  "service-health-status" at severity "info" with dedupeKey "github:none",
  evidence fully resolved, identical across two runs, T9 confirming the match
  to the source investigation's conclusion.

- **What this compile cost, and what it bought.** Reaching that verdict took
  five iterations, and four of the five fixed something outside this Sentinel.
  In order: a stub body filled per Stage 12 (T3); the fixture provider's
  hardcoded capability whitelist, which rejected any newly-minted id even with
  the fixture on disk (T4); a nested `?:` in `severityExpression` whose
  parenthesised branch the executor's rewrite left as a literal `?`, killing
  the emit node; an evidence `field: output.text` where §14 wants a single
  output key, not a dotted path (T6) — that one a genuine error in this
  artifact; and finally the whole tool-node premise, withdrawn under CORE.md
  Stage 2.1 (JC-1).

  Two of those are worth naming as a class. T6 is exactly the check CORE.md
  says was added after an empty-evidence `critical` finding reached production,
  and it caught a real instance here. And the ternary bug had been latent since
  the rewrite was written — no checked-in Sentinel had ever nested one. This
  compile found it by being the first to try.

- **The fetch is not in the DAG**, so this Sentinel is not autonomously
  schedulable as compiled. See "Why the fetch is an input" above. The blocking
  gap is function-node fixtures in the trial harness.

- **`functions.<id>.filesystem` is still unenforced.** `network` is now
  enforced by `spike/executor/sandbox.py`; `filesystem` remains declared,
  linted, and ignored at runtime. This Sentinel's one function touches
  neither, so nothing here depends on it — recorded because a reader of any
  `deployment.yaml` would reasonably assume otherwise.
- **Single-branch trial coverage.** The source session exercised only the
  `operational` branch (indicator `none`). The `degraded`/`incident` severity
  mappings in `emit-health-status.severityExpression` are compiler
  generalizations — they follow the statuspage.io indicator convention but
  the source investigation did not observe them.

## Not retained (with reason)

- Ordinal 1 `ToolSearch(select:WebFetch)` — INCIDENTAL (harness plumbing).
- Everything from message 27 onward (the `/mechanize` invocation and all its
  own tool calls) — INCIDENTAL per Stage 2 ("any call occurring after the
  terminal assistant conclusion").
