# mechanize-compile (spike)

**Compile a Claude Code session into a Sentinel DAG candidate.** This is spike-quality: it's for testing what a well-guided skill can produce from a real session, before we commit to the phase 2 compiler design. Not a shipping skill.

## When to run

Invoked as a subagent from a parent session. Given: the path to a Claude Code session JSONL (from `~/.claude/projects/<encoded-cwd>/<session>.jsonl`), produce a candidate Sentinel YAML plus a rationale document.

## Inputs

- `SESSION_PATH` — absolute path to the JSONL file being compiled.
- `OUT_DIR` — where to write outputs. Default `spike/skill-driven-harvest/out/`.
- `SPEC_PATH` — path to `sentinels.md` for reference. Default `<repo-root>/sentinels.md`.

## Read the spec first, in this order

Do NOT skip this. The compiler design depends on knowing what a Sentinel is.

1. `sentinels.md` §8 — Sentinel schema (full example)
2. `sentinels.md` §9 — Node model
3. `sentinels.md` §10 — Tool-node contract
4. `sentinels.md` §11 — Function-node contract
5. `sentinels.md` §12 — LLM-node contract
6. `sentinels.md` §13 — Condition-node contract
7. `sentinels.md` §14 — Emit-node contract
8. `sentinels.md` §28 + §28.1 — CaptureEvent + adapter contract (esp. attachment rules)
9. `sentinels.md` §29 — Compiler stages (this skill executes them)
10. `sentinels.md` §37 — Experiment 1 pass criteria (what "success" means)
11. `sentinels.md` §47 — Audit log (what you must produce alongside the DAG)
12. `sentinels.md` §52 — Most important design constraint (do NOT optimize for reuse percentage)

Also read: `spike/dag-harvest/FINDINGS.md` — empirical findings from the heuristic spike. Every rule in that document supersedes what a naive reading of the spec might suggest.

## What a Sentinel DAG looks like — concrete example

This is the shape you are producing. Keep it in front of you while compiling.

```yaml
apiVersion: mechanize.dev/v1alpha1
kind: Sentinel
metadata:
  name: post-deployment-error-regression
  version: 0.1.0
spec:
  purpose:
    summary: >
      Determine whether a deployment caused a material increase in
      application errors relative to its recent baseline.
    reusableQuestion: >
      Did a recent change produce a statistically and operationally
      meaningful increase in errors?
    conclusionType: regression-assessment
  inputs:
    service: { type: string, required: true }
    environment: { type: string, default: production }
    baselineWindow: { type: duration, default: 24h }
    minimumIncrease: { type: number, default: 0.25 }
  capabilities:
    required:
      - id: deployments.list
        capabilityType: tool
      - id: telemetry.query-timeseries
        capabilityType: tool
  variationPoints:
    - path: /spec/inputs/service
      operations: [bind]
    - path: /spec/nodes/query-current/config/toolRef
      operations: [replace-binding]
  nodes:
    get-deployment:
      kind: tool
      dependsOn: []
      config:
        toolRef: deployments.list
        arguments:
          service: "${inputs.service}"
          environment: "${inputs.environment}"
      output:
        schema:
          type: object
          required: [deploymentId, deployedAt]
    query-baseline:
      kind: tool
      dependsOn: [get-deployment]
      config:
        toolRef: telemetry.query-timeseries
        arguments:
          metric: application.errors
          start: "${nodes.get-deployment.output.deployedAt - inputs.baselineWindow}"
          end: "${nodes.get-deployment.output.deployedAt}"
    query-current:
      kind: tool
      dependsOn: [get-deployment]
      config:
        toolRef: telemetry.query-timeseries
        arguments:
          metric: application.errors
          start: "${nodes.get-deployment.output.deployedAt}"
          end: "${execution.now}"
    compare-rates:
      kind: function
      dependsOn: [query-baseline, query-current]
      config:
        source: functions/compare-rates.py
        entrypoint: run
        arguments:
          baseline: "${nodes.query-baseline.output}"
          current: "${nodes.query-current.output}"
      output:
        schema:
          type: object
          required: [relativeIncrease, sampleSufficient]
    regression-condition:
      kind: condition
      dependsOn: [compare-rates]
      config:
        expression: >
          nodes.compare-rates.output.sampleSufficient == true &&
          nodes.compare-rates.output.relativeIncrease >= inputs.minimumIncrease
    emit-finding:
      kind: emit
      dependsOn: [get-deployment, compare-rates, regression-condition]
      when: "${nodes.regression-condition.output == true}"
      config:
        finding:
          type: deployment-error-regression
          title: "Error regression after deployment for ${inputs.service}"
          dedupeKey: "${inputs.service}:${nodes.get-deployment.output.deploymentId}"
  outputs:
    finding:
      value: "${nodes.emit-finding.output}"
      required: false
  execution:
    concurrency: 1
    failureMode: fail-fast
    defaultTimeout: 5m
```

