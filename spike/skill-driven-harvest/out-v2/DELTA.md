# SKILL-v2 delta report — spike-2 second run

Compiling capture `e470b9a9-8ccb-4059-a4f0-13da5373b70c` (lakerunner worklane depth investigation) blind against SKILL-v2 vs. SKILL v1's captured output in `spike/skill-driven-harvest/out/`.

## Skill changes (SKILL-v2 vs SKILL v1)

Per-gap:

**Gap 1 — Spill-to-disk collapsing (F3 refinement).**
Added new **Stage 1.5** ("Recognize spill-to-disk pairs"). Rules: (a) recognize `Output has been saved to <path>` markers in tool_results; (b) treat a subsequent `bash.jq/cat/head/tail/python` call whose input path is exactly that spill path as a spill-projection and collapse into the preceding node; (c) discriminator — an `~/.claude/` path IS meta only when no preceding tool_result declared it as a spill. Added new **COLLAPSED** classification in Stage 2. Added spill-projection collapse discipline check as Validation item 8 in Stage 5.

**Gap 2 — Code-reading compression.**
Added new **Stage 2.5** ("Code-reading compression policy"). Three-option chooser (A: LLM node with directory access; B: function node extracting typed constants; C: compression into structured operator inputs, v1's default) plus explicit tradeoffs and a three-question decision procedure. Requires the rationale name the option chosen and state fidelity loss.

**Gap 3 — Expression language.**
Added new **Expression language** section clarifying §13 governs *condition* expressions specifically. Enumerated subset B for tool-argument and finding-body expressions: `join(array, separator)`, `format(template, ...args)`, string concatenation via interpolation, arithmetic on time/numeric inputs, and ternary in `severityExpression`. Documented as a spec-clarification-needed flag with a pragmatic v2 ruling.

**Gap 4 — Node-ID stability under iteration.**
Rewrote **Stage 6**. Rule: node IDs are FROZEN after Round 1. Rounds 2 and 3 may not rename. A rename that would materially improve the ID is a Stage 4 decision that resets the freeze (with an explicit restart). Added `Node-ID style guide` section with meaningful/pretty/ordinal examples.

**Gap 5 — Attachment chooser.**
Added new **Stage 4.5** ("Attachment chooser") with an ordered Q1→Q4 decision procedure covering all four options from §29 stage 3. Q1: operator-inference-became-input → replaced-by-plain-input. Q2: runtime needs the attachment → input-typed. Q3: nice-to-have context → requires-manual-input OR omit. Q4: never describe attachment content as evidence. Rationale must record which question fired.

Additional meta-feedback fixes:
- Added **prior spike output — explicit policy** paragraph: MAY read `spike/dag-harvest/out/*` for cross-checks; MAY NOT read `spike/skill-driven-harvest/out/*` before writing own compilation.
- Added **Node-ID style guide** section with concrete "meaningful vs pretty vs ordinal" examples.

## Output diffs (out-v2 vs out)

### Node count and kinds

| Metric | v1 (out/) | v2 (out-v2/) |
|---|---|---|
| Total nodes | 6 | 11 |
| tool | 3 | 4 |
| llm | 0 | 1 |
| function | 1 | 2 |
| condition | 1 | 2 |
| emit | 1 | 2 |

### Node-by-node

| v1 node | v2 node | Change |
|---|---|---|
| `query-metric-timeseries` | `query-metric-timeseries-baseline` | renamed (clarifies role) |
| `query-metric-by-dimensions` | `query-metric-by-dimensions` | identical |
| `locate-metric-emission-context` | `locate-metric-emission-code` | renamed (verb reflects intent) |
| — | `query-related-counter-by-labels` | NEW: promotes v1's "variation extension" (#18) into a first-class OPTIONAL node |
| — | `interpret-metric-semantics-from-code` | NEW: LLM node encoding the code-reading judgment (Stage 2.5 Option A hybrid) |
| `detect-degeneracy` | `detect-degeneracy` | identical |
| — | `detect-counter-label-dominance` | NEW: function for L118-item-2 reasoning |
| `degeneracy-condition` | `degeneracy-condition` | identical |
| — | `capacity-starvation-condition` | NEW: condition for capacity-starvation branch |
| `emit-degeneracy-finding` | `emit-degeneracy-finding` | evidence array grew (optional refs to LLM + grep) |
| — | `emit-capacity-starvation-finding` | NEW: second finding for capacity-starvation branch |

### Retained-vs-omitted tool-call decisions

| Tool call | v1 decision | v2 decision |
|---|---|---|
| #1 grep bare-token (empty) | EXPLORATORY, omitted | EXPLORATORY, omitted — same |
| #2 grep metric name | SUPPORTING → `locate-metric-emission-context` (OPTIONAL) | SUPPORTING → `locate-metric-emission-code` (OPTIONAL) — same role |
| #3, #4, #5, #6, #8, #9 code reading | SUPPORTING, all omitted; encoded implicitly in inputs | SUPPORTING, all still omitted as tool nodes, but now feed the OPTIONAL LLM node via `locate-metric-emission-code` grep matches |
| #7 git log | EXPLORATORY | EXPLORATORY — same |
| #10 ToolSearch | INCIDENTAL | INCIDENTAL — same |
| #11 kubectl (failed SSO) | FAILED | FAILED — same |
| #12, #13 kube list (empty) | FAILED | FAILED — same |
| #14 namespace list | EXPLORATORY | EXPLORATORY — same |
| #15 list_instances | EXPLORATORY (collapsed into input) | EXPLORATORY (collapsed into input) — same |
| #16 grep "oportun" | EXPLORATORY | EXPLORATORY — same, plus Stage 1.5 discriminator record (spill exists but no follow-up projection → NOT collapsed) |
| #17 baseline query | REQUIRED → `query-metric-timeseries` | REQUIRED → `query-metric-timeseries-baseline` — same intent |
| #18 counter query | REQUIRED (deferred to Variation extension) | REQUIRED → `query-related-counter-by-labels` (first-class OPTIONAL node) — **promoted** |
| #19 dimensional query | REQUIRED → `query-metric-by-dimensions` | REQUIRED → `query-metric-by-dimensions` — same |
| #20 jq on #19 spill | REQUIRED (collapsed, editorial call) | COLLAPSED (mechanical via Stage 1.5 rule) |
| #21 jq on #18 spill | REQUIRED (collapsed, editorial call) | COLLAPSED (mechanical via Stage 1.5 rule) |

**Structural difference on collapse:** v1 called out the collapse of #20/#21 as an editorial judgment in prose. v2 formalizes it via a new `provenance.collapsedSteps[]` array in the Sentinel with `collapsedInto: <node-id>` and `reason: spill-projection`. Same outcome, different rigor.

### Attachment handling

| Aspect | v1 | v2 |
|---|---|---|
| Chosen disposition | replaced-by-plain-input | replaced-by-plain-input |
| Chooser question fired | (not recorded — no procedure) | Q1 (recorded explicitly in provenance) |
| Rationale for the choice | listed as one of four options, spike picked the cheaper | Q1 fired first per Stage 4.5 (operator inference became downstream inputs) |

Same outcome. v2 gives the reasoning as a mechanical procedure that a reviewer can rerun against a different attachment.

### Reusable question scope

| Aspect | v1 | v2 |
|---|---|---|
| Reusable question | "Is this metric anomaly real, or is the metric itself degenerate?" | "Why did this metric spike — is the metric itself trustworthy, and if so, what workload pattern in a related counter explains the correlated signal?" |
| L118 branch 2 (capacity-starvation) | Moved to Variation extension | First-class second branch in the DAG |
| Node count | 6 (narrow scope) | 11 (wider scope) |

**Trade:** v2 favors fidelity to L118's two-branch conclusion; v1 favors scope discipline per §52 ("optimize for the largest semantically valid procedure" — v1 argued the integrity check is the shared procedure, v2 argues both branches are). Both are defensible; SKILL-v2 does not force this choice.

### Provenance shape

v2 adds `provenance.collapsedSteps[]` (new field, not in v1 or in §8 example). v1 folded collapse notes into `omittedSteps` and prose. v2's separate array is a spec-suggestion (the spec's §8 example has only `retainedSteps` and `omittedSteps`; a real compiler probably needs three categories).

