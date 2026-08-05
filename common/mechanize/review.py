"""Taxonomy + base rubric library for the mechanize skill's Stage 9 review.

Stage 9 lives in the mechanize skill. Two subagents:
  9a  (warm)  — generates `rubric.md` = base rubric + per-sentinel appendix + ≥1 falsifier.
  9b  (cold)  — grades against `rubric.md`, writes `review.md` (per-item, no verdict word).

This module is a thin CLI helper the skill invokes to keep 9a's rubric grounded in
a taxonomy so different sentinels of the same class get comparable base coverage.
It does NOT run the review — the skill spawns the subagents.

Commands:
  classify <sentinel_dir>          -> bucket name (one of BUCKETS)
  base-rubric <bucket>             -> markdown rubric for that bucket
  nodes <sentinel_dir>             -> node inventory (id, kind, toolRef/source), one per line
  rubric-gen-instructions <dir>    -> prompt text for the Stage 9a subagent
  grade-instructions <dir>         -> prompt text for the Stage 9b subagent

Framework-free. Depends on PyYAML (same as ratification.py, lint.py).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

# Ordered by classifier priority — first bucket wins on tie.
BUCKETS = [
    "reconciliation",
    "regression",
    "security",
    "capacity",
    "misconfiguration",
    "availability",
    "general",
]

# Keyword sets per bucket. Matched (case-insensitive substring) against
# conclusionType, findingTypes on emit nodes, and the purpose summary/question.
KEYWORDS: dict[str, list[str]] = {
    "reconciliation": ["reconcil", "mismatch", "cross-source", "agrees", "delta"],
    "regression": ["regression", "baseline", "delta", "increase", "decrease", "change"],
    "security": ["security", "access", "exposure", "privilege", "unauthoriz", "leak"],
    "capacity": ["capacity", "saturation", "utilization", "headroom", "throttl", "quota", "limit"],
    "misconfiguration": ["misconfig", "drift", "declared vs actual", "config-drift"],
    "availability": ["availability", "uptime", "health", "healthy", "outage", "reachab", "error", "restart"],
}

# Base rubrics — deliberately short so 9a appends rather than rewrites.
# Each item is one question with an explicit pass criterion.
BASE_RUBRICS: dict[str, str] = {
    "capacity": """\
# Base rubric — capacity / saturation

- **Threshold justification.** Every utilization threshold has a defensible source (SLO, prior observation, vendor guidance) named in `rationale.md`. PASS iff every threshold input has a matching justification paragraph.
- **Headroom vs. alarm.** The Sentinel distinguishes "trending toward saturation" from "already saturated" — not a single binary threshold. PASS iff there are ≥2 threshold levels or the finding severity is derived from the utilization value, not a fixed word.
- **Aggregation window sanity.** Aggregation windows (peak, avg, p95) match the operational question (peak for burst detection, sustained-p95 for saturation risk). PASS iff each aggregation choice is either named in `rationale.md` or matches the input's declared purpose.
- **Sample sufficiency.** The Sentinel guards against zero-data or too-few-points false positives (an inconclusive branch, an emit gate on point count, or an explicit sufficiency check). PASS iff at least one guard exists.
- **Per-resource attribution.** Findings identify *which* resource is saturating (pod, container, service) — not just "the deployment." PASS iff the finding title or dedupeKey resolves to a specific resource.
""",
    "availability": """\
# Base rubric — availability / health

- **Signal freshness.** Every capability-derived signal has a bounded query window (`start`/`end` or explicit window input). Stale/unbounded queries FAIL. PASS iff every tool node's arguments include a bounded time range.
- **False-positive discipline.** The Sentinel handles known noise sources: rolling deploys (transient replica dip), scheduled restarts, in-progress rollouts. PASS iff `rationale.md` names at least one such source and the DAG handles it (inconclusive branch, minimum-duration guard, or condition-level exclusion), OR the reusable question explicitly narrows scope to steady-state.
- **Binary vs. graded state.** Health is expressed as a graded status (healthy/degraded/critical) or numeric score, not a single binary. PASS iff `overall` (or equivalent) has ≥3 states, OR the finding severity is derived from a numeric input, not a fixed word.
- **Signal independence.** Multi-signal composites don't double-count correlated signals (e.g., "restarts" and "pod uptime decreased" are the same event). PASS iff `rationale.md` acknowledges each signal's independence, or the composite function explicitly deduplicates.
- **Actionability.** The finding title tells the operator *what* is degraded, not just *that* it's degraded. PASS iff the title interpolates the specific signal or root cause, not just service name + status word.
""",
    "regression": """\
