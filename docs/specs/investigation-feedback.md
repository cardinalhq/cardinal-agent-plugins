# Investigation feedback repair

This change addresses lost supervisor posts, unreadable event previews, prose-only lane
status and remote evidence that reviewers cannot inspect. The companion Conductor change
validates structured checkpoint fields and presents current status.

## Plan and implementation

1. File/stdin input: `post`, `rule`, `question` and `propose-decision` accept `--text-file`
   or `--stdin`; `ack` accepts `--note-file` or `--stdin`. Input is UTF-8, verbatim, bounded
   by the existing field limits. Inline forms remain compatible.
2. Guarded reviews: declare outboxes with `review-plan --cursor-file C --batch B
   --require-post P` before invoking the shell. Post using `--cursor-file C --batch B
   --outbox-file P`. The exact pending page cannot commit until every declared outbox has
   a confirmed server event for that investigation and batch. Lost responses retry with
   the same idempotency key. An unrelated old successful outbox cannot satisfy the guard.
   `reviewed --require-post P` can additionally declare a required post, but declaration
   before posting is essential to survive a command that never starts. Pages with no
   required posts retain the existing explicit review workflow. This is durable intent,
   not an OS isolation boundary against someone who can rewrite private state files.
3. Notifications: put a bounded, word-preserving digest first and include the full event's
   timeline anchor. Full event envelopes retain their authority and text escaping.
4. Structured status: result checkpoints accept optional `lanes` and `run` when the server
   advertises `checkpoint_details`. Unsupported servers fail before sending; these fields
   are never silently stripped. The UI projects the latest current claim revisions,
   removes retractions and derives counts. Old prose is not heuristically converted.
5. Remote evidence: `bundle ID --investigation INV --out DIR [--root ROOT]` exports the
   selected semantic claim revisions, cited receipts and, with an explicit root, the
   recorded artifact files after checking their hashes. Destination must be new; output
   is private and bounded to 64 MiB. `verify --bundle DIR` works offline, detects missing
   or changed files and explicitly distinguishes integrity from independent verification.

## Checkpoint example

```json
[{"type":"experiment.completed","id":"exp_benchmark","outcome":"Consumer exceeded the memory cap",
  "lanes":[{"lane":"consumer","h":"64","state":"failed","detail":"Exceeded 2 GiB"}],
  "run":{"host":"aws-worker","source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "image_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
  "artifacts":[{"path":"reports/rss.log","sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","role":"report"}],
  "evidence":["rcpt_dddddddddddddddddddddddd"]}]
```

Lane states are pending, running, passed, failed, blocked or skipped. H is an optional
string so a domain-specific parameter retains its exact label. Evidence remains on the
checkpoint and passes the existing receipt-access validation. Run fields are bounded,
participant-reported context: host, full source_commit, image_digest, command, started_at
and finished_at. A digest does not prove which source ran or whether a measurement is valid.

## Validation and rollout

Regression tests cover shell-sensitive text, missing/failed posts, idempotent retries,
wrong-batch reuse, schema bounds, receipt/artifact transfer, tampering and path traversal.
Existing budget attribution and all four plugin adapters retain their compatibility gates.
Merge/deploy Conductor first to enable `checkpoint_details`, then release the adapters.
Safe posting and bundle export use existing routes and work independently of that capability.
Production rollout and independent verification of real AWS runs are separate from these
local implementation tests. No telemetry uploads or file transfers are newly automatic.
