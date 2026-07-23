# Invariant advisory git pre-commit hook

## What

An advisory git `pre-commit` hook that runs [Invariant](https://github.com/cardinalhq/conductor/tree/main/packages/invariant)'s
`check-pr` against your staged diff and prints any guarantee / bug-pattern
fires it finds. **Advisory only — it never blocks a commit.** No matter what
`check-pr.ts` reports, or whether anything in the hook fails, the hook always
exits `0`. This is a deliberate contract: aggressive availability, not forced
consumption.

## Prereq

You need [`cardinal-hq/conductor`](https://github.com/cardinalhq/conductor)
checked out locally, with the Invariant CLI (`packages/invariant/scripts/check-pr.ts`)
invocable via:

```bash
pnpm --filter @cardinalhq/invariant exec tsx scripts/check-pr.ts
```

The hook looks for conductor at `$HOME/workspace/conductor/packages/invariant`
by default. If yours lives somewhere else, point at it with:

```bash
export INVARIANT_PKG_DIR=/path/to/conductor/packages/invariant
```

If conductor isn't checked out, or `INVARIANT_PKG_DIR` doesn't resolve to a
real directory, the hook silently exits `0` — no warnings, no broken
commits. Missing tooling is never an error here.

## Install

From inside the git repo you want the hook active in:

```bash
bash core/git-hooks/pre-commit-install.sh
```

(Point the path at wherever you have `cardinal-agent-plugins` checked out.)
The installer resolves its own location, so it works when invoked from any
directory. It symlinks `.git/hooks/pre-commit` to `pre-commit-hook.sh`. If a
non-symlink hook is already present, it's backed up first (never clobbered)
to `.git/hooks/pre-commit.pre-invariant.bak`.

## Uninstall

```bash
rm .git/hooks/pre-commit
```

If the installer made a backup of a pre-existing hook, restore it instead:

```bash
mv .git/hooks/pre-commit.pre-invariant.bak .git/hooks/pre-commit
```

## Log

Every commit that trips the hook while conductor tooling is available may
append a record to `~/.invariant/dogfood-log.jsonl`:

```
{ ts, repo, branch, staged_files_count, self_referential, fires: [{guarantee_id, kind}], outcome: null }
```

Logging policy: a record is written when the diff is self-referential
(invariant changing its own guarded code — useful for calibrating the
self-referential rate) or when `fires` is non-empty. A clean run with no
fires and no self-reference is not logged, to keep the log scannable.

## Outcome categories

The trial this hook feeds measures what happens after a fire is shown to an
engineer, via post-hoc annotation of `outcome`:

- `ignored` — the fire was seen but nothing about the commit changed.
- `reviewed-no-change` — the engineer looked at it, decided it was a
  non-issue, and proceeded as-is.
- `changed` — the fire caused the engineer to change the commit before
  landing it.

## Post-hoc annotation

`outcome` starts `null` at log time (the hook can't know what happens after
the commit). Annotate entries later from conductor:

```bash
pnpm --filter @cardinalhq/invariant exec tsx scripts/annotate-log.ts
```

## Why this lives in agent-plugins

The hook itself is agent-agnostic: engineers commit through `git` regardless
of which Cardinal agent adapter (Claude, Gemini, Cursor, Codex) they're
using day to day, so the hook doesn't belong under any one adapter's
`hooks/`. It lives here, under `core/`, as the shared distribution point for
all adapters. The guarded-code specifics — what `check-pr.ts` actually
checks, the `.invariants.yaml` format, the guarantee/bug-pattern catalog —
stay in conductor, where Invariant itself lives.