Note the shape:
- Stable, semantic node IDs (`get-deployment`, `query-baseline`) — never `tool-1`, `step-7`.
- Explicit `dependsOn`.
- Explicit expressions using `${inputs.x}`, `${nodes.y.output.z}`, `${execution.now}`.
- Explicit input contract with types and defaults.
- Explicit output contract per node (schema shape).
- Explicit capability contract (abstract IDs, not vendor tool names).
- Variation points declared up front.

## Compilation flow — walk these stages in order

### Stage 1 — Read and segment

Read the JSONL. Each line is a JSON object with a `type` field:

- `type: "user"` messages carry user text (from message.content text blocks) and tool_result blocks (message.content type=tool_result, linked by tool_use_id).
- `type: "assistant"` messages carry assistant text and `tool_use` blocks (message.content type=tool_use).
- Non-text content blocks — type=`image`, type=`document` — are **attachments**. Do NOT decode them. Note only their kind, mimeType (source.media_type), and size in bytes.

Produce a mental model of:
- **Objective**: first substantive user text (skip `<local-command-caveat>` prefixes and slash-command entries).
- **Tool calls**: ordered list of tool_use blocks with their ordinal, name, input, and paired tool_result content.
- **Attachments**: any image/document blocks; where they appear.
- **Conclusion**: last substantive assistant text block(s).

### Stage 2 — Classify each tool call (§29 stage 3)

For each tool call, assign exactly one:

- **REQUIRED** — produced evidence directly cited or used by the conclusion. Retain.
- **SUPPORTING** — needed for reproducibility or confidence but not directly cited. Retain when unclear.
- **EXPLORATORY** — hypothesis-driven probe that returned nothing useful or refuted a hypothesis. Omit from DAG; note in audit.
- **FAILED** — attempted but errored. Omit from DAG.
- **INCIDENTAL** — meta-work not part of the investigation. Includes any tool call whose input references `~/.claude/projects/` or that occurs after the terminal assistant conclusion. Omit.
- **LOCAL_ONLY** — depends on operator's local machine state that cannot be parameterized. Omit or convert to input.

Also determine, for each Bash call, its **synthetic capability ID** from `argv[0]`: `bash.grep`, `bash.kubectl`, `bash.git`, `bash.gh`, `bash.jq`, `bash.find`, `bash.ls`, `bash.cat`, `bash.mv`, `bash.curl`. Preserve the raw tool name; add the synthetic ID for capability binding.

### Stage 3 — Extract procedure signature (§25)

State the vendor-independent procedure the investigation followed. Example:

```
objectiveClass: metric-anomaly-explanation
evidencePattern:
  - metric-emission-source
  - metric-time-series
  - dimensional-breakdown
transformations:
  - identify-emission-code-path
  - group-by-dimensions
  - detect-stuck-values
judgments:
  - metric-correctness-classification
outputClass: metric-integrity-finding
```

If the investigation does not have a coherent procedure — for example, if it's task execution ("do X, then Y, then Z") rather than investigation ("why is X happening") — **stop here** and produce an audit report explaining why compilation is not appropriate. Do not force a Sentinel out of a task-execution session. This is the §40 negative-reuse discipline restated.

