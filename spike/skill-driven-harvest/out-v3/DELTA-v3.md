# DELTA — v3 vs v2 vs ground truth

Compiled from the same Session A JSONL, blind (v2/GT/EVAL/SCORING
files were not read before v3 was written). Assesses whether the
amended §32 (three-way analytical selection, no-optional-analytical,
causal-cross-source LLM-safety test) propagated through SKILL-v2
into different compilation decisions.

## 1. Spec-change effects observed

### §32 rule (i) — three-way analytical selection

**Applied? Yes, everywhere.** Every judgment step recorded an
explicit walk of function → llm → ask_human, and the rationale's
`## Analytical-node decisions per amended §32` section reproduces
the decision procedure for the four candidates:

| Judgment | Decision | Rule that fired |
|---|---|---|
| `detect-histogram-degeneracy` | function | Rule 1 (deterministic) |
| `classify-trigger-pattern` | function | Rule 1 (deterministic) |
| `interpret-metric-semantics` | llm | Rule 2 (LLM-safe qualitative) |
| `assess-findings-relationship` | llm | Rule 2 (LLM-safe qualitative) |

**Did this change decisions vs v2?**

- `detect-histogram-degeneracy` (v2 called it `detect-degeneracy`): **no change** — v2 also chose function; v3's justification is more explicit under the amended §32.
- `interpret-metric-semantics`: **kind unchanged** (both llm) but
  **essentiality changed** — v2 made this node OPTIONAL
  (`when: "${inputs.codeRepoPath != null}"`); v3 makes it REQUIRED
  because §32's non-existence-of-optional-analytical rule prohibits
  gating an llm node on a `when:`. This is the biggest concrete
  spec-driven divergence.
