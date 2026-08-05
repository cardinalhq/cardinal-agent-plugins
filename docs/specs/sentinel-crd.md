# Sentinel CRD + minimal controller — implementation plan

**Status:** planning, not yet built.
**Scope tier:** Runtime Phase 3 level (b). Sentinel CRD + minimal kopf controller + published executor image + plugin `deploy-sentinel` skill. Explicitly *not* full Phase 3 (no admission webhook, no per-tenant RBAC, no SentinelRun history CRD, no SentinelTrigger CRD, no multi-cluster).
**Estimate:** ~1 working week end-to-end, single implementer.

## Goal

A team member on any repo that owns an **ownership boundary** — usually a service (lakerunner, chq-*), sometimes a platform team's cross-cutting concerns repo (e.g. `cardinal-sentinels` for SRE) — can:

1. `cd` into that repo.
2. Run Claude Code with the Cardinal plugin.
3. `/mechanize` → produces a Sentinel directory inside the repo.
4. `/cardinal:deploy-sentinel <dir>` → authors a Sentinel CR next to it, referencing the current repo + path.
5. `git commit && git push`.
6. Whatever deploys that repo already picks up the new CR and applies it. Findings start posting.

No cross-repo PR. No hand-authored Job manifest. No conversation with a platform engineer. No container image build per Sentinel. Sentinels live in and ship with whatever they're accountable for — the code they monitor (service-owned), or the cross-cutting question they answer (platform-owned).

## Non-goals for this cut

- **No admission webhook.** Structural validation happens in the controller after the CR lands. Bad Sentinels get a status condition, not a rejected apply.
- **No per-tenant RBAC beyond namespace boundaries.** k8s namespace isolation is the tenancy shape. A service's Sentinels run in the service's namespace.
- **No SentinelRun history CRD.** Each execution is a Job (or CronJob-owned Job). `kubectl get jobs -n <ns>` is the history surface.
- **No SentinelTrigger CRD.** Runs are one-shot or scheduled. Event-driven is later.
- **No image-baking of the Sentinel source.** Sentinel directory ships via git-clone at pod startup. The *executor* is a container image (see below); the *Sentinel directory* is git-cloned from the owning repo.
- **No auto-wiring of the owning repo's deploy glue.** Each repo needs a one-time addition to its existing deploy pipeline (Kustomization / Helm / ApplicationSet — depends on the repo) so Sentinel CRs get applied alongside the rest of what it ships. We document the pattern; we don't author the glue programmatically for every repo.
- **No web UI.** Status is `kubectl describe sentinel` and pod logs.

## Deploy topology

Three roles. Each owns one layer.

```
cardinal-agent-plugins                    <owning-repo>
├─ k8s/                                   ├─ (service code, if service-owned;
│  ├─ controller/    (kopf image)         │   otherwise just sentinels/)
│  ├─ chart/                              ├─ sentinels/
│  └─ crds/                               │  └─ <sentinel-name>/
├─ spike/executor/   (executor image)     │     ├─ sentinel.yaml
└─ adapters/claude/skills/                │     ├─ functions/
   └─ deploy-sentinel/                    │     ├─ deployment.yaml
                                          │     └─ sentinel-cr.yaml   ← authored
                                          │        by /cardinal:deploy-sentinel
                                          └─ deploy-glue/
                                             (adds sentinels/*/sentinel-cr.yaml
                                              to the repo's existing deploy path)
                                          
                                          kubernetes-clusters
                                          ├─ sentinel-controller/
                                          │  └─ application-set.yaml
                                          │     (installs the controller
                                          │      chart on the cluster —
                                          │      one-time platform action)
                                          └─ …everything else the team ships…
```

- **cardinal-agent-plugins** publishes three release artifacts:
  1. `ghcr.io/cardinalhq/sentinel-executor:vX.Y.Z` — a container image with the executor and its Python deps preinstalled. This is what every Sentinel Pod runs.
  2. `ghcr.io/cardinalhq/sentinel-controller:vX.Y.Z` — the kopf controller image.
  3. A Helm chart under `k8s/chart/` that installs the CRD + controller on any cluster.
  4. The `deploy-sentinel` skill in the Claude plugin.
