#!/usr/bin/env python3
"""
Spike: extract a naive Sentinel-shaped DAG from a Claude Code session JSONL.

Purely heuristic. No LLM. Deterministic. Deliberately crude — every place the
heuristic guesses is a place the real compiler will need help.

Output per session:
  session-<label>-naive-dag.yaml   # scrappy §8-shaped DAG
  session-<label>-report.md        # what the heuristic did, guessed, or dropped
"""
import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


MIN_MATCH_LEN = 12       # whole-string substring: at least this long
MIN_TOKEN_LEN = 6        # token-level substring: at least this long
TOKEN_SPLIT_RE = re.compile(r"[^A-Za-z0-9_./]+")
STOPWORD_TOKENS = frozenset({
    "true", "false", "null", "none", "return", "import", "package",
    "function", "class", "public", "private", "static", "const", "await",
    "async", "string", "number", "boolean", "object", "unknown",
})


def load_jsonl(path):
    events = []
    with path.open() as fh:
        for i, line in enumerate(fh):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"type": "parse-error", "raw_line_no": i})
    return events


def is_user_msg(e):
    return e.get("type") == "user"


def is_assistant_msg(e):
    return e.get("type") == "assistant"


def iter_content_blocks(msg):
    content = msg.get("content")
    if isinstance(content, list):
        yield from content
    elif isinstance(content, str) and content.strip():
        yield {"type": "text", "text": content}


def extract_objective(events):
    for e in events:
        if not is_user_msg(e):
            continue
        for block in iter_content_blocks(e.get("message") or {}):
            if block.get("type") != "text":
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue
            if text.startswith("<local-command-caveat>"):
                continue
            if text.startswith("<command-name>"):
                continue
            if text.startswith("/"):  # slash command invocation
                continue
            return text
    return None


def extract_conclusion(events):
    for e in reversed(events):
        if not is_assistant_msg(e):
            continue
        texts = []
        for block in iter_content_blocks(e.get("message") or {}):
            if block.get("type") == "text":
                t = (block.get("text") or "").strip()
                if t:
                    texts.append(t)
        if texts:
            return "\n\n".join(texts)
    return None


def extract_tool_events(events):
    tool_uses = []
    tool_results = {}
    for i, e in enumerate(events):
        if is_assistant_msg(e):
            for block in iter_content_blocks(e.get("message") or {}):
                if block.get("type") == "tool_use":
                    tool_uses.append({
                        "id": block.get("id"),
                        "name": block.get("name", "?"),
                        "input": block.get("input", {}),
                        "ordinal": len(tool_uses) + 1,
                        "event_index": i,
                    })
        elif is_user_msg(e):
            for block in iter_content_blocks(e.get("message") or {}):
                if block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        tool_results[tid] = block.get("content")
    return tool_uses, tool_results


def extract_attachments(events):
    attachments = []
    for i, e in enumerate(events):
        msg = e.get("message") or {}
        for block in iter_content_blocks(msg):
            btype = block.get("type", "")
            if btype in ("image", "document"):
                source = block.get("source") or {}
                data = source.get("data", "")
                mime = source.get("media_type", "")
                size = len(data) if isinstance(data, str) else 0
                digest = "unknown"
                if isinstance(data, str) and data:
                    digest = hashlib.sha256(data.encode()).hexdigest()[:16]
                attachments.append({
                    "ordinal": len(attachments) + 1,
                    "event_index": i,
                    "event_type": e.get("type"),
                    "kind": btype,
                    "mime_type": mime,
                    "size_bytes": size,
                    "digest_prefix": digest,
                })
    return attachments


def result_text(result):
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = []
        for b in result:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def input_strings(inp, out=None, depth=0):
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(inp, str):
        if inp.strip():
            out.append(inp)
    elif isinstance(inp, dict):
        for v in inp.values():
            input_strings(v, out, depth + 1)
    elif isinstance(inp, list):
        for v in inp:
            input_strings(v, out, depth + 1)
    return out


def tokenize_for_match(strings):
    """Break input strings into candidate match tokens."""
    tokens = set()
    for s in strings:
        for tok in TOKEN_SPLIT_RE.split(s):
            if len(tok) < MIN_TOKEN_LEN:
                continue
            if tok.lower() in STOPWORD_TOKENS:
                continue
            tokens.add(tok)
    return tokens