# Base rubric — regression assessment

- **Baseline window defensibility.** The baseline window length is either a declared input with a default the rationale defends, or matches the change event's expected effect duration. PASS iff `rationale.md` justifies the window length.
- **Same-shape comparison.** Baseline and current windows use identical query shape (same metric, same dimensions, same aggregation) — no apples-to-oranges. PASS iff the two tool nodes share config except for `start`/`end`.
- **Statistical sufficiency.** The Sentinel checks that both windows have enough data points to compare (an inconclusive branch or sample-size gate). PASS iff a sufficiency guard exists.
- **Effect-size threshold, not just direction.** The condition uses a minimum effect size (absolute or relative), not just "current > baseline." PASS iff the condition uses a threshold input, not a bare comparison.
- **Change-event attribution.** The finding names *what* changed (deployment id, config version) when possible, so the operator knows where to look. PASS iff the finding title or attributes carry the change event identifier.
""",
    "security": """\
# Base rubric — security / access

- **Scope containment.** The Sentinel operates over a bounded set of subjects (specific accounts, roles, resources) — not "everything the credential can see." PASS iff `spec.inputs` names the scope explicitly.
- **Evidence chain.** The finding carries enough evidence (source event id, timestamp, actor) for a human to independently verify the claim. PASS iff `emit.evidence` names ≥2 fields that a reviewer could cite.
- **False-positive discipline.** Known-benign patterns are handled (service accounts, break-glass workflows, expected admin actions). PASS iff `rationale.md` names at least one exclusion and the DAG applies it, OR the reusable question narrows scope past them.
- **Time-boundedness.** Every query has a bounded window; unbounded scans FAIL.
- **Severity discipline.** Severity is not `critical` for every finding — it varies with the specific violation. PASS iff severity is expression-derived or the reusable question narrowly covers one severity level.
""",
    "misconfiguration": """\
# Base rubric — misconfiguration / drift

- **Declared source of truth.** The "correct" configuration is named — a schema, a policy document, a known-good input — not implicit in the function body. PASS iff the target-config source is an input or explicitly cited in `rationale.md`.
- **Diff, not equality.** The Sentinel reports *what* drifted (field name + expected vs. actual), not just "different." PASS iff the finding evidence contains a structured diff or per-field comparison.
- **Drift vs. intentional change.** The Sentinel distinguishes drift from a legitimate recent change (deploy timestamp, PR reference, changelog). PASS iff the DAG or rationale names how it filters intentional changes.
- **Per-resource attribution.** Findings identify the specific misconfigured resource. PASS iff the finding title/dedupeKey resolves to a resource, not the whole cluster/environment.
""",
    "reconciliation": """\
# Base rubric — cross-source reconciliation

- **Same real-world quantity.** Both sources answer the *same* question about the *same* subject over the *same* window. PASS iff the two tool nodes share the subject (service, entity) and time window inputs.
- **Extraction discipline.** Count/quantity extraction is a `function` node (M2 pattern), not prose in the rationale or hand-computed inline. PASS iff a `function` node computes the comparison.
- **Explain field.** The finding carries a machine-readable diff (delta, both sides), not just "mismatch." PASS iff the emit evidence includes both source outputs and the computed diff.
- **Tolerance policy.** Small discrepancies aren't findings — the Sentinel has an absolute or relative tolerance, or the rationale explains why exact match is required. PASS iff a tolerance input exists or `rationale.md` justifies zero-tolerance.
- **Direction-blindness.** The Sentinel is honest about which side is authoritative (or that neither is). PASS iff `rationale.md` names the ground-truth relationship or explicitly declines to assign one.
""",
    "general": """\
# Base rubric — general (fallback)