- **Each owning repo** owns its own Sentinels under `sentinels/<name>/`, plus a small one-time deploy-glue addition so its existing deploy pipeline picks them up. The owning repo is usually a service repo (e.g. `lakerunner` owns Sentinels about lakerunner) and sometimes a cross-cutting platform repo (e.g. `cardinal-sentinels` for cross-service reconciliations owned by an SRE team). The controller doesn't care which — the deploy pattern is identical.
- **kubernetes-clusters** installs the sentinel-controller once. After that, it plays no role per-Sentinel — the owning repos are the source of truth.

## Data model — `Sentinel` CRD v1alpha1

Namespaced, group `sentinels.cardinalhq.io`, kind `Sentinel`.

```yaml
apiVersion: sentinels.cardinalhq.io/v1alpha1
kind: Sentinel
metadata:
  name: service-health-lakerunner
  namespace: lakerunner        # namespace = tenancy boundary; usually the service's own namespace
spec:
  # Where the Sentinel directory lives. Normally the same repo this CR lives in.
  source:
    git:
      url: https://github.com/cardinalhq/lakerunner
      ref: main                          # branch, tag, or commit SHA
      path: sentinels/service-health-lakerunner
      credentialsSecretRef: null         # optional deploy-key secret for private repos

  # Inputs — merged over the Sentinel directory's own inputs.json.
  inputs:
    instance: prod-us-east-2
    serviceQuery: lakerunner

  # Schedule. Omit for one-shot; set to a cron expression for recurring.
  schedule: "*/5 * * * *"

  # Runtime knobs.
  runtime:
    image: ghcr.io/cardinalhq/sentinel-executor:v0.1.0   # default; can be overridden
    resources:
      requests: { cpu: 100m, memory: 128Mi }
      limits:   { cpu: 500m, memory: 512Mi }
    timeoutSeconds: 600
    activeDeadlineSeconds: 900

  # Capability provider bindings. Each abstract capability id declared by the
  # Sentinel's `spec.capabilities.required[]` must resolve to a concrete provider
  # here (or the controller writes a Blocked status).
  capabilities:
    - id: observability.list-services
      provider: mcp
      endpointSecretRef: cardinal-mcp-endpoint
      tokenSecretRef: cardinal-mcp-token
    - id: observability.query-metrics
      provider: mcp
      endpointSecretRef: cardinal-mcp-endpoint
      tokenSecretRef: cardinal-mcp-token

  # Findings sinks. Same shape as the Sentinel dir's own deployment.yaml.
  sinks:
    - id: stdout
    - id: slack.channel
      channelSecretRef: sentinel-findings-slack

status:
  observedGeneration: 3
  phase: Pending | Reconciling | Running | Succeeded | Failed | Blocked
  conditions:
    - type: SourceResolved
      status: "True"
      lastTransitionTime: "..."
      reason: GitCloneOK
    - type: CapabilitiesBound
      status: "False"
      reason: MissingCapabilityProvider
      message: "observability.error-overview declared by sentinel but not bound in spec.capabilities"
  lastRunAt: "..."
  lastRunJobName: sentinel-service-health-lakerunner-abc123
  lastRunResult: Succeeded | Failed | Timeout
  findingsCount: 2
```

**Design notes:**
- `spec.source.git` is the only source variant for now. `spec.source.image` / `spec.source.configMap` are future fields — the union is deliberate.
- `spec.inputs` is a free-form object because the Sentinel schema for `spec.inputs` is itself free-form. Validation happens by the executor at runtime; the controller does not re-validate.
- `spec.capabilities[].id` MUST come from the abstract registry (`observability.*`, `code.*`). Same rule R2 enforces on the Sentinel itself.
- Every secret is a `<field>SecretRef: <name>`. No inline secrets. Secrets must exist in the same namespace as the Sentinel CR.

## Runtime shape — how a Sentinel becomes a Job

The controller reconciles a Sentinel CR into a `Job` (one-shot) or `CronJob` (scheduled). The Pod has two containers:

**initContainer:** `alpine/git:latest` — git-clones `spec.source.git.url` at `ref` into `/sentinel` (an `emptyDir` shared with the main container). If `credentialsSecretRef` is set, mounts the deploy key.

