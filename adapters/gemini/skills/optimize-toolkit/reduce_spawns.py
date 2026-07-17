#!/usr/bin/env python3
"""Mechanical reduction of spawn descriptions.

Input: raw cluster_spawns JSON (from stdin) — every spawn as its own "cluster"
       when called with min_jaccard=0.99 + min_cluster_size=1.
Output: reduced JSON with dedup + verb-bucket + tool-sig groupings, small
        enough to feed to an LLM for the semantic pass.

Stages:
  1. Flatten: unnest cluster.members into a flat spawn list.
  2. Normalize label: strip / lowercase for grouping keys; keep original for display.
  3. Dedupe by exact normalized label (label collapses N-of-a-kind to one row).
  4. Verb bucket: group by the first content word of the label ("Migrate", "Verify",
     "Trace", "Find", "Research", "Fix", "Build", "Plan", "Organize", "Review",
     "Code", "Silent", "Test", "Type", "Comment", "General", "Apply", "Explore",
     "Implement", "Extract", "Map", "Inventory", "Assess", "Reconcile", "Investigate",
     "Search:", "Validate", "Sonnet-5", "Independent", "Fresh-eyes", "MCP", "PLG.1",
     "W3.T3.2", "W4.T4.1"). No taxonomy — the verb IS the key.
  5. Tool-sig shape: within a verb bucket, sub-group by dominant-tool signature
     ("Bash+Read", "Read+Write", "Bash-only", "Empty", etc.).
  6. Session-burst collapse: consecutive spawns from the same session within 5min
     count as one "burst" (retain per-burst member count for display).
  7. Emit: per-verb-bucket row with {verb, count, unique_labels[:8], token_sum,
     sessions, dominant_tool_shape, sample_bursts}.
"""
import json
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime

def normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s.strip().lower())

def first_verb(label: str) -> str:
    """First content word, lowercased. Preserves prefixes like 'Search:' or 'W3.T3.2'."""
    words = label.strip().split()
    if not words:
        return "<empty>"
    w = words[0].lower().rstrip(',.:;')
    # Preserve compound/prefix identifiers verbatim
    if re.match(r'^(w\d|plg|mcp|search|sonnet-\d|fresh-eyes|per-adapter)', w):
        return w
    return w

def tool_shape(tool_signature: dict) -> str:
    if not tool_signature:
        return "<empty>"
    ranked = sorted(tool_signature.items(), key=lambda kv: -kv[1])
    if len(ranked) == 1:
        return f"{ranked[0][0]}-only"
    return "+".join(t for t, _ in ranked[:2])

def burst_key(session_id: str, at_iso: str, bucket_minutes: int = 5) -> str:
    t = datetime.fromisoformat(at_iso.replace('Z', '+00:00'))
    slot = t.replace(minute=(t.minute // bucket_minutes) * bucket_minutes,
                     second=0, microsecond=0)
    return f"{session_id}@{slot.isoformat()}"

def main():
    raw = json.load(sys.stdin)
    spawns = []
    for cluster in raw.get('clusters', []):
        sig = cluster.get('tool_signature') or {}
        for m in cluster.get('members', []):
            spawns.append({
                'label': m.get('subagent_description') or '',
                'session_id': m.get('session_id'),
                'at': m.get('at'),
                'model': m.get('subagent_model'),
                'tokens': m.get('tokens_total') or 0,
                'tool_signature': sig,
            })
    if not spawns:
        print(json.dumps({'error': 'no spawns with descriptions'}))
        return

    # Stage 4-5: verb bucket, sub-group by tool shape
    buckets = defaultdict(lambda: {
        'labels': Counter(),
        'tokens': 0,
        'sessions': set(),
        'tool_shapes': Counter(),
        'bursts': set(),
        'sample_examples': [],  # (label, tokens, model, shape)
    })
    for s in spawns:
        v = first_verb(s['label'])
        b = buckets[v]
        b['labels'][s['label']] += 1
        b['tokens'] += s['tokens']
        b['sessions'].add(s['session_id'])
        b['tool_shapes'][tool_shape(s['tool_signature'])] += 1
        b['bursts'].add(burst_key(s['session_id'], s['at']))
        # Keep top-token exemplars per bucket
        b['sample_examples'].append(
            (s['label'], s['tokens'], s['model'], tool_shape(s['tool_signature']))
        )

    # Emit
    reduced = []
    for verb, b in sorted(buckets.items(), key=lambda kv: -kv[1]['tokens']):
        exs = sorted(b['sample_examples'], key=lambda x: -x[1])[:5]
        reduced.append({
            'verb': verb,
            'spawn_count': sum(b['labels'].values()),
            'unique_labels': len(b['labels']),
            'top_labels': [l for l, _ in b['labels'].most_common(8)],
            'tokens_total': b['tokens'],
            'session_count': len(b['sessions']),
            'burst_count': len(b['bursts']),
            'dominant_tool_shapes': b['tool_shapes'].most_common(3),
            'sample_top_by_tokens': [
                {'label': l, 'tokens': t, 'model': m, 'shape': sh}
                for l, t, m, sh in exs
            ],
        })
    print(json.dumps({
        'input_spawns': len(spawns),
        'input_verb_buckets': len(buckets),
        'reduced_rows': reduced,
    }, indent=2, default=str))

if __name__ == '__main__':
    main()