- **Reusable question is specific.** The `reusableQuestion` names a concrete decision, not a topic area. PASS iff it ends in a question mark and could be answered "yes" or "no" from the DAG's outputs.
- **Judgments are honestly kinded.** Every `kind: llm` or `kind: ask_human` node has a `judgmentJustification` explaining why it isn't `kind: function`. Structural lint checks this (R3), so PASS unless the reviewer sees an obviously-deterministic transformation smuggled as an LLM node.
- **Findings are actionable.** Every emit node's title tells the operator *what* to investigate, not just *that* something happened. PASS iff titles interpolate specific context.
- **Inputs are exercised.** Every declared input under `spec.inputs` is referenced somewhere in the DAG (a tool arg, function arg, condition expression, or emit template). PASS iff no orphan inputs.
- **Rationale matches the DAG.** For each node, `rationale.md` explains why it exists and what it decides. PASS iff every node has a rationale entry that names its purpose in the investigation.
""",
}


def _load_sentinel(sentinel_dir: Path) -> dict:
    path = sentinel_dir / "sentinel.yaml"
    if not path.exists():
        raise SystemExit(f"sentinel.yaml not found in {sentinel_dir}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _emit_finding_types(nodes: dict) -> list[str]:
    out = []
    for spec in nodes.values():
        if spec.get("kind") != "emit":
            continue
        finding = ((spec.get("config") or {}).get("finding") or {})
        typ = finding.get("type")
        if typ:
            out.append(str(typ))
    return out


def _score_bucket(bucket: str, blob_hi: str, blob_lo: str) -> int:
    kws = KEYWORDS.get(bucket, [])
    score = 0
    for kw in kws:
        if kw in blob_hi:
            score += 3
        if kw in blob_lo:
            score += 1
    return score


def classify(sentinel_dir: Path) -> str:
    sentinel = _load_sentinel(sentinel_dir)
    spec = sentinel.get("spec") or {}
    purpose = spec.get("purpose") or {}
    nodes = spec.get("nodes") or {}

    conclusion = str(purpose.get("conclusionType") or "").lower()
    finding_types = " ".join(_emit_finding_types(nodes)).lower()
    blob_hi = conclusion + " " + finding_types
    blob_lo = (
        str(purpose.get("summary") or "").lower()
        + " "
        + str(purpose.get("reusableQuestion") or "").lower()
    )

    best_bucket = "general"
    best_score = 0
    for bucket in BUCKETS:
        if bucket == "general":
            continue
        s = _score_bucket(bucket, blob_hi, blob_lo)
        if s > best_score:
            best_score = s
            best_bucket = bucket
    return best_bucket


def base_rubric(bucket: str) -> str:
    if bucket not in BASE_RUBRICS:
        raise SystemExit(f"unknown bucket: {bucket}. valid: {', '.join(BASE_RUBRICS)}")
    return BASE_RUBRICS[bucket]


def nodes_inventory(sentinel_dir: Path) -> str:
    sentinel = _load_sentinel(sentinel_dir)
    nodes = (sentinel.get("spec") or {}).get("nodes") or {}
    lines = []
    for name, spec in nodes.items():
        kind = spec.get("kind", "?")
        cfg = spec.get("config") or {}
        extra = ""
        if kind == "tool":
            extra = f"  toolRef={cfg.get('toolRef','')}"
        elif kind == "function":
            extra = f"  source={cfg.get('source','')}"
        elif kind == "emit":
            finding = cfg.get("finding") or {}
            extra = f"  findingType={finding.get('type','')}"
        lines.append(f"{name}  {kind}{extra}")
    return "\n".join(lines)


def rubric_gen_instructions(sentinel_dir: Path) -> str:
    bucket = classify(sentinel_dir)
    base = base_rubric(bucket)
    inv = nodes_inventory(sentinel_dir)
    return f"""\
You are Stage 9a of the mechanize compilation flow: RUBRIC GENERATION.

Your job: write `{sentinel_dir}/rubric.md` — a semantic-quality rubric a cold
reviewer (Stage 9b) will use to grade this Sentinel. You DO have access to the
compile context — use it to write a rubric that's specific to what this
Sentinel actually does. The cold reviewer will not have that context.

Inputs available to you:
- `{sentinel_dir}/sentinel.yaml` — the Sentinel DAG
- `{sentinel_dir}/rationale.md` — the compiler's justifications
- `{sentinel_dir}/functions/` — generated function bodies
- Node inventory (below)
- Base rubric for this Sentinel's class (below)

