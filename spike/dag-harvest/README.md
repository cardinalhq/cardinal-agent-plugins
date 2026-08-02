# spike/dag-harvest

Throwaway heuristic-only harness. Cracks open a Claude Code session JSONL and produces:

- `session-<label>-naive-dag.yaml` — scrappy §8-shaped Sentinel candidate
- `session-<label>-report.md` — what the heuristic did, guessed, or dropped

**Read `FINDINGS.md`** for the synthesis. That's the deliverable. The code and per-session reports are exhibits.

## Run

```
python3 harvest.py \
  --session A=/path/to/session1.jsonl \
  --session B=/path/to/session2.jsonl \
  --out out
```

## Do not

- Merge this to `main` — it's a spike, not phase 1 code.
- Reuse the code in `mechanize/` — the whole point is to learn what the real thing needs *before* committing to a shape.
- Trust the emitted DAGs. They are exhibits of what a naive heuristic produces.

## Cleanup

Delete `spike/` once the phase 1 plan is revised per `FINDINGS.md` and the branch is deleted.