def infer_edges(tool_uses, tool_results):
    """
    Two evidence classes:
      strong: whole input string (>= MIN_MATCH_LEN) appears verbatim in prior result
      token:  a token (>= MIN_TOKEN_LEN) appears in prior result
    Emit at most one edge per (from, to) pair, preferring strong.
    """
    edges = []
    seen_pairs = set()
    result_by_ordinal = {}
    for u in tool_uses:
        result_by_ordinal[u["ordinal"]] = result_text(tool_results.get(u["id"]))

    for u in tool_uses:
        u_ord = u["ordinal"]
        u_strings = input_strings(u["input"])
        u_tokens = tokenize_for_match(u_strings)

        for prior in tool_uses:
            if prior["ordinal"] >= u_ord:
                break
            pair = (prior["ordinal"], u_ord)
            if pair in seen_pairs:
                continue
            prior_text = result_by_ordinal.get(prior["ordinal"], "")
            if not prior_text:
                continue

            # strong evidence
            matched_strong = None
            for s in u_strings:
                if len(s) >= MIN_MATCH_LEN and s in prior_text:
                    matched_strong = s
                    break
            if matched_strong:
                edges.append({
                    "from": prior["ordinal"], "to": u_ord,
                    "evidence": "strong",
                    "sample": matched_strong[:60],
                })
                seen_pairs.add(pair)
                continue

            # token evidence
            matched_tokens = [t for t in u_tokens if t in prior_text]
            if matched_tokens:
                edges.append({
                    "from": prior["ordinal"], "to": u_ord,
                    "evidence": "token",
                    "sample": ", ".join(sorted(matched_tokens)[:5]),
                })
                seen_pairs.add(pair)
    return edges


def find_dead_ends(tool_uses, edges):
    referenced = {e["from"] for e in edges}
    return [u["ordinal"] for u in tool_uses if u["ordinal"] not in referenced]


def find_parameterizable_literals(tool_uses):
    counts = Counter()
    for u in tool_uses:
        for s in input_strings(u["input"]):
            if 6 <= len(s) <= 100 and not s.startswith("/"):
                counts[s] += 1
    once = [s for s, n in counts.items() if n == 1]
    repeated = [(s, n) for s, n in counts.items() if n > 1]
    repeated.sort(key=lambda x: -x[1])
    return once[:20], repeated[:20]


def sanitize(name):
    return re.sub(r"[^a-zA-Z0-9-]", "-", name).lower()[:40]


def emit_naive_dag(label, objective, tool_uses, edges, attachments, conclusion):
    lines = [
        "apiVersion: mechanize.dev/v1alpha1",
        "kind: NaiveSentinel  # spike output — NOT a real Sentinel",
        "metadata:",
        f"  name: session-{label}",
        "  spikeGeneratedBy: dag-harvest v0",
        "spec:",
        "  purpose:",
        f"    summary: {json.dumps((objective or '')[:200].replace(chr(10), ' '))}",
        '    reusableQuestion: "UNKNOWN — heuristic cannot extract"',
        '    conclusionType: "UNKNOWN — heuristic cannot classify"',
        "  inputs:",
        "    # UNKNOWN — heuristic cannot distinguish inputs from constants",
        "    # candidates listed in spike-report.md",
        "  attachments:",
    ]
    if not attachments:
        lines.append("    []")
    else:
        for a in attachments:
            lines.append(f'    - id: att-{a["ordinal"]}')
            lines.append(f'      kind: {a["kind"]}')
            lines.append(f'      mimeType: {a.get("mime_type") or "unknown"}')
            lines.append(f'      sizeBytes: {a["size_bytes"]}')
            lines.append(f'      contentDigest: sha256:{a["digest_prefix"]}')
            lines.append(f'      # referenced by event index {a["event_index"]}')
    lines.append("  nodes:")
    deps_of = defaultdict(list)
    for e in edges:
        deps_of[e["to"]].append(e["from"])
    for u in tool_uses:
        node_id = f'{sanitize(u["name"])}-{u["ordinal"]}'
        lines.append(f"    {node_id}:")
        lines.append("      kind: tool  # heuristic")
        lines.append(f'      toolRef: {u["name"]}')
        deps = sorted(set(deps_of.get(u["ordinal"], [])))
        if deps:
            dep_ids = [
                f'{sanitize(next(x["name"] for x in tool_uses if x["ordinal"]==d))}-{d}'
                for d in deps
            ]
            lines.append(f'      dependsOn: [{", ".join(dep_ids)}]')
        else:
            lines.append("      dependsOn: []  # root or unclear predecessor")
        if isinstance(u["input"], dict) and u["input"]:
            k, v = next(iter(u["input"].items()))
            snippet = str(v)[:80].replace("\n", " ")
            lines.append(f'      # first arg: {k} = {json.dumps(snippet)}')
    lines.append("  outputs:")
    lines.append("    conclusion:")
    concl = (conclusion or "")[:300].replace("\n", " ")
    lines.append(f"      # {json.dumps(concl)}")
    lines.append('      value: "UNKNOWN — heuristic cannot map conclusion to node outputs"')
    return "\n".join(lines) + "\n"