This Sentinel was classified as: **{bucket}**

Base rubric — reproduce these items verbatim as section 1 of `rubric.md`.
Do NOT edit or remove any base item. Cross-Sentinel comparability depends
on the base being stable across Sentinels of the same class.

```
{base}```

After the base rubric, add section 2: **Appendix — specific to this Sentinel.**
Add 2–5 items that target THIS Sentinel's specific claims and structure.
Look at the actual node ids, capability choices, function bodies, and
rationale. Ask the questions a domain expert would ask about *this* one —
the base rubric can't. Each appendix item MUST include:
- A one-line question
- An explicit PASS criterion (e.g., "PASS iff every occurrence of X ...")
- A pointer to the specific node(s) or file(s) it targets

Section 3 is mandatory: **Falsifier.** Include AT LEAST ONE item that
the Sentinel, as compiled, plausibly FAILS. This is a hedge against
rubric softness — a rubric with only pass-shaped items is theater.
Frame the falsifier as a question the reviewer should investigate, not
a prosecution. Anchor it to specific evidence (node id, function body
line, rationale claim).

Write `rubric.md` and nothing else. Do not run the review yourself —
Stage 9b (a cold subagent) does that. Your rubric is the artifact.

Node inventory (for your reference — DO NOT paste into rubric.md):
```
{inv}
```
"""


def grade_instructions(sentinel_dir: Path) -> str:
    return f"""\
You are Stage 9b of the mechanize compilation flow: COLD GRADING.

You have NO context on how this Sentinel was compiled. You have not seen
the source session, the compiler's reasoning, or any prior conversation.
This is intentional — a cold read catches blind spots a warm reader inherits.

Your job: write `{sentinel_dir}/review.md` grading each item in
`{sentinel_dir}/rubric.md` against the Sentinel directory:
- `{sentinel_dir}/sentinel.yaml`
- `{sentinel_dir}/rationale.md`
- `{sentinel_dir}/functions/`

For each rubric item, emit:
- The item title (verbatim from rubric.md)
- Verdict: `PASS`, `FAIL`, or `PARTIAL` (with the specific gap)
- Evidence: pointer to the node id, function file line, or rationale
  section that supports your verdict. Every non-PASS verdict MUST cite
  specific evidence — a bare "FAIL" without evidence is not a review.
- If FAIL or PARTIAL: a one-line suggested fix (e.g., "add a
  sample-sufficiency check between compute-health-summary and
  degraded-condition").

Group your output by rubric section (Base / Appendix / Falsifier).

DO NOT emit an overall verdict word (no APPROVE, REVISE, or REJECT at the
top). A single-word verdict trains readers to skim past the details.
Emit per-item comments only. The human reader decides what to do with the
pattern of PASS/FAIL/PARTIAL across items.

If the rubric asks a question you genuinely cannot answer from the
directory alone (missing evidence, ambiguous DAG), verdict is `PARTIAL`
and evidence names what would be needed.
"""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "classify":
        if len(argv) != 3:
            print("usage: review.py classify <sentinel_dir>", file=sys.stderr)
            return 2
        print(classify(Path(argv[2]).resolve()))
        return 0
    if cmd == "base-rubric":
        if len(argv) != 3:
            print("usage: review.py base-rubric <bucket>", file=sys.stderr)
            return 2
        print(base_rubric(argv[2]))
        return 0
    if cmd == "nodes":
        if len(argv) != 3:
            print("usage: review.py nodes <sentinel_dir>", file=sys.stderr)
            return 2
        print(nodes_inventory(Path(argv[2]).resolve()))
        return 0
    if cmd == "rubric-gen-instructions":
        if len(argv) != 3:
            print("usage: review.py rubric-gen-instructions <sentinel_dir>", file=sys.stderr)
            return 2
        print(rubric_gen_instructions(Path(argv[2]).resolve()))
        return 0
    if cmd == "grade-instructions":
        if len(argv) != 3:
            print("usage: review.py grade-instructions <sentinel_dir>", file=sys.stderr)
            return 2
        print(grade_instructions(Path(argv[2]).resolve()))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