**main container:** `spec.runtime.image` (default `ghcr.io/cardinalhq/sentinel-executor`). The image ships the executor as an entrypoint. Working dir is `/sentinel/<spec.source.git.path>`. Runs:

```
sentinel-executor run . \
  --inputs /config/inputs.json \
  --deployment /config/deployment.yaml
```

- `/config/inputs.json` is a projected Secret built by the controller from `spec.inputs` merged over the Sentinel directory's own `inputs.json`.
- `/config/deployment.yaml` is projected similarly from `spec.sinks` + `spec.capabilities`.
- Capability endpoint/token env vars are projected from their `SecretRef`s into the pod env.
- `stdout` sink → pod logs. `slack.channel` sink → posts to the configured channel.

**Job naming:** `sentinel-<name>-<hash>` where the hash covers `spec.source.git.ref`, `spec.inputs`, `spec.capabilities`, `spec.sinks`, `spec.runtime`. Same spec → same name → controller no-ops.

**CronJob path:** identical `jobTemplate`, `schedule` from `spec.schedule`, `concurrencyPolicy: Forbid`, `successfulJobsHistoryLimit: 3`, `failedJobsHistoryLimit: 5`.

**Ownership:** every Job/CronJob has `metadata.ownerReferences` back to the Sentinel CR, so `kubectl delete sentinel` cascades.

## Controller logic

Python + [`kopf`](https://kopf.readthedocs.io/). Single-replica Deployment (leader election is Phase c).

Reconcile loop:

1. **Validate spec.** Basic YAML shape + required fields. `Blocked` phase + `SpecInvalid` condition on failure.
2. **Resolve capability bindings.** Fetch the Sentinel directory's `sentinel.yaml` (via a cached git-clone in the controller pod's own workspace) to read `spec.capabilities.required[]`. Compare against CR `spec.capabilities[]`. Missing bindings → `Blocked` phase + `CapabilitiesBound: False`.
3. **Build the projected config.** Merge CR inputs/sinks/capabilities over the Sentinel directory's `inputs.json` + `deployment.yaml`. Materialize as a namespace-local Secret, hash-suffixed, owner-referenced.
4. **Reconcile the Job or CronJob.** Compute spec-hash → name. If it exists at that content, no-op. Otherwise create with ownerRef.
5. **Watch owned resources.** On Job `Complete`/`Failed`, project into `Sentinel.status.lastRun*`. Parse pod logs for `FINDING` lines (stdout sink prefix) → `findingsCount`.
6. **On CR delete.** ownerRef cascade cleans up Jobs, CronJobs, projected Secrets. No finalizer for the minimal cut.

## The plugin's `deploy-sentinel` skill

Lives at `adapters/claude/skills/deploy-sentinel/SKILL.md` (and mirrors, once proven on Claude). Invocation: `/cardinal:deploy-sentinel <sentinel-dir>`.

The skill:

