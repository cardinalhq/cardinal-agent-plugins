# k8s/ — Cardinal Sentinels deploy surface

This directory is the Kubernetes deploy surface for **Cardinal Sentinels**: the
`Sentinel` CustomResourceDefinition, the kopf controller that reconciles
Sentinel CRs into `Job` / `CronJob` workloads, and the Helm chart that installs
both onto a cluster.

Sentinel CRs themselves live in **service repos** (alongside the code they
monitor) — not here. This directory only ships the platform.

## Layout

```
k8s/
├─ crds/         CustomResourceDefinition manifest for sentinels.cardinalhq.io/v1alpha1
├─ controller/   kopf controller (Python): reconciler, projections, handlers, tests
├─ chart/        Helm chart that installs the CRD + controller + RBAC
└─ docs/         Runbooks for validation on kind and rollout on prod EKS
```

One-line summaries:

- **`crds/sentinel.yaml`** — the `Sentinel` CRD schema (namespaced,
  `sentinels.cardinalhq.io/v1alpha1`). Ships with the chart via
  `chart/templates/crd.yaml`.
- **`controller/`** — kopf handlers + a pure-Python reconciler and projections
  module (unit-tested, no k8s client dependency in the reconcile logic).
- **`chart/`** — installs the CRD, ServiceAccount, ClusterRole +
  ClusterRoleBinding, and the single-replica controller Deployment into
  `sentinel-system` (configurable).
- **`docs/`** — [M5 validation runbook](docs/M5-validation-runbook.md) (kind
  end-to-end) and [M9 prod deploy runbook](docs/M9-prod-deploy-runbook.md)
  (ArgoCD install on prod EKS).

## Install the controller

```sh
helm install sentinel-controller ./k8s/chart \
  --namespace sentinel-system --create-namespace
```

That is the whole platform install. Every Sentinel after this ships from a
service repo — see `/cardinal:deploy-sentinel` below.

See [`chart/README.md`](chart/README.md) for values, RBAC, and upgrade
semantics.

## Develop the controller locally

The controller is pure Python. Point it at any kubeconfig context.

```sh
pip install -r k8s/controller/requirements.txt kopf kubernetes
kopf run k8s/controller/handlers.py --namespace <ns>
```

`--namespace` narrows kopf to a single tenant namespace; drop it to watch all
namespaces (matches the in-cluster default).

## Run the tests

```sh
cd k8s/controller && pytest
```

Tests cover the reconciler (spec-hash stability, Job/CronJob shape),
projections (inputs / deployment merging), capability-binding validation, and
the kopf handler wiring. No cluster required.

## Author a Sentinel CR from a service repo

Do not hand-write CRs. From inside the service repo whose Sentinel you want to
ship, in Claude Code with the Cardinal plugin installed:

```
/cardinal:deploy-sentinel <sentinel-dir>
```

The skill reads the Sentinel directory (produced by `/mechanize`), discovers
the repo URL / branch / path via `git`, prompts for the deploy-time bindings
(namespace, inputs, schedule, capability providers, sinks), and writes
`sentinel-cr.yaml` next to the Sentinel. Full skill:
[`adapters/claude/skills/deploy-sentinel/SKILL.md`](../adapters/claude/skills/deploy-sentinel/SKILL.md).

After `git push`, the service repo's existing deploy pipeline applies the CR
and the controller reconciles it into a Job or CronJob.

## References

- Plan: [`docs/specs/sentinel-crd.md`](../docs/specs/sentinel-crd.md)
- Status: [`docs/specs/sentinel-crd-status.md`](../docs/specs/sentinel-crd-status.md)
- CRD: [`crds/sentinel.yaml`](crds/sentinel.yaml)
- Executor image (what every Sentinel Pod runs):
  [`spike/executor/Dockerfile`](../spike/executor/Dockerfile)
