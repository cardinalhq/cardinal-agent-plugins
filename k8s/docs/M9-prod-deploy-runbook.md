# M9 — install the sentinel-controller on prod EKS via ArgoCD

Milestone M9 of the [Sentinel CRD plan](../../docs/specs/sentinel-crd.md#milestones):
one-time platform install of the controller on the prod EKS cluster
(`aws-prod-us-east-2-global`) via ArgoCD.

This runbook is executed **by a human** with prod cluster access + write access
to `cardinalhq/kubernetes-clusters` — CI cannot execute it.

## Prerequisites

- `kubectl` context for `aws-prod-us-east-2-global` (verify:
  `kubectl config current-context` and `kubectl get ns argocd`).
- Write access to `github.com/cardinalhq/kubernetes-clusters`.
- The controller image tag you want to pin (e.g. `v0.1.0`) has been published
  to `ghcr.io/cardinalhq/sentinel-controller` by the
  [`controller-image.yml`](../../.github/workflows/controller-image.yml) workflow (and `executor-image.yml` published the executor image the CR references).
- The M5 kind runbook has been executed cleanly on the same commit.

## Steps

### 1. Author the ArgoCD wiring in kubernetes-clusters

Add a new top-level directory in `kubernetes-clusters`:

```
sentinel-controller/
├─ application-set.yaml
└─ aws-prod-us-east-2-global/
   └─ values.yaml
```

Sketch of `application-set.yaml` (match the surrounding conventions in that
repo — this is illustrative):

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: sentinel-controller
  namespace: argocd
spec:
  generators:
    - list:
        elements:
          - cluster: aws-prod-us-east-2-global
  template:
    metadata:
      name: sentinel-controller-{{cluster}}
    spec:
      project: platform
      source:
        repoURL: https://github.com/cardinalhq/cardinal-agent-plugins
        targetRevision: main            # or a pinned tag
        path: k8s/chart
        helm:
          valueFiles:
            - $values/sentinel-controller/{{cluster}}/values.yaml
      destination:
        server: https://kubernetes.default.svc
        namespace: sentinel-system
      syncPolicy:
        automated: { prune: true, selfHeal: true }
        syncOptions:
          - CreateNamespace=true
          - ServerSideApply=true
```

`aws-prod-us-east-2-global/values.yaml` pins the image and any cluster-specific
overrides:

```yaml
# Pin the Deployment name so `kubectl -n sentinel-system get deploy sentinel-controller`
# works regardless of the ArgoCD release name (which is per-cluster and would
# otherwise flow into the fullname template as `sentinel-controller-<cluster>`).
fullnameOverride: sentinel-controller
image:
  repository: ghcr.io/cardinalhq/sentinel-controller
  tag: v0.1.0
controllerNamespace: sentinel-system
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<acct>:role/sentinel-controller
logLevel: info
```

### 2. PR against kubernetes-clusters

```sh
cd ~/workspace/kubernetes-clusters
git checkout -b add-sentinel-controller
git add sentinel-controller/
git commit -m "install sentinel-controller on aws-prod-us-east-2-global"
git push -u origin add-sentinel-controller
gh pr create --fill
```

Get review + merge per the usual `kubernetes-clusters` gate.

### 3. Wait for ArgoCD to sync

```sh
kubectl -n argocd get application sentinel-controller-aws-prod-us-east-2-global -w
# STATUS should progress: OutOfSync/Missing → Synced/Healthy
```

Or watch in the ArgoCD UI.

### 4. Verify the controller

```sh
kubectl get crd sentinels.sentinels.cardinalhq.io
kubectl -n sentinel-system get deploy sentinel-controller
kubectl -n sentinel-system rollout status deploy/sentinel-controller
kubectl -n sentinel-system logs deploy/sentinel-controller --tail=50
```

Success: Deployment is `1/1 READY`, logs show kopf `Ready` and no permission
errors.

### 5. Seed the shared Cardinal MCP secrets in target namespaces

Any service namespace that will ship a Sentinel needs the secrets its CR
references (`endpointSecretRef`, `tokenSecretRef`, Slack `channelSecretRef`,
etc.). These are shared across Sentinels within a namespace — create them
**once** per namespace if they aren't already present:

```sh
kubectl -n <service-ns> get secret cardinal-mcp-endpoint cardinal-mcp-token \
                                    sentinel-findings-slack 2>/dev/null || \
  echo "missing — create before applying any Sentinel CR in <service-ns>"

# Example (values from 1Password / your platform secret store):
kubectl -n <service-ns> create secret generic cardinal-mcp-endpoint \
  --from-literal=endpoint=https://mcp.cardinalhq.io
kubectl -n <service-ns> create secret generic cardinal-mcp-token \
  --from-file=token=/path/to/token
```

Without these, Sentinel CRs in that namespace will land as
`phase: Blocked / CapabilitiesBound: False` until the secrets exist.

## Rollback

```sh
kubectl -n argocd delete application sentinel-controller-aws-prod-us-east-2-global
```

The ArgoCD `automated.prune` policy cleans up the controller Deployment,
ServiceAccount, and RBAC. The `Sentinel` CRD is annotated
`helm.sh/resource-policy: keep` — remove it explicitly if you want a full
teardown:

```sh
kubectl delete crd sentinels.sentinels.cardinalhq.io
kubectl delete namespace sentinel-system
```

Revert the `kubernetes-clusters` commit to make the removal durable.
