---
name: deploy-sentinel
description: "Author a Sentinel Custom Resource next to a compiled Sentinel directory and guide the user through the git-push → ArgoCD → controller reconciliation flow."
---

# deploy-sentinel (Claude Code) — author a Sentinel CR next to a compiled Sentinel directory

**What this skill does.** Given a Sentinel directory (produced by `/mechanize`), author a `sentinel-cr.yaml` next to it that references the current git repo + path, prompts the user for the deploy-time bindings (namespace, inputs, schedule, capability providers, sinks), and prints the git-push + verify steps.

**What this skill does NOT do.** It never `git push`es. It never `git commit`s. It never creates k8s namespaces, secrets, or the controller. Those are one-time platform/human actions.

The load-bearing facts (repo URL, branch, path-in-repo) come from `git` commands, not from guesses. The user is prompted for everything else.

## Stage 1 — Argument parsing

The user typed `/cardinal:deploy-sentinel <sentinel-dir>`.

- If the argument is **absent**: ask the user for the path. Do NOT guess (there may be many Sentinel dirs under `mechanize-out/` or `sentinels/`).
- If the argument **exists but does not contain `sentinel.yaml`**: refuse. Print exactly why (e.g. "no `sentinel.yaml` at `<dir>` — did you mean the parent?"). Do not proceed.
- If the argument is a **relative path**: resolve it to an absolute path against the current working directory before continuing.

Call the resolved absolute path `SENTINEL_DIR` for the rest of this skill.

## Stage 2 — Discover repo context

Run these Bash commands from inside `SENTINEL_DIR`. Fail loudly (stop, print the error, do not write a CR) if any of them fails.

```
cd "<SENTINEL_DIR>"
REPO_ROOT=$(git rev-parse --show-toplevel)
REPO_URL_RAW=$(git remote get-url origin)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
```

Normalize `REPO_URL_RAW`:

- `git@github.com:ORG/REPO.git` → `https://github.com/ORG/REPO`
- `https://github.com/ORG/REPO.git` → `https://github.com/ORG/REPO`
- Leave any other shape unchanged (but tell the user, so they can hand-edit).

Compute `PATH_IN_REPO` as `SENTINEL_DIR` relative to `REPO_ROOT`. If `SENTINEL_DIR` is not inside `REPO_ROOT` (e.g. absolute path outside the tree), refuse.

Print a one-line summary before continuing so the user can interrupt if any of this is wrong:

```
Repo:   <REPO_URL> @ <BRANCH>
Path:   <PATH_IN_REPO>
Root:   <REPO_ROOT>
```

## Stage 3 — Read the Sentinel dir

Read three files from `SENTINEL_DIR`:

1. **`sentinel.yaml`** (required). Capture:
   - `metadata.name` → will become the CR's default `metadata.name`.
   - `spec.inputs` → the free-form input schema. Note which keys have declared defaults and which are required.
   - `spec.capabilities.required[]` → the list of abstract capability ids the executor will need bound.

   If `sentinel.yaml` is missing or unparseable, stop and print why.

2. **`inputs.json`** (optional but common). Capture the current key/value defaults. These become the suggested defaults in Stage 4 for anything the user is prompted to fill in.

3. **`deployment.yaml`** (optional). If present, capture the sinks and capability-binding hints already recorded. Use them as suggested defaults in Stage 4.

## Stage 4 — Author the CR interactively

Prompt the user for each field below **in order**. For each prompt, offer a concrete default the user can accept with a single word. Do not batch the prompts — one question at a time so the user can course-correct.

### 4.1 `metadata.namespace`

Ask: "Which k8s namespace on the target cluster should this Sentinel run in?"

Suggest the most likely value in this order:
- If the Sentinel's `metadata.name` looks like `<something>-<service>`, suggest `<service>`.
- Else suggest the repo name (last segment of `REPO_URL`).
- Else suggest `default`.

Tell the user: "This namespace must already exist on the target cluster. This skill does not create it."

