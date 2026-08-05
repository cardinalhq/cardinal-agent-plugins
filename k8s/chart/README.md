# sentinel-controller Helm chart

Installs the Cardinal **Sentinel controller** on a Kubernetes cluster: the
`Sentinel` CRD (`sentinels.cardinalhq.io/v1alpha1`), a single-replica
controller Deployment, its ServiceAccount, and the cluster-wide RBAC it
needs to reconcile Sentinel CRs into `Job` / `CronJob` workloads.

After this chart is installed, service repos ship their own Sentinel CRs
(via `/cardinal:deploy-sentinel` + their existing deploy pipeline). The
controller watches for those CRs and does the rest.

---

## Install

Add the chart directory to your working tree (this chart lives in
`k8s/chart/` in `cardinal-agent-plugins`):

```sh
helm install sentinel-controller ./k8s/chart
```

The chart is self-contained: it creates the `sentinel-system` namespace
(configurable), installs the CRD, creates the ServiceAccount +
ClusterRole + ClusterRoleBinding, and rolls out the controller
Deployment.

Verify:

```sh
kubectl get crd sentinels.sentinels.cardinalhq.io
kubectl -n sentinel-system get deploy sentinel-controller
kubectl -n sentinel-system logs deploy/sentinel-controller -f
```

## Upgrade

```sh
helm upgrade sentinel-controller ./k8s/chart
```

The `Sentinel` CRD is annotated `helm.sh/resource-policy: keep`, so
`helm uninstall` leaves the CRD (and any Sentinel CRs) in place. Delete
it explicitly if you truly want it gone:

```sh
kubectl delete crd sentinels.sentinels.cardinalhq.io
```

## Values you typically override

| Value                              | Default                                             | Why override                                                                             |
| ---------------------------------- | --------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `image.repository`                 | `ghcr.io/cardinalhq/sentinel-controller`            | Point at a mirror or private registry.                                                   |
| `image.tag`                        | chart `appVersion`                                  | Pin to a specific controller build.                                                      |
| `image.pullPolicy`                 | `IfNotPresent`                                      | Set to `Always` while iterating on a mutable tag.                                        |
| `imagePullSecrets`                 | `[]`                                                | Pull from a private registry (list of `{ name }` entries).                               |
| `controllerNamespace`              | `sentinel-system`                                   | Place the controller in an existing platform namespace instead.                          |
| `watchNamespaces`                  | `[]` (all namespaces)                               | Narrow the controller to a specific set of tenant namespaces.                            |
| `logLevel`                         | `info`                                              | `debug` while investigating a reconcile problem.                                         |
| `resources`                        | 100m/128Mi requests, 500m/512Mi limits              | Bump if the controller is watching many Sentinels.                                       |
| `serviceAccount.create`            | `true`                                              | Set `false` and provide `serviceAccount.name` when the SA is pre-created (e.g. IRSA).    |
| `serviceAccount.name`              | `sentinel-controller`                               | Match a pre-existing SA when `create: false`.                                            |
| `serviceAccount.annotations`       | `{}`                                                | Attach an IRSA role ARN on EKS.                                                          |
| `crd.install`                      | `true`                                              | Set `false` when the CRD is managed out-of-band (e.g. a separate bootstrap step).        |
| `nodeSelector` / `tolerations` / `affinity` | empty                                      | Pin the controller to a specific node pool.                                              |
| `extraEnv`                         | `[]`                                                | Pass extra env vars to the controller (e.g. proxy configuration).                        |

Full defaults live in [`values.yaml`](./values.yaml).

## The `sentinel-system` namespace

By default, the controller runs in a dedicated `sentinel-system`
namespace. The chart creates it as a `pre-install` hook and keeps it
around with `helm.sh/resource-policy: keep`, so re-installs and
uninstalls don't churn it. If the namespace already exists (created by
your cluster bootstrap), the pre-install apply is idempotent.

Set `controllerNamespace` to place the controller elsewhere (e.g.
`argocd`, `platform-system`) — but note that Sentinel CRs themselves
still live in the *service's* own namespace. The controller-namespace
choice only affects where the controller Pod runs.

## RBAC

The chart installs a `ClusterRole` + `ClusterRoleBinding` because
`watchNamespaces` defaults to all-namespaces. The role grants:

- `sentinels.cardinalhq.io/sentinels` — get/list/watch/patch/update (+ status)
- `batch/jobs`, `batch/cronjobs` — full CRUD (owned by each Sentinel CR)
- `""/secrets` — full CRUD (projected inputs/deployment secrets per run)
- `""/pods`, `""/pods/log` — read-only (findings-count log parsing)
- `""/events`, `events.k8s.io/events` — create/patch (kopf event emission)
- `""/namespaces` — get/list/watch (cluster-wide discovery)

If you narrow `watchNamespaces` and want to swap to Role/RoleBinding
scoped to those namespaces only, that is a Phase (c) enhancement — the
minimal cut keeps the RBAC surface uniform.

## Adding a Sentinel

The chart installs the platform. To ship an actual Sentinel:

1. From a service repo, run `/mechanize` in Claude Code to compile a
   past investigation into a Sentinel directory.
2. Run `/cardinal:deploy-sentinel <dir>` to author the `Sentinel` CR.
3. `git commit && git push`. Your service's existing deploy pipeline
   (Kustomization / Helm / ArgoCD) applies the CR alongside the service.
4. `kubectl get sentinel -A` — the controller picks it up.

See `docs/specs/sentinel-crd.md` in this repo for the full flow.

## Uninstall

```sh
helm uninstall sentinel-controller
```

The `sentinel-system` namespace and `Sentinel` CRD are intentionally
retained. Delete them explicitly if you want to fully remove the
platform:

```sh
kubectl delete crd sentinels.sentinels.cardinalhq.io
kubectl delete namespace sentinel-system
```
