# PR #187 review

Reviewed hosted compilation/acceptance, receipt persistence, full-suite replay,
offline promotion/qualification, freeze/lifecycle boundaries and packaged imports.

Blocking findings fixed before merge:

1. An ordinary test request or receipt-validation failure could reuse a previous
   passing observation, and a failed request for a new case was not recorded in
   the regression suite. Requests now reserve cases and invalidate old results
   before contacting the backend. Completion tokens reject superseded responses.
   The failure was reproduced by a regression test before the fix.
2. The production-method migration guard hashed Python AST dumps, which differ
   between Python releases. The GitHub Python 3.12 check failed despite identical
   methods. It now hashes exact method source segments from the recorded original
   plugin commit, preserving the guard without Python-version dependence.
3. Concurrent authoring operations could overwrite acceptance metadata or share
   a temporary receipt filename. Program updates are now serialized with scope
   admission, completion re-reads current metadata, and atomic writes use unique
   private temporary files. A synchronized eight-writer test verifies complete
   private receipts without shared temporary-file collisions.

Local validation: 87 behavior tests passed, including the three actual retained
SDK failures over all 69 development traces with zero backend model calls; the
built-artifact release test also passed. Source/semantic instruction guards pass.
No new qualification experiment, SDK/JEV modification or production deployment.

Remaining architectural limits are documented in the PR: hosted development
acceptance is distinct from independent offline qualification; direct deployed
API clients do not acquire the plugin's local regression-history gate. These
are explicit consolidation boundaries, not new correctness claims.
