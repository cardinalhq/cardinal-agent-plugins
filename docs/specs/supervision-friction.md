# Supervision ergonomics proposal

Status: implemented and validated; independent final review reports NO BLOCKERS.
Base: c4c89fc.

The independent review found two blockers: a renewal/commit race and hidden rows
on self-only pages. The design below incorporates continuous budget→cursor locks
and complete sequence coverage. The reviewer rechecked it and reported NO BLOCKERS
before implementation began. Implementation review then reproduced an existing
filtered-auto-review bug: a finding-only display committed a hidden challenge.
Cursor-backed auto-review now rejects class/type/max filters before reading or
mutating state. The reviewer confirmed NO BLOCKERS and reran 129 focused tests,
all passing. The final author-run focused suite passes 217 tests.

## Problem and scope

A Claude supervisor reported truncated Monitor notifications, grant renewal by
editing cursor JSON, redundant commits for its own posts, manual outbox numbering,
late discovery that owner relay was disabled, and confusing milestone attribution.
Local inspection confirms those paths. Monitor truncation is outside the plugin;
changing a CLI text cap cannot repair it. The plugin can make notifications mere
wake signals and make the subsequent review complete and easy to commit.

Implement this in shared core and the four shipped supervision skills. No server
schema, deployment, owner-setting changes, or new external-system polling service.
Independent GitHub/k8s checks remain required; document how to cite their captured
evidence and existing artifact hashes, rather than claim an unimplemented live link.

## Proposed changes

1. Add `events --signal-only`, valid only for a cursor-backed follow watch without
   auto-review, JSON, or display filters. Emit a short sequence-range wake signal,
   never event bodies. The existing follow watch continues to read without moving
   the review cursor. Claude Monitor uses this flag. The next ordinary cursor read
   returns complete text, evidence and refs. Compact JSON retains secondary
   semantic text fields, tests and based_on; compact line output includes them.
   No claim that the Monitor notification itself contains full event text.

2. Add `events --save-page <directory>` for a non-follow, unfiltered cursor read.
   Persist the exact full pending page as private `<batch>.json`, using exclusive
   creation and rejecting symlinks/content mismatch; output its absolute path.
   The agent reads that immutable snapshot, then uses
   `reviewed --page-file <path>` instead of copying a hash. Retain `--batch` for
   compatibility; require exactly one. Verify snapshot identity and hash against
   the current pending page inside a cursor lock held continuously through
   budget/tag application and cursor advancement. Lock order is budget then
   cursor; do not release the cursor between validation and commit, including
   across remote budget writes. Renewal takes the cursor lock and cannot
   replace the pending page during review. Never
   interpret saving or reading a file as a completed review.

3. Cursor reads exclude own posts by default (include-self overrides). Automatically
   advance only a nonempty page consisting entirely of known self events, using
   the server viewer principal and existing self predicate, never owner decisions.
   Require complete contiguous sequence coverage from after+1 to next_after:
   a nonempty page with hidden sequence gaps is not safe to skip either.
   Unknown metadata, mixed pages, pages with hidden rows, and other people's
   events always require review. Bound skipping to 10 pages per invocation. Retain
   raw mixed pending pages and their hashes for budget attribution and crash replay.
   Watch remains read-only; no default auto-review. Explicit filtered reads must
   never gain the ability to commit hidden non-self events automatically.

4. Add `renew-cursor --previous-credential-file <old> --credential-file <new>
   --cursor-file <cursor>`. Require private old/new credentials, exact old cursor
   identity, same origin/org/investigation and equal token scope sets. Validate
   the new token with a server read before changing anything. Preserve committed
   after; discard any unreviewed cached page and refetch it with the new grant.
   Thus pending content and producer identity never leak across grant changes.
   Failure leaves the cursor unchanged. No automatic renewal or broader scopes.
   Old challenges remain owned by the old grant; renewal does not transfer ruling
   authority, and the command/help must say so. The old grant may be expired.

5. Scoped posts with an explicit credential file may omit `--outbox-file`: use a
   private `outbox/` beside that credential and the existing content-addressed
   idempotency key as filename. Keep explicit outbox behavior and legacy token
   compatibility. No token on stdout. A timeout retry with the same content and
   grant resolves to the same file/key. Renewing a grant remains a new principal;
   do not silently retry an old uncertain post with a new grant.

6. Registration and draft-decision output explicitly distinguish relay ready,
   disabled, and unknown. When disabled, show the existing web settings link and
   explain that `decide:`/`approve <id>` cannot relay until opt-in. Shared skill
   setup branches on that status before promising delivery; direct owner prompts
   in the worker chat remain authoritative without a prefix. A signed-in web
   decision is the fallback, using existing UI; do not pretend to add a new
   one-click web approval feature or enable the account setting through the key.

7. Explain budget attribution at the review point and in CLI help: an event's tag
   charges the preceding active interval, not a soak/wall-clock timer or an implied
   milestone start. Use a preparation/reporting milestone, or explicit `none`,
   before a soak starts. Display valid verdict enum values and current-page tag
   requirements. Do not silently change already-recorded budget accounting. A
   first-class milestone lifecycle and retagging historical records need a separate
   server-compatible design, not a local-only rewrite of receipts.

## Invariants and validation

- Delivered, saved and reviewed remain separate; a crash replays the outstanding
  page. Hash mismatch, stale snapshot, wrong identity and invalid watermark fail
  without advancing the cursor or writing budget events.
- No automatic commit of another principal's event, owner decision, unknown event,
  or an event hidden only by class/type/maximum display filters.
- Renewal authenticates the new grant remotely, does not replay old pending
  payloads, and never transfers challenge/ruling authority or uncertain outboxes.
- Private files reject symlinks, have 0600 files/0700 generated directories, and
  do not print credentials. Concurrent writes either lock or fail safely.
- Tests cover long multiline resolution explanations, watch non-mutation, snapshot
  replay/staleness/tampering, self-only versus mixed/unknown pages, scope and origin
  mismatches, failed renewal, timeout/outbox deduplication, relay-off wording, and
  budget attribution/help. Run core, adapter and relevant packaging contract suites.

## Review gate

An independent agent reviews this proposal and relevant code, reports blockers
with concrete failure cases, and cannot edit implementation. Revise the proposal
until it has no blockers before implementation. Obtain an independent review of
the final diff and resolve findings before delivery.

## Validation record

- Final shared-core suite: 1,058 tests passed.
- Final author-run focused supervision suites: 217 tests passed.
- Independent review rerun: 129 focused tests passed, NO BLOCKERS.
- Codex adapter: 226 tests passed; Cursor: 186; Gemini: 192.
- Repository contracts: 35 tests passed; shared-skill sync and diff checks passed.
- Claude adapter full suite: 713 tests, OK (3 skipped).

These are local checks. No plugin release, server deployment, account setting
change, or live external-state integration was performed.
