# M5 — end-to-end validation on kind

Milestone M5 of the [Sentinel CRD plan](../../docs/specs/sentinel-crd.md#milestones):
prove a Sentinel CR reconciles into a Pod that git-clones its source,
executes, and emits `FINDING` lines on a local kind cluster.

This runbook is executed **by a human** on a workstation with a working docker
daemon — the CI workflow cannot execute it because it needs a real cluster.

## Prerequisites

- `docker` running (Docker Desktop, colima, orbstack, etc.)
- `kind` ≥ 0.20 — `brew install kind`
- `kubectl` ≥ 1.28 — `brew install kubectl`
- `helm` ≥ 3.13 — `brew install helm`
- Clone of `cardinal-agent-plugins` at the commit you want to validate.

## Steps

### 1. Create a kind cluster

```sh
kind create cluster --name sentinel-e2e
kubectl config use-context kind-sentinel-e2e
kubectl get nodes    # should show one control-plane node Ready
```

### 2. Build both images locally

The CI workflow (`.github/workflows/executor-image.yml`) publishes these on tag
pushes; for local validation we build them fresh.

```sh
# Executor — build context is the repo root because the Dockerfile
# COPYs both spike/executor/ and common/mechanize/.
docker build -t ghcr.io/cardinalhq/sentinel-executor:dev -f spike/executor/Dockerfile .

# Controller — narrow context (no imports outside k8s/controller/).
docker build -t ghcr.io/cardinalhq/sentinel-controller:dev -f k8s/controller/Dockerfile k8s/controller
```

### 3. Load both images into kind

```sh
kind load docker-image --name sentinel-e2e ghcr.io/cardinalhq/sentinel-executor:dev
kind load docker-image --name sentinel-e2e ghcr.io/cardinalhq/sentinel-controller:dev
```

### 4. Install the controller via Helm

```sh
helm install sentinel-controller ./k8s/chart \
  --namespace sentinel-system --create-namespace \
  --set image.repository=ghcr.io/cardinalhq/sentinel-controller \
  --set image.tag=dev \
  --set image.pullPolicy=IfNotPresent

kubectl -n sentinel-system rollout status deploy/sentinel-controller --timeout=120s
kubectl -n sentinel-system logs deploy/sentinel-controller -f &
```

### 5. Apply a sample Sentinel CR

Points at the checked-in Sentinel directory in this repo so no external service
is needed.

```yaml
# /tmp/sample-sentinel.yaml
apiVersion: sentinels.cardinalhq.io/v1alpha1
kind: Sentinel
metadata:
  name: f89df52b-v2-smoke
  namespace: default
spec:
  source:
    git:
      url: https://github.com/cardinalhq/cardinal-agent-plugins
      ref: main
      path: mechanize-out/f89df52b-v2
  inputs: {}
  runtime:
    image: ghcr.io/cardinalhq/sentinel-executor:dev
    timeoutSeconds: 300
  sinks:
    - id: stdout
```

```sh
kubectl apply -f /tmp/sample-sentinel.yaml
```

### 6. Watch the reconcile

```sh
kubectl get sentinel -A -w                     # phase: Pending → Reconciling → Running → Succeeded
kubectl get jobs -A -w                         # a Job appears in default/
kubectl logs -l 'sentinels.cardinalhq.io/sentinel=f89df52b-v2-smoke' -n default -f
kubectl describe sentinel f89df52b-v2-smoke -n default
```

## Success checklist

- [ ] `kubectl get crd sentinels.sentinels.cardinalhq.io` returns the CRD.
- [ ] `kubectl -n sentinel-system get deploy sentinel-controller` shows
      `1/1 READY`.
- [ ] Controller logs show `Handler 'sentinel_create' succeeded` (or the kopf
      equivalent) for the sample CR.
- [ ] `kubectl get jobs -n default` shows a `sentinel-f89df52b-v2-smoke-<hash>`
      Job.
- [ ] The Job's Pod goes `Init:0/1 → Init:1/1 → Running → Completed`.
- [ ] Pod logs contain at least one `FINDING` line (stdout sink prefix).
- [ ] `kubectl describe sentinel f89df52b-v2-smoke` shows
      `phase: Succeeded`, `lastRunResult: Succeeded`, `findingsCount ≥ 0`,
      and `SourceResolved: True` / `CapabilitiesBound: True` conditions.
- [ ] `kubectl delete sentinel f89df52b-v2-smoke` cascades — the owned Job and
      projected Secret both disappear.

## Common failure modes

| Symptom | Diagnose | Fix |
|---|---|---|
| Pod stuck `ImagePullBackOff` on `sentinel-executor:dev` or `sentinel-controller:dev` | `kubectl describe pod ...` shows the pull error. Kind pulls from the node's containerd, not the host docker. | Re-run `kind load docker-image` for the missing tag. Set `image.pullPolicy=IfNotPresent`. |
| initContainer `Error` — pod logs "git clone timed out / could not resolve host" | Node has no outbound network, or the repo URL / ref is wrong. | Check `spec.source.git.url` and `ref` resolve from a shell inside the node: `docker exec -it sentinel-e2e-control-plane sh` then `nslookup github.com`. Add `credentialsSecretRef` if the repo is private. |
| Sentinel stuck `phase: Blocked`, `CapabilitiesBound: False` | Sentinel's own `sentinel.yaml` declares `spec.capabilities.required[]` entries that the CR does not bind under `spec.capabilities[]`. | `kubectl describe sentinel <name>` — the condition `message` names the missing capability id. Add a matching `spec.capabilities[]` entry with the right `provider` + secret refs. |
| Controller pod `CrashLoopBackOff` with `Forbidden` in the log | RBAC missing a verb (usually because the ClusterRole drifted from `handlers.py`). | `kubectl auth can-i --as=system:serviceaccount:sentinel-system:sentinel-controller <verb> <resource>` to confirm. Add the verb to `k8s/chart/templates/clusterrole.yaml`, `helm upgrade`. |
| Job runs but `findingsCount` stays 0 even though logs show `FINDING ...` | Log-parser regex mismatch (findings prefix differs from what the reconciler expects). | Compare the actual log prefix against `k8s/controller/handlers.py`'s parser. This is best-effort in phase (b) — real accounting is Phase (c). |

## Teardown

```sh
kubectl delete sentinel --all -A
helm uninstall sentinel-controller -n sentinel-system
kind delete cluster --name sentinel-e2e
```