def emit_report(label, path, events, objective, conclusion, tool_uses,
                tool_results, edges, attachments, dead_ends,
                once_literals, repeated_literals):
    tool_inv = Counter(u["name"] for u in tool_uses)
    n = len(tool_uses)
    n_with_result = sum(1 for u in tool_uses if u["id"] in tool_results)
    nodes_with_deps = len({e["to"] for e in edges})
    nodes_without_deps = n - nodes_with_deps

    L = []
    L.append(f"# Spike report — session {label}")
    L.append("")
    L.append(f"**Source file:** `{path.name}`")
    L.append(f"**Total JSONL lines:** {len(events)}")
    L.append(f"**Tool uses:** {n}")
    L.append(f"**Tool results linked:** {n_with_result} / {n}")
    strong = sum(1 for e in edges if e.get("evidence") == "strong")
    token = sum(1 for e in edges if e.get("evidence") == "token")
    L.append(f"**Edges inferred (total):** {len(edges)}  —  strong: {strong}, token: {token}")
    L.append(f"**Nodes with ≥1 inferred dep:** {nodes_with_deps}")
    L.append(f"**Nodes with NO inferred dep:** {nodes_without_deps}")
    L.append(f"**Attachments:** {len(attachments)}")
    L.append(f"**Dead-end nodes (result never referenced downstream):** {len(dead_ends)}")
    L.append("")
    L.append("## Objective (first user text)")
    L.append("")
    L.append("```")
    L.append((objective or "<none found>")[:500])
    L.append("```")
    L.append("")
    L.append("## Conclusion (last assistant text)")
    L.append("")
    L.append("```")
    L.append((conclusion or "<none found>")[:800])
    L.append("```")
    L.append("")
    L.append("## Tool inventory")
    L.append("")
    L.append("| Tool | Count |")
    L.append("|---|---|")
    for name, cnt in tool_inv.most_common():
        L.append(f"| `{name}` | {cnt} |")
    L.append("")
    L.append("## Attachments")
    L.append("")
    if not attachments:
        L.append("_None._")
    else:
        for a in attachments:
            L.append(
                f'- **att-{a["ordinal"]}** ({a["kind"]}, {a.get("mime_type") or "?"}, '
                f'{a["size_bytes"]}B, digest sha256:{a["digest_prefix"]}) '
                f'in event #{a["event_index"]} ({a["event_type"]})'
            )
    L.append("")
    L.append("## Dead-end nodes")
    L.append("")
    if not dead_ends:
        L.append("_None._")
    else:
        L.append("_(Tool call whose result is not substring-referenced by any later node — either exploratory dead end, or the heuristic missed the reference.)_")
        L.append("")
        for ord_ in dead_ends[:20]:
            u = next(x for x in tool_uses if x["ordinal"] == ord_)
            first = ""
            if isinstance(u["input"], dict) and u["input"]:
                first = str(next(iter(u["input"].values())))[:100].replace("\n", " ")
            L.append(f'- **{u["ordinal"]}. `{u["name"]}`** — first arg: `{first}`')
    L.append("")
    L.append("## Parameterization candidates")
    L.append("")
    L.append("### Literals appearing once (likely inputs)")
    L.append("")
    if not once_literals:
        L.append("_None._")
    else:
        for s in once_literals[:15]:
            L.append(f"- `{s[:80]}`")
    L.append("")
    L.append("### Literals appearing more than once (likely constants or shared refs)")
    L.append("")
    if not repeated_literals:
        L.append("_None._")
    else:
        for s, cnt in repeated_literals[:10]:
            L.append(f"- `{s[:80]}` × {cnt}")
    L.append("")
    L.append("## Sample edges")
    L.append("")
    if not edges:
        L.append("_None inferred._")
    else:
        L.append("| From | To | Evidence | Sample |")
        L.append("|---|---|---|---|")
        for e in edges[:20]:
            L.append(f"| {e['from']} | {e['to']} | {e.get('evidence','?')} | `{e.get('sample','')[:60]}` |")
    L.append("")
    L.append("## Ambiguity log — where the heuristic guessed")
    L.append("")
    ambiguities = []
    if not objective:
        ambiguities.append("No objective could be identified — first user text was empty or system-command-shaped.")
    if not conclusion:
        ambiguities.append("No conclusion could be identified — session may have ended mid-flow.")
    if n and nodes_without_deps > n * 0.5:
        ambiguities.append(
            f"{nodes_without_deps}/{n} nodes had no inferred dep. Either most calls are truly parallel/independent, "
            f"or the substring heuristic is missing structural references (IDs, cross-tool coordination)."
        )
    if n and len(dead_ends) > n * 0.4:
        ambiguities.append(
            f"{len(dead_ends)}/{n} nodes are dead ends. Suggests exploratory investigation with many rejected paths — "
            f"the compiler will need to classify these as EXPLORATORY vs REQUIRED."
        )
    if not attachments and objective and any(k in objective.lower() for k in ("image", "screenshot", "picture", "pdf")):
        ambiguities.append(
            "Objective text mentions an image/pdf but no attachment was extracted. Adapter may not be recognizing the attachment shape."
        )
    if not ambiguities:
        ambiguities.append("_None flagged automatically. Read the DAG and judge manually._")
    for a in ambiguities:
        L.append(f"- {a}")
    L.append("")
    L.append("## Recognizability check")
    L.append("")
    L.append("_To be filled in by the engineer who ran this session:_")
    L.append("")
    L.append("- [ ] Does the extracted DAG look like your investigation?")
    L.append("- [ ] Are the nodes in the right order?")
    L.append("- [ ] Did the heuristic classify the right things as inputs vs constants?")
    L.append("- [ ] Did any critical step get dropped?")
    L.append("- [ ] Would you trust this DAG to re-run without you?")
    L.append("")
    return "\n".join(L) + "\n"


def process(session_path, label, out_dir):
    events = load_jsonl(session_path)
    objective = extract_objective(events)
    conclusion = extract_conclusion(events)
    tool_uses, tool_results = extract_tool_events(events)
    attachments = extract_attachments(events)
    edges = infer_edges(tool_uses, tool_results)
    dead_ends = find_dead_ends(tool_uses, edges)
    once_lits, repeat_lits = find_parameterizable_literals(tool_uses)

    dag = emit_naive_dag(label, objective, tool_uses, edges, attachments, conclusion)
    rep = emit_report(label, session_path, events, objective, conclusion,
                      tool_uses, tool_results, edges, attachments,
                      dead_ends, once_lits, repeat_lits)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"session-{label}-naive-dag.yaml").write_text(dag)
    (out_dir / f"session-{label}-report.md").write_text(rep)

    print(
        f"session {label}: {len(tool_uses)} tools, {len(edges)} edges, "
        f"{len(attachments)} atts, {len(dead_ends)} dead ends → {out_dir}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", action="append", required=True,
                    help="label=path")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    out = Path(args.out)
    for spec in args.session:
        label, path = spec.split("=", 1)
        process(Path(path), label, out)


if __name__ == "__main__":
    main()