1. Reads `<sentinel-dir>/sentinel.yaml`, `deployment.yaml`, `inputs.json`.
2. Runs `git rev-parse --show-toplevel` and `git remote get-url origin` to discover the repo URL and derive the `path` relative to repo root.
3. Runs `git rev-parse --abbrev-ref HEAD` for the default `ref`.
4. Authors `<sentinel-dir>/sentinel-cr.yaml` with the fields prefilled. Prompts the user to fill in:
   - `metadata.namespace` (defaults to the sentinel's `metadata.name`-derived best guess if there's a matching k8s namespace, else asks)
   - `spec.inputs.*` (from the sentinel's `spec.inputs` schema — defaults where declared, prompts where required)
   - `spec.schedule` (asks: "one-shot or recurring?")
   - `spec.capabilities[].tokenSecretRef` / `endpointSecretRef` (asks the user which secret names already exist in the target namespace — provides examples for Cardinal MCP secrets)
   - `spec.sinks` (defaults to stdout; asks about Slack)
5. Prints:
   - The next step: `git add sentinels/<name>/sentinel-cr.yaml && git commit && git push`
   - A note about the one-time deploy-glue setup (if `<repo>/sentinels/` doesn't yet appear in the repo's deploy pipeline — see next section)
   - How to verify: `kubectl get sentinel <name> -n <ns>` and where findings will appear

Skill is thin — no code, just markdown instructions the model executes. The load-bearing state (URL, path, ref) comes from `git` commands, not from the skill guessing.

## Service repo deploy-glue — one-time per repo

Each owning repo needs a one-time addition so its existing deploy pipeline picks up Sentinel CRs alongside whatever else it ships. What "the pipeline" is varies by repo — Kustomization, Helm chart, ApplicationSet, ArgoCD Application:

- **Kustomization user:** add `sentinels/*/sentinel-cr.yaml` to `resources:` (glob if supported, else explicit paths).
- **Helm chart user:** add a `sentinels.yaml` template that includes each `sentinel-cr.yaml` as a raw manifest, or list them in `resources:` if the chart uses `kubernetes-clusters`-style resource lists.
- **ArgoCD ApplicationSet:** add a matrix generator whose second dimension globs `sentinels/*/`.
- **No pipeline (rare):** the service is deployed by hand or by a script. Add `kubectl apply -f sentinels/*/sentinel-cr.yaml` to that script.

The plan does NOT programmatically add this glue for every repo — the shapes vary too much. `deploy-sentinel` prints instructions the first time it detects `sentinels/` is absent from the repo's obvious deploy paths.

Reference example: `docs/specs/sentinel-crd-examples/` (added in M8) shows the addition for one owning repo in each supported shape.

## Repo layout

New under `cardinal-agent-plugins/`:

```
k8s/
├─ crds/
│  └─ sentinel.yaml              # CustomResourceDefinition manifest
├─ controller/
│  ├─ Dockerfile                 # builds sentinel-controller image
│  ├─ requirements.txt           # kopf, kubernetes, pyyaml, jsonschema
│  ├─ handlers.py                # kopf handlers
│  ├─ reconciler.py              # pure-python reconcile logic (testable)
│  ├─ projections.py             # inputs.json / deployment.yaml merging
│  └─ tests/
├─ chart/
│  ├─ Chart.yaml
│  ├─ values.yaml
│  ├─ templates/
│  │  ├─ crd.yaml
│  │  ├─ deployment.yaml
│  │  ├─ serviceaccount.yaml
│  │  └─ clusterrole.yaml
│  └─ README.md
└─ examples/
   └─ service-health-sample-cr.yaml   # sample Sentinel CR

spike/executor/
├─ Dockerfile                    # NEW — builds sentinel-executor image
└─ (existing files)

adapters/claude/skills/
└─ deploy-sentinel/
   └─ SKILL.md
```

Two images published on merges to main:
- `ghcr.io/cardinalhq/sentinel-controller:v<X.Y.Z>`
- `ghcr.io/cardinalhq/sentinel-executor:v<X.Y.Z>`

**kubernetes-clusters** gets one new top-level dir:

```
sentinel-controller/
├─ application-set.yaml
└─ aws-prod-us-east-2-global/
   └─ values.yaml
```

That's the *only* touch on kubernetes-clusters — the one-time controller install. Every Sentinel after that lives in an owning repo.

## Milestones — ordered slices, each independently reviewable

**M1 — CRD schema + validation (0.5 day).** Author `k8s/crds/sentinel.yaml`. Apply against a kind cluster, `kubectl explain sentinel` sanity check.

**M2 — Reconciler unit shell (1 day).** Pure-python reconciler: given a Sentinel dict, produce a Job manifest dict. No k8s client. Full unit test coverage on hash stability, projected config merging, capability-binding validation.

**M3 — Executor image (0.5 day).** Add `spike/executor/Dockerfile`. CI job builds + publishes `ghcr.io/cardinalhq/sentinel-executor:v<X.Y.Z>` on tag. Image entrypoint is `sentinel-executor` (a small console-script wrapper).

**M4 — kopf handlers + local run (1 day).** Wire the reconciler behind kopf `@kopf.on.create/update/delete`. Run against a local kind cluster; apply a Sentinel CR pointing at any public repo (e.g., cardinal-agent-plugins itself for the first bootstrap); verify the Job appears with correct spec.

**M5 — Real Sentinel end-to-end (1 day).** Pod actually clones the source repo and executes. Fixture: a Sentinel CR pointing at `cardinal-agent-plugins@main:mechanize-out/f89df52b-v2` (until a real owning repo has a Sentinel dir). Verify pod logs contain `FINDING` lines.

**M6 — Capability bindings + secrets + CronJob path (1 day).** Project CR `spec.capabilities` + `SecretRef`s into pod env. Add the `spec.schedule` branch → CronJob. Verify a scheduled Sentinel spawns Jobs on cadence.

**M7 — Helm chart + kind install (1 day).** Package the CRD + controller Deployment + RBAC. `helm install sentinel-controller ./k8s/chart` on kind, apply a Sentinel, verify end-to-end. Publish the controller image (`ghcr.io/cardinalhq/sentinel-controller:v<X.Y.Z>`).

**M8 — Deploy skill + first real service (1 day).** Author `adapters/claude/skills/deploy-sentinel/SKILL.md`. In `~/workspace/lakerunner`, run `/mechanize` on a sample session, then `/cardinal:deploy-sentinel` on the result. Follow whatever glue-add is needed for lakerunner's deploy pipeline. `git push`. Verify: ArgoCD (or equivalent) applies the CR, controller reconciles, findings appear.

**M9 — Install controller on prod EKS via ArgoCD (0.5 day).** Author `kubernetes-clusters/sentinel-controller/application-set.yaml`. ArgoCD sync. Move the M8 real-service Sentinel from kind to prod.

Total: ~7.5 working days, one implementer, mergeable in 9 PRs.

## What (b) explicitly defers to (c)

- **Admission webhook** wrapping `spike/executor/lint.py`. Bad Sentinels currently land and then get flagged in status.
- **SentinelRun CRD** for structured run history.
- **SentinelTrigger CRD** for event-driven runs.
- **Per-tenant RBAC beyond namespace.**
- **Multi-cluster.**
- **Image-baked Sentinels** (`spec.source.image`).
- **Leader election** for controller HA.
- **Finalizers** for pre-delete cleanup steps.
- **Controller-side outcomes wiring** (NEXT_SESSION.md backlog item 1, user shelved).
- **Auto-authoring the deploy-glue** per owning repo.

## Open questions to lock before starting M1

1. **kopf vs. Go controller-runtime?** Python matches the executor and is faster to iterate; the team's other controllers (maestro, lakerunner) are Go. My lean: kopf. Reconciler stays a pure function that ports easily if scale demands.
2. **Namespace-scoped CRD (assumed) or cluster-scoped?** k8s convention for tenancy is namespaced. Confirm.
3. **Should `spec.source.git.ref` require a commit SHA (not a branch) for determinism?** My lean: allow both, add a `sourceReconcilePolicy` field in Phase (c).
4. **Findings-count from pod logs is best-effort. OK for phase (b)?** Real accounting is Phase c.
5. **Controller namespace: dedicated `sentinel-system`, or ride in on `argocd` / existing platform namespace?** My lean: dedicated `sentinel-system`.
6. **How does lakerunner (or any first-mover service) actually deploy to prod today?** Need this to write the M8 glue-addition. If it's ArgoCD-Application-per-service pointing at a Helm chart in the repo, the glue is a template addition. If it's some other pattern, the glue is different. Answer determines the concrete step in M8; doesn't reshape the plan.

## Success criterion

A team member on any owning repo (service or cross-cutting) can:

1. `cd ~/workspace/<owning-repo>`
2. `claude` (Cardinal plugin installed)
3. `/mechanize` on a completed investigation session
4. `/cardinal:deploy-sentinel sentinels/<id>/`
5. `git add sentinels/<id>/ && git commit && git push`
6. (First time only) add the deploy-glue to the owning repo per printed instructions
7. See findings appearing in Slack or `kubectl logs -l sentinels.cardinalhq.io/sentinel=<name> -n <ns>` within one cron cycle

No cross-repo PR. No hand-authored Job. No container-image build per Sentinel. No conversation with a platform engineer.

If that works for one Sentinel in one real owning repo on prod EKS, (b) is done and (c) becomes incremental additions to a working controller.