**How to tell:** the conclusion of an investigation *classifies* or *explains* ("the metric is stuck because...", "this is caused by X"). The conclusion of a task execution *reports actions* ("Done. Rebased X. Sent Y."). If your conclusion is the second shape, refuse compilation.

### Stage 4 — Synthesize the DAG (§29 stages 7–8)

Now produce YAML in the shape above. Rules:

- **Node kinds:** tool for external calls, function for deterministic transformations (comparing, filtering, parsing), condition for boolean gates, emit for findings. Reserve llm for genuinely analytical/classification steps where deterministic rules cannot express the judgment (§12).
- **Node IDs:** semantic and stable — describe what the node does, not its order. `query-error-baseline`, not `tool-4`.
- **Edges:** derive from actual data flow. If node B's input needs a value produced by node A, add A to B's dependsOn.
- **Inputs:** extract literals that look like inputs (service name, environment, time range, thresholds). Leave literals that are investigation-domain constants inline (metric names, code identifiers).
- **Attachments:** any attachment referenced by a retained action becomes a Sentinel input of type `image`, `pdf`, or `binary`, OR its human-derived inference becomes a plain input (e.g., a boolean "spike observed"). Never emit text purporting to describe the attachment as evidence.
- **Variation points:** declare which input bindings, tool bindings, thresholds, and node replacements should be exposed to future Variations.

### Stage 5 — Validate (§49 layers 1–4)

Check the emitted DAG for:

1. **Schema validity** — matches §8 shape.
2. **Referential validity** — every `dependsOn` target exists; every `${nodes.x.output.y}` reference resolves.
3. **Graph validity** — acyclic; declared outputs reachable.
4. **Type validity** — arguments look plausibly typed.

Plus semantic quality checks:

5. **Semantic drift** — does the DAG actually represent the original investigation? Or has the synthesis stage drifted into producing something plausible-looking but different?
6. **Honest LLM nodes** — is anything marked `kind: llm` that could plausibly be deterministic? If yes, prefer function.
7. **Attachment discipline** — did any attachment content get inlined as evidence text? If yes, remove it.

### Stage 6 — Iterate (max 3 rounds)

If validation reports issues:
- Round 1: fix all errors, re-validate.
- Round 2: fix remaining errors + as many warnings as reasonable, re-validate.
- Round 3: emit the best DAG you can plus an unresolved-issues section in the rationale.

If after 3 rounds the DAG is still invalid, emit what you have and a **failure report** listing what could not be resolved. **Do not** hide failures.

### Stage 7 — Emit outputs

Write three files to `OUT_DIR`:

1. `sentinel.yaml` — the final Sentinel candidate.
2. `rationale.md` — for each retained node: which tool_use ordinal(s) it derived from, why this node kind, what was preserved verbatim, what was generalized, what was guessed. Also list every tool call NOT retained with its classification and rationale.
3. `audit.jsonl` — per §47, one entry per capture event with the compiler's decision. Skip if too expensive for a spike — but the rationale.md is mandatory.

## What you MUST NOT do (§52 restated)

- Do NOT invent tool outputs. If a tool_result was empty or errored, mark that node's classification honestly.
- Do NOT describe an image's content as evidence. Ever.
- Do NOT produce a Sentinel from a task-execution session (see stage 3).
- Do NOT optimize for a large DAG. A 3-node Sentinel from a real investigation beats a 15-node Sentinel that pretends the operator's exploratory dead ends were required.
- Do NOT rename `tool-4` → `query-current` unless you can defend the semantic choice. Meaningful IDs > pretty IDs.
- Do NOT skip the rationale.md. Without it, no reviewer can tell whether the compilation was honest.

## Success criterion for this spike run

You produced:
- A `sentinel.yaml` that is structurally valid, OR a `failure-report.md` that honestly explains why compilation didn't complete.
- A `rationale.md` that a human reader could use to audit every compilation decision.

If a human reads the rationale and says "yes, this is what the investigation was, and here's why these nodes exist" — the spike succeeded. If they say "this is a plausible-looking DAG but doesn't match what I did" — the spike surfaced a compiler weakness, which is also success (for the spike, not for a shipped skill).