- `assess-findings-relationship`: **NEW node in v3**, absent in v2.
  v2 stopped at deterministic per-branch functions plus a cross-
  guard condition; v3 adds a required llm node explicitly to satisfy
  the "operator made a synthesis claim across two findings" fidelity.
  Both §32 rule (iii) (causal cross-source LLM-safety) and rule (ii)
  (no-optional-analytical implies "if you want the claim mechanized,
  it's a required node") drove this.

### §32 rule (ii) — non-existence of optional analytical nodes

**Applied? Yes, and it changed one decision.** v3 removed both `when:`
gates that v2 had on analytical nodes:

- v2: `interpret-metric-semantics-from-code` had
  `when: "${inputs.codeRepoPath != null}"` → v3 REJECTED. `codeRepoPath`
  is now a required input; the llm node is required. A future variant
  that skips this judgment is a **Variation** (remove the node and
  add `dimensionsExpectedToVary` as an input), which is exactly the
  §32-allowed pattern.
- v2: `capacity-starvation-condition` chained through `detect-counter-
  label-dominance` whose upstream had `when: "${inputs.relatedCounterMetric != null}"`.
  Not analytical, but the pattern encouraged a mindset v3 rejects.
  v3 makes the counter query required and the trigger metric a
  required input.

### §32 rule (iii) — causal-cross-source LLM-safety test

**Applied? Yes, and it added a node.** v3's `assess-findings-relationship`
ties evidence from two analytical branches (`detect-histogram-degeneracy`
and `classify-trigger-pattern`) into a causal claim ("these are
independent, or same-root-cause, or one-is-artifact"). The test:

- Qualitative in nature? YES — cross-metric causal classification.
- Autonomous delegation acceptable? YES — flows into an emit node's
  attribute, no destructive downstream.

Both conditions passed → llm. Recorded in the rationale.

v2 skipped this node entirely; the operator's synthesis claim was
mechanized as a cross-guard `!degeneracy-condition.output` in
`capacity-starvation-condition`, which is a *deterministic rule* not
a *causal claim*. That was the v2 miss (SCORING Miss 2, resolved by
the amended §32).

## 2. `ask_human` usage

**Did v3 materialize any `ask_human` nodes? No.** But the enumeration
is explicit in the rationale under `## Ask-human enumeration
(v3-specific)`. Every candidate was considered against the LLM-safety
test and each resolved to LLM or to "encode as input" (attachment
Q1 disposition, instance selection).

Why no ask_human materialized:

- **Interpret-metric-semantics** → llm, not ask_human. The operator's
  code-reading was information work, not accountability. Wrong output
  → wrong finding, not destructive.
- **Assess-findings-relationship** → llm, not ask_human. Same
  reasoning — produces an attribute on a finding, no destructive
  downstream.
- **Instance selection** → input (`instance` required, default "prod").
  Runtime confirmation would slow every execution for no
  accountability gain.
- **Metric selection** → Attachment chooser Q1 fired; the screenshot's
  extracted claim became typed inputs (`metricName`, `service`).
- **Whether to emit the degeneracy finding** → v0 emits are
  non-destructive (§14: stdout / JSON file / webhook). No ratification
  required.

v3's rationale explicitly flags the **future** case where ask_human
IS the right answer: if a Variation makes the emit path destructive
(auto-file a bug against the metric-emission code, page oncall,
disable an alerting rule), the §32 rule-2 safety test would then fail
for the upstream analytical LLM nodes, and an ask_human node must be
inserted between the llm and the destructive emit. This is §14a
being honest about the v0/v1 boundary.

## 3. Node-by-node diff — v2 vs v3 vs GT

Names differ across the three; the table maps by semantic role.

| Semantic role | v2 node | v3 node | GT node | v2 kind | v3 kind | GT kind | Notes |
|---|---|---|---|---|---|---|---|
| Locate emission code | `locate-metric-emission-code` (opt) | `locate-metric-emission-site` (req) | `locate-driver-emission` (req) | tool | tool | tool | v3 removed optionality per §32; matches GT essentiality |
| Read emission source | — (absorbed into LLM evidence) | `read-emission-code` (req) | (LLM has `code.read` tool access) | — | tool | (tool-in-llm) | GT gave LLM tool access; v3 externalized the read as its own tool node |
| Interpret metric semantics | `interpret-metric-semantics-from-code` (opt LLM) | `interpret-metric-semantics` (req LLM) | `summarize-driver-semantics` (req LLM) | llm | llm | llm | v3 matches GT on essentiality; v3 lacks GT's inline `code.read` capability access |
| Query aggregate baseline | `query-metric-timeseries-baseline` | `query-metric-aggregate` | `query-driver-timeseries` | tool | tool | tool | present in all three |
| Query per-dimension | `query-metric-by-dimensions` | `query-metric-by-dimension` | `query-driver-by-dimensions` | tool | tool | tool | present in all three; GT's dependsOn wires it AFTER the LLM summary (uses LLM's `dimensionsEmitted`) |
| Query trigger/alt counter | `query-related-counter-by-labels` (opt) | `query-trigger-rate-by-dimension` (req) | `query-alternative-drivers` (opt, integrity-gated) | tool | tool | tool | v3 removed optionality; GT keeps optionality on integrity-failure gate (Variation-flavored) |
| Detect degeneracy | `detect-degeneracy` | `detect-histogram-degeneracy` | `detect-degeneracy` | function | function | function | all three agree; kind unchanged by amended §32 |
| Detect counter pattern | `detect-counter-label-dominance` | `classify-trigger-pattern` | (folded into LLM `explain-real-signal`) | function | function | — | v2/v3 kept deterministic; GT went LLM |
| Cross-signal causal synthesis | (encoded as cross-guard in condition) | `assess-findings-relationship` | `explain-real-signal` | condition (implicit) | **llm (NEW)** | llm | **v3 closes v2's Miss 2** via amended §32 rule (iii) |
| Degeneracy condition | `degeneracy-condition` | `degeneracy-finding-condition` | `integrity-condition` | condition | condition | condition | rename in v3 |
| Capacity/starvation condition | `capacity-starvation-condition` | `starvation-finding-condition` | (implicit in gates on emit branches) | condition | condition | — | rename in v3; GT drops the explicit condition and puts logic in emit `when:` |
| Emit degeneracy | `emit-degeneracy-finding` | `emit-degeneracy-finding` | (part of `emit-explanation`) | emit | emit | emit-part | v3 keeps two-emit shape (Miss 3 not closed) |
| Emit workload cause | `emit-capacity-starvation-finding` | `emit-starvation-finding` | (part of `emit-explanation`) | emit | emit | emit-part | v3 keeps two-emit shape |
| Emit driver-confirmed | — | — | `emit-driver-confirmed` | — | — | emit | GT has three emit-branch coverage; v3 has two |
| Emit inconclusive | — | — | `emit-inconclusive` | — | — | emit | GT has three emit-branch coverage; v3 has two |

Totals: v2 = 11 nodes (1 llm), v3 = **13 nodes (2 llm, 2 function,
5 tool, 2 condition, 2 emit)**, GT = 10 nodes (2 llm, 1 function).

## 4. Miss-closure scorecard

### Miss 1 — Code-reading essentiality (SCORING axes B/C/J)

- **v2:** `codeRepoPath: required: false`; LLM node OPTIONAL; code-
  reading tool calls all omitted.
- **v3:** `codeRepoPath: required: true`; LLM node REQUIRED; **two**
  code-reading tool nodes retained (`locate-metric-emission-site`,
  `read-emission-code`).
- **GT:** `codeRepoPath: required: true`; LLM node REQUIRED with
  inline `code.read` tool access; grep+read calls RETAINED as
  feeding that node.

**v3 status: CLOSED.** The amended §32 rule (ii) drove this directly
— the pattern "input optional → LLM optional" is now prohibited.
v3 doesn't have GT's exact structural shape (v3 externalizes the
`code.read` step as its own tool node; GT wraps it inside the LLM
node's tool access), but the essentiality gap is fully closed.

### Miss 2 — Second LLM node absent (SCORING axis E)

- **v2:** no second LLM node; cross-signal synthesis reduced to a
  cross-guard condition (`!degeneracy-condition.output`).
- **v3:** `assess-findings-relationship` (LLM) added, explicitly
  justified against §32 rule (iii)'s causal-cross-source test.
- **GT:** `explain-real-signal` (LLM) present, ties alt-driver
  timeseries + emission semantics into a proposed real driver.

**v3 status: CLOSED (in kind, not in exact framing).** v3's node
answers "are these two findings the same phenomenon, independent, or
is one an artifact?" GT's node answers "given the integrity check
failed and here are alternative drivers, which is the real cause?"
Both are LLM synthesis over ≥2 tool-node evidences; both pass the
LLM-safety test; both are required. Framing difference reflects
Miss 3, not a §32 gap.

### Miss 3 — Two findings vs unified (SCORING axis F)

- **v2:** two separate emits (`emit-degeneracy-finding`, `emit-
  capacity-starvation-finding`) with cross-guard.
- **v3:** two separate emits (`emit-degeneracy-finding`, `emit-
  starvation-finding`). Same shape as v2, no cross-guard because
  `assess-findings-relationship` now writes a `relationshipToWorkload`
  attribute on both findings so a reader can see the coupling.
- **GT:** one two-part `emit-explanation` finding with
  `parts.integrityAssessment` and `parts.realDriverHypothesis`.

**v3 status: SAME AS V2 (still open).** The amended §32 does not
speak to emit-shape. SCORING recommended a new Stage 4.6 for emit
unification; that guidance isn't in SKILL-v2, so this compilation
still emits two findings. Partial mitigation: v3's shared
`assess-findings-relationship.output.relationship` attribute is
copied onto both findings, so a reader who sees both findings gets
the coupling narrative — but a reader who sees only one loses it,
which is exactly GT's objection.

## 5. Was SKILL-v2 sufficient?

**Mostly, but not entirely.** SKILL-v2's Stage 4 / Stage 2.5 did
successfully translate two of the three amended §32 rules:

- Rule (i) three-way selection: SKILL-v2's Stage 4 already said
  "reserve llm for genuinely analytical/classification steps where
  deterministic rules cannot express the judgment" — the amended
  §32 adds ask_human as a third option and formalizes the
  decision procedure. **The SKILL does not enumerate ask_human at
  all** in Stage 4 or Stage 2.5. v3 succeeded here by reading §32
  directly, but a compiler that only reads the SKILL would miss
  ask_human. **SKILL edit needed:** Stage 4 must enumerate all
  three kinds explicitly and reference §32's `judgmentJustification`
  structure.
- Rule (ii) non-existence of optional analytical: **not in the
  SKILL at all.** SKILL-v2 Stage 2.5 Option A explicitly permits
  a hybrid "LLM available if repo provided" pattern, which the
  amended §32 now prohibits. Stage 2.5 needs to be rewritten:
  the three options collapse under the amended rule to (a) required
  LLM in base, (b) input replacing LLM in base, (c) Variation
  adding/removing the LLM node. The "optional gated" hybrid is no
  longer available. **SKILL edit needed:** Stage 2.5 must be
  rewritten to match §32.
- Rule (iii) causal-cross-source LLM-safety test: **not called out
  in the SKILL.** v3 succeeded here by reading §32 directly. A
  compiler that only reads the SKILL might miss it (SCORING already
  identified this as Miss 2's root cause). **SKILL edit needed:**
  add a Stage 4 sub-rule: "if the judgment ties evidence from ≥2
  tool nodes into a causal claim, run the LLM-safety test — it is
  almost always llm."

Concrete SKILL-v3 edit list, in priority order:

1. **Stage 4 rewrite** — enumerate function / llm / ask_human as
   the three choices; embed §32's decision procedure verbatim;
   require the rationale to record `judgmentJustification` for
   every non-function node.
2. **Stage 2.5 rewrite** — Options A/B/C collapse. New options:
   (A) required LLM in base + REQUIRED code-repo input; (B)
   deterministic constant-extraction function (unchanged);
   (C) drop the LLM entirely, encode operator conclusion as an
   input, mention Variation as the way to reintroduce the judgment.
   The hybrid "optional LLM gated on input" is REMOVED.
3. **New Stage 4.5.5** (or fold into Stage 4) — enumerate ask_human
   candidates explicitly: destructive emit downstream, high-stakes
   ratification, source investigation had a human-in-the-loop
   moment that a re-run must reproduce. Give a concrete negative
   example (v0-shape emits = no ask_human) alongside a concrete
   positive example (auto-file-bug-against-emission-path = ask_human
   before the emit).
4. **New Stage 4.6** — emit-unification guidance (still not in the
   amended spec, so this is a SKILL-only ruling). SCORING already
   proposed the shape.

Without edits 1–3, a fresh SKILL-v2 compiler would repeat v2's
mistakes even against the amended spec. v3 succeeded only because
the operator (me, mid-task) read the amended spec sections directly
and applied them in Stage 4 rather than relying on SKILL-v2's
pre-amendment guidance.

## Summary line

v3 differs from v2 in three concrete places driven by amended §32:
(a) code-reading LLM node made required (Miss 1 closed), (b)
new cross-signal synthesis LLM node added (Miss 2 closed), (c)
zero `when:` gates on analytical nodes (rule ii applied). No
`ask_human` materialized — all candidates resolved to input,
attachment-input, or LLM under the safety test. Miss 3 (unified vs
split emit) unchanged because it is a spec-adjacent question the
amended §32 does not answer. SKILL-v2 needs three edits before a
fresh compiler could exploit the amended spec without direct
spec-reading.