### 4.2 `metadata.name`

Ask: "CR name?" Default: the Sentinel's `metadata.name`. Accept anything DNS-1123 compliant.

### 4.3 `spec.inputs`

For every key in the Sentinel's `spec.inputs` schema:

- If the key has a default in the schema OR a value in `inputs.json`, present that as the suggested default.
- Otherwise, prompt with no default and require an answer.

Only include keys the Sentinel actually declares. Do not invent inputs.

### 4.4 `spec.schedule`

Ask: "One-shot or recurring?"

- One-shot → omit `spec.schedule` in the output.
- Recurring → ask for a cron expression. Default: `*/5 * * * *` (every 5 minutes, the standard polling cadence). Validate it looks like a 5-field cron string; if not, re-ask.

### 4.5 `spec.capabilities[]`

For each `id` in the Sentinel's `spec.capabilities.required[]`:

- Ask: "Which provider satisfies `<id>`?" Default: `mcp` (this is the only concrete provider today).
- Ask: "Which Secret holds the endpoint URL?" Default: `cardinal-mcp-endpoint`.
- Ask: "Which Secret holds the auth token?" Default: `cardinal-mcp-token`.

Tell the user: "These Secrets must already exist in the target namespace. This skill does not create them."

Emit one entry in `spec.capabilities[]` per required id, with the three collected fields.

### 4.6 `spec.sinks`

Start with a default sink of `{ id: stdout }` (findings go to pod logs — always safe, always present).

Ask: "Add a Slack sink?"

- If yes: ask "Which Secret holds the Slack channel id / webhook?" Default: `sentinel-findings-slack`. Append `{ id: slack.channel, channelSecretRef: <name> }`.
- Repeat until the user says no.

If `deployment.yaml` already listed sinks, offer those as the initial suggestion instead of just `stdout`.

### 4.7 `spec.runtime.image`

Ask: "Executor image?" Default: `ghcr.io/cardinalhq/sentinel-executor:v0.1.0`. Almost always accepted as-is.

Do NOT prompt for `spec.runtime.resources`, `timeoutSeconds`, `activeDeadlineSeconds`. The controller applies sane defaults; the CR omits them unless the user specifically asks to override.

### 4.8 Private-repo handling

If `REPO_URL` is an SSH URL or the user tells you the repo is private, ask: "Which Secret holds the deploy key for this repo?" and emit `spec.source.git.credentialsSecretRef: <name>`.

If the repo is public (the common case for Cardinal-owned repos), **explicitly emit `credentialsSecretRef: null`** in the output so it is obvious the omission was intentional.

## Stage 5 — Write `sentinel-cr.yaml`

Write the collected fields to `<SENTINEL_DIR>/sentinel-cr.yaml`. Structure exactly as follows; **omit any field the user did not set** (except the intentional `credentialsSecretRef: null` from 4.8):

```yaml
apiVersion: sentinels.cardinalhq.io/v1alpha1
kind: Sentinel
metadata:
  name: <4.2>
  namespace: <4.1>
spec:
  source:
    git:
      url: <REPO_URL>
      ref: <BRANCH>
      path: <PATH_IN_REPO>
      credentialsSecretRef: <4.8 or null>
  inputs:
    <4.3 collected key/value pairs>
  schedule: <4.4 if recurring>
  runtime:
    image: <4.7>
  capabilities:
    - id: <id>
      provider: <provider>
      endpointSecretRef: <name>
      tokenSecretRef: <name>
    # ...one entry per 4.5 collection
  sinks:
    - id: stdout
    # ...plus 4.6 additions
```

Do NOT write null or empty maps/arrays for sections the user did not populate (e.g. drop `schedule:` entirely for one-shot; drop `inputs:` entirely if the Sentinel declares none).

After writing, print the full CR back to the user for a final sanity check and ask "Write this to `<SENTINEL_DIR>/sentinel-cr.yaml`? (y/n)". Only write on `y`.

## Stage 6 — Print next steps