### Expression language

v1 used `join(inputs.dimensionalBreakdown, ", ")` and flagged it as an unresolved spec question. v2 uses the same `join(...)` and additionally uses ternary `? :` in `severityExpression` (matching the §8 canonical example). v2 documents both as subset B per SKILL-v2's Expression language section and flags spec-clarification-needed. Same set of expression features used; v2 has explicit spec-language backing.

## New unresolved (surfaced by v2, not in v1's meta-feedback)

1. **Multi-branch reusable question templating.** SKILL-v2's Stage 3 assumes a single procedure signature per Sentinel. This compilation has two coupled branches (integrity classification + workload characterization) and had to invent an ad-hoc structural shape (parallel probes → two conditions → two emits with cross-guard). Future SKILL should include a canonical template for this multi-branch case.

2. **OPTIONAL LLM branch cost bookkeeping.** When `codeRepoPath` is set, `interpret-metric-semantics-from-code` runs and consumes budget. SKILL-v2 does not tell the compiler how to budget optional LLM nodes. `maxCost.llmTokens: 10000` in v2's YAML is a guess.

3. **Cross-guard between branches.** v2's `capacity-starvation-condition` includes `nodes.degeneracy-condition.output == false` so a degenerate metric doesn't emit a workload finding. This is a genuine multi-branch pattern that SKILL-v2 doesn't discuss but a general compiler will meet whenever two branches classify the same underlying phenomenon.

4. **`provenance.collapsedSteps[]` field.** New field introduced by v2 to record spill-projection collapses distinctly from omitted steps. Not in the §8 canonical shape. Spec question: is this a first-class provenance category, or should collapsed steps be a sub-category of retainedSteps (since they DID contribute to a retained node's output)?

5. **Function-node source/fixture generation not addressed.** v2 declares two function nodes with `source:` paths but does not emit those Python files or their fixtures. SKILL-v2 (like v1) stops at Stage 7 emit; §29 stages 8+ are out of scope. A shipping compiler needs a companion skill.

6. **Optional-node `emit-degeneracy-finding` evidence with all-optional upstream.** When neither `codeRepoPath` nor the LLM node runs, `emit-degeneracy-finding.evidence[]` has two `optional: true` entries that will be null at runtime. Runtime semantics per §17 must tolerate this cleanly. v2 assumes so; not spec-verified.
