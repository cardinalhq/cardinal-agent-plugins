# Sentinel CRD — build status

Companion to [`sentinel-crd.md`](sentinel-crd.md). What the workflow completed
vs. what is deferred, milestone by milestone.

## Milestone status

| Milestone | Status | Notes |
|---|---|---|
| **M1 — CRD schema + validation** | Complete | `k8s/crds/sentinel.yaml` shipped; mirrored into the chart at `k8s/chart/templates/crd.yaml` with `helm.sh/resource-policy: keep`. |
| **M2 — Reconciler unit shell** | Complete | Pure-Python: `k8s/controller/reconciler.py`, `projections.py`, `capabilities.py`. Full unit coverage under `k8s/controller/tests/` — hash stability, inputs/deployment merging, capability-binding validation. No k8s client dependency in the reconcile logic. |
| **M3 — Executor image** | Complete | `spike/executor/Dockerfile` + `spike/executor/VERSION` (currently `0.1.0`). CI at `.github/workflows/executor-image.yml` builds + publishes `ghcr.io/cardinalhq/sentinel-executor:v<X.Y.Z>` on tag pushes. |
| **M4 — kopf handlers** | Complete | `k8s/controller/handlers.py` wires `@kopf.on.create/update/delete` to the M2 reconciler, projects the config Secret, creates the owned Job/CronJob, watches for completion, parses `FINDING` lines out of pod logs into `status.findingsCount`. |
| **M5 — Real Sentinel end-to-end** | **Runbook-only** | Needs a real cluster; CI cannot execute it. See [`k8s/docs/M5-validation-runbook.md`](../../k8s/docs/M5-validation-runbook.md) — kind cluster, load both images, `helm install`, apply the fixture Sentinel CR pointing at `cardinal-agent-plugins@main:mechanize-out/f89df52b-v2`, verify Job runs and `FINDING` lines appear. |
| **M6 — Capability bindings + secrets + CronJob path** | Complete | Absorbed into M2 + M4: capability-binding validation lives in `capabilities.py` (with tests in `test_capabilities.py`); `SecretRef` env projection and the `spec.schedule` → CronJob branch live in `handlers.py` + `reconciler.py`. No separate slice needed. |
| **M7 — Helm chart + kind install** | Complete (chart shipped; install step is part of M5) | `k8s/chart/` — `Chart.yaml`, `values.yaml`, `templates/{crd,namespace,serviceaccount,clusterrole,clusterrolebinding,deployment}.yaml`, `_helpers.tpl`. README at `k8s/chart/README.md`. The `helm install` on kind is executed as part of the M5 runbook. |
| **M8 — deploy-sentinel skill** | Complete (skill shipped; first-real-service dogfood is deferred) | `adapters/claude/skills/deploy-sentinel/SKILL.md` + `README.md`. Reads the Sentinel dir, derives repo URL / branch / path via `git`, prompts for namespace, inputs, schedule, capability provider secrets, sinks, writes `sentinel-cr.yaml` next to the Sentinel. Dogfooding in `~/workspace/lakerunner` is the "what to do next" item below. |
| **M9 — Install on prod EKS via ArgoCD** | **Runbook-only** | Needs prod cluster access + write on `cardinalhq/kubernetes-clusters`; CI cannot execute it. See [`k8s/docs/M9-prod-deploy-runbook.md`](../../k8s/docs/M9-prod-deploy-runbook.md). |

## Call-outs

- **M5 and M9 are runbooks, not code.** Both require environments the CI
  workflow cannot reach (a real docker + kind cluster for M5, prod EKS + write
  access to `kubernetes-clusters` for M9). The runbooks are step-by-step so a
  human can execute them in one sitting.
- **M6 is not a separate deliverable.** Its scope (capability binding
  projection, `SecretRef` env wiring, `spec.schedule` → CronJob branch) fell
  out naturally while implementing M2 (`capabilities.py`, projection tests)
  and M4 (`handlers.py`). The plan's numbering is preserved in this table for
  traceability.

## What to do next

1. **Execute the M5 runbook.** Prove end-to-end on a local kind cluster —
   controller reconciles the fixture Sentinel CR into a Pod that clones
   `cardinal-agent-plugins@main:mechanize-out/f89df52b-v2`, executes it, and
   emits `FINDING` lines. This is the last gate before anything touches prod.
2. **Execute the M9 runbook.** PR into `cardinalhq/kubernetes-clusters` to
   install the controller on `aws-prod-us-east-2-global` via ArgoCD; verify
   the controller Deployment is Running in `sentinel-system`; seed the
   Cardinal MCP secrets in whichever service namespace ships first.
3. **Dogfood `/cardinal:deploy-sentinel` in `~/workspace/lakerunner`.** Take a
   real lakerunner investigation session, run `/mechanize`, then
   `/cardinal:deploy-sentinel sentinels/<id>/`. Add lakerunner's one-time
   deploy-glue (Kustomization / Helm / ApplicationSet — whichever it uses).
   `git push`. Watch the CR land, the controller reconcile, and findings
   appear. This is the [plan's success criterion](sentinel-crd.md#success-criterion).

Phase (c) items — admission webhook, `SentinelRun` history CRD, event-driven
triggers, image-baked Sentinels, controller HA, per-tenant RBAC beyond
namespaces — are unchanged; see the plan's ["What (b) explicitly defers to
(c)"](sentinel-crd.md#what-b-explicitly-defers-to-c) section.