Once the file is written, print three blocks.

### 6.1 Git commands

Print the exact commands, ready to copy-paste. Do NOT run them.

```
cd <REPO_ROOT>
git add <PATH_IN_REPO>/sentinel-cr.yaml
git commit -m "Add Sentinel CR for <metadata.name>"
git push
```

### 6.2 Deploy-glue check

Detect whether the service repo's deploy pipeline already picks up `sentinels/*/sentinel-cr.yaml`. Use Bash (from `REPO_ROOT`) to check for these markers, in order:

- **Kustomize:** `find . -maxdepth 4 -name kustomization.yaml -not -path '*/node_modules/*'`, then grep the matches for the string `sentinels/` or `sentinel-cr.yaml`.
- **Helm:** `find charts -maxdepth 3 -name 'Chart.yaml' 2>/dev/null` and, if any chart exists, grep its `templates/` for `sentinel-cr`.
- **ArgoCD ApplicationSet:** `grep -r 'kind: ApplicationSet' . --include='*.yaml' -l 2>/dev/null`, then grep those files for `sentinels/`.
- **Nothing found:** the repo has no deploy glue for Sentinel CRs yet.

Report which shape you found (or "none"). Then print the matching one-time addition:

- **Kustomize repo, glue missing:** "Add `sentinels/*/sentinel-cr.yaml` to `resources:` in the top-level `kustomization.yaml` (use a glob if the tool supports it, else list the paths explicitly)."
- **Helm chart repo, glue missing:** "Add a `templates/sentinels.yaml` that includes each `sentinels/*/sentinel-cr.yaml` as a raw manifest (via `{{ .Files.Get }}` + `range`)."
- **ApplicationSet repo, glue missing:** "Add a matrix generator whose second dimension globs `sentinels/*/`, or add a per-Sentinel Application resource."
- **No pipeline detected at all:** "This repo appears not to have an obvious deploy pipeline. If deploys happen by hand or by a script, add `kubectl apply -f sentinels/*/sentinel-cr.yaml` to that path. Talk to whoever owns this service's deploy."

If deploy glue **was** found matching this Sentinel, skip the one-time-addition text and just say: "Glue detected in `<file>` — the next `git push` should pick this up automatically."

### 6.3 Verify

Print exactly:

```
kubectl get sentinel <metadata.name> -n <metadata.namespace>
kubectl describe sentinel <metadata.name> -n <metadata.namespace>
kubectl logs -l 'sentinels.cardinalhq.io/sentinel=<metadata.name>' -n <metadata.namespace> --tail=200
```

Tell the user: "Give ArgoCD (or your deploy pipeline) a minute to sync, then the controller a few seconds to reconcile. `status.phase` will move Pending → Reconciling → Running. Findings appear in pod logs (stdout sink) and, if configured, the Slack channel."

## What NOT to do

- **Do NOT `git push`.** Only print the command.
- **Do NOT `git commit`.** Only print the command.
- **Do NOT create the k8s namespace.** Only tell the user it must exist.
- **Do NOT create the Secrets** referenced in `spec.capabilities[]` or `spec.sinks[]`. Only tell the user they must exist.
- **Do NOT install the sentinel-controller.** That is a one-time platform action, not a per-Sentinel step.
- **Do NOT add the deploy-glue programmatically.** Only print the matching instructions from Stage 6.2 and let the user decide. Deploy pipelines vary too much repo-to-repo.
- **Do NOT invent inputs, capabilities, or sinks the Sentinel didn't declare.** The CR is a binding layer, not a redesign.

## Success criterion

A `sentinel-cr.yaml` exists next to the Sentinel directory, references the correct repo URL / ref / path (verified via `git`), has every required capability bound to a Secret pair, and lists at least the `stdout` sink. The user has the exact three commands they need to run to ship it (`add`, `commit`, `push`), and — if the repo needs deploy-glue — the exact addition they need to make once, matched to the deploy shape their repo actually uses.
