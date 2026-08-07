"""Pure-python reconciler for the Sentinel CRD (M2).

Given a Sentinel custom resource as a plain dict, ``reconcile_sentinel``
returns a :class:`ReconcileResult` describing what the kopf handler layer
(M4) should apply to the cluster:

* a Kubernetes ``Job`` manifest (one-shot) or ``CronJob`` manifest
  (``spec.schedule`` present), wrapping the executor pod;
* a projected ``Secret`` mounted at ``/config`` in the pod, carrying the
  merged ``inputs.json`` / ``deployment.yaml``;
* a ``phase`` + ``conditions`` list for the CR ``status`` subresource.

No k8s client is imported here — the function is pure so it can be unit
tested end-to-end with plain ``pytest``. Owner references are populated on
every returned manifest so ``kubectl delete sentinel`` cascades.

Job / CronJob naming is deterministic: ``sentinel-<name>-<hash8>`` where the
hash covers only the *behavioural* subset of the spec (source ref, inputs,
capabilities, sinks, runtime). Unrelated mutations to ``metadata.labels`` or
``status`` do not change the name — the same spec always reconciles to the
same object, which is what lets the controller no-op idempotently.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from projections import capability_bindings, project_deployment, project_inputs


API_VERSION = "sentinels.cardinalhq.io/v1alpha1"
KIND = "Sentinel"
# Must track spike/executor/VERSION, which is what .github/workflows/
# executor-image.yml publishes as v<VERSION>. Two floors, both load-bearing:
#   * v0.1.0 was amd64-only and fails to pull on the prod arm64 Graviton
#     nodes; multi-arch builds start at v0.1.1.
#   * the `mcp` capability provider (spike/executor/providers/mcp.py) first
#     ships in v0.1.3. Any tag below that raises UnknownProviderError at the
#     first tool node of a CR that binds `provider: mcp` — which is what
#     every live-telemetry Sentinel binds.
# Two further floors start at v0.1.4, both reachable from a valid Sentinel:
#   * `provider: fixture` bound to a capability id outside the six in
#     capabilities._FIXTURE_CAPABILITIES raises UnknownProviderError on
#     v0.1.3 and below, even with the fixture file present.
#   * a nested `?:` in a `severityExpression` whose branch is parenthesised
#     (`c1 ? x : (c2 ? y : z)`) fails the emit node on v0.1.3 and below.
# Bump in lockstep with spike/executor/VERSION; the parity is pinned by
# tests/test_reconciler.py and spike/executor/tests/test_release_versions.py.
DEFAULT_EXECUTOR_IMAGE = "ghcr.io/cardinalhq/sentinel-executor:v0.1.4"
GIT_INIT_IMAGE = "alpine/git:latest"
SENTINEL_DIR = "/sentinel"
CONFIG_DIR = "/config"


@dataclass
class ReconcileResult:
    phase: str
    conditions: list[dict] = field(default_factory=list)
    job_or_cronjob: dict | None = None
    projected_secret: dict | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Public entrypoint                                                           #
# --------------------------------------------------------------------------- #

def reconcile_sentinel(sentinel: dict) -> ReconcileResult:
    """Reconcile a Sentinel CR dict into the manifests the controller applies."""
    if not isinstance(sentinel, dict):
        return ReconcileResult(
            phase="Blocked",
            conditions=[_condition("SpecInvalid", "False", "NotAMapping",
                                   "Sentinel CR must be a mapping")],
            error="sentinel is not a dict",
        )

    metadata = sentinel.get("metadata") or {}
    name = metadata.get("name")
    namespace = metadata.get("namespace")
    uid = metadata.get("uid")
    spec = sentinel.get("spec") or {}

    missing: list[str] = []
    if not name:
        missing.append("metadata.name")
    if not namespace:
        missing.append("metadata.namespace")
    if not uid:
        missing.append("metadata.uid")
    if not spec:
        missing.append("spec")
    else:
        source = spec.get("source") or {}
        git = source.get("git") or {}
        for req in ("url", "ref", "path"):
            if not git.get(req):
                missing.append(f"spec.source.git.{req}")

    if missing:
        msg = "missing required field(s): " + ", ".join(missing)
        return ReconcileResult(
            phase="Blocked",
            conditions=[_condition("SpecInvalid", "False", "MissingFields", msg)],
            error=msg,
        )

    # spec.capabilities is validated up front because the CRD schema does NOT
    # catch its failure modes: ``required: [id, provider]`` only checks
    # presence (a blank provider passes), the array has no uniqueness
    # constraint (a duplicate id passes), and nothing knows that two distinct
    # ids can collide onto one CARDINAL_CAP_<SLUG>_* pair. The API server
    # therefore accepts such a CR happily and it lands here. Letting
    # ``capability_bindings``' ValueError escape would blow up the kopf
    # handler and spin the Sentinel in a retry loop over input that can never
    # become valid without an edit, so it is converted into the same terminal
    # Blocked result the missing-field checks above produce. The validation
    # itself deliberately stays in projections.py; this is only the seam that
    # turns it into operator-visible status.
    try:
        capability_bindings(spec.get("capabilities"))
    except (ValueError, TypeError) as exc:
        msg = f"spec.capabilities is invalid: {exc}"
        return ReconcileResult(
            phase="Blocked",
            conditions=[
                _condition("CapabilitiesBound", "False", "InvalidCapabilities", msg),
            ],
            error=msg,
        )

    hash8 = _spec_hash(spec)
    obj_name = f"sentinel-{name}-{hash8}"
    owner_ref = _owner_ref(name, uid)

    # Projected config — no dir-side inputs/deployment available at the pure
    # layer, so the merge base is empty. The kopf handler layer (M3+) hydrates
    # the dir side from its cached git clone before calling reconcile again.
    # Same reasoning as the capability check above, for the remaining
    # projectable spec fields (spec.inputs must be a mapping, spec.sinks a
    # list — neither shape is enforced by the CRD's x-kubernetes-preserve
    # sections).
    try:
        inputs_json = project_inputs(spec.get("inputs"), {})
        deployment_yaml = project_deployment(
            spec.get("sinks"), spec.get("capabilities"), {}
        )
    except (ValueError, TypeError) as exc:
        msg = f"spec cannot be projected into pod config: {exc}"
        return ReconcileResult(
            phase="Blocked",
            conditions=[_condition("SpecInvalid", "False", "InvalidProjection", msg)],
            error=msg,
        )

    secret = _build_secret(
        name=obj_name,
        namespace=namespace,
        owner_ref=owner_ref,
        inputs_json=inputs_json,
        deployment_yaml=deployment_yaml,
        sentinel_name=name,
    )

    pod_spec = _build_pod_spec(spec, secret_name=obj_name)
    runtime_cfg = spec.get("runtime") or {}
    # If the user sets only spec.runtime.timeoutSeconds (soft executor
    # timeout), default the Job's hard limit to it so a hung Sentinel
    # can't run forever. When both are set, honor activeDeadlineSeconds
    # unchanged — belt-and-suspenders as documented in the CRD.
    active_deadline = (
        runtime_cfg.get("activeDeadlineSeconds")
        or runtime_cfg.get("timeoutSeconds")
    )
    job_manifest = _build_job(
        name=obj_name,
        namespace=namespace,
        owner_ref=owner_ref,
        sentinel_name=name,
        pod_spec=pod_spec,
        active_deadline_seconds=active_deadline,
    )

    schedule = spec.get("schedule")
    if schedule:
        top = _build_cronjob(
            name=obj_name,
            namespace=namespace,
            owner_ref=owner_ref,
            sentinel_name=name,
            schedule=schedule,
            job_manifest=job_manifest,
        )
    else:
        top = job_manifest

    conditions = [
        _condition("SpecInvalid", "False", "Valid", "spec passed structural checks"),
        _condition("CapabilitiesBound", "True", "BindingsPresent",
                   "capability validation deferred to handler with dir-side yaml"),
    ]

    return ReconcileResult(
        phase="Reconciling",
        conditions=conditions,
        job_or_cronjob=top,
        projected_secret=secret,
        error=None,
    )


# --------------------------------------------------------------------------- #
# Hashing                                                                     #
# --------------------------------------------------------------------------- #

def _spec_hash(spec: dict) -> str:
    """First 8 hex chars of sha256 over the behavioural subset of the spec.

    Only the fields that actually change the pod's behaviour are hashed:
    the full source.git triple (url + ref + path so cross-repo/path
    Sentinels with the same metadata.name cannot collide onto the same
    Job name), inputs, capabilities, sinks, runtime. Everything else
    (metadata.labels, status, spec.schedule cadence changes on the CronJob
    wrapper) is deliberately excluded so unrelated edits don't churn the
    downstream Job name.
    """
    git = ((spec.get("source") or {}).get("git") or {})
    payload = {
        "source_url": git.get("url"),
        "source_ref": git.get("ref"),
        "source_path": git.get("path"),
        "inputs": spec.get("inputs") or {},
        "capabilities": spec.get("capabilities") or [],
        "sinks": spec.get("sinks") or [],
        "runtime": spec.get("runtime") or {},
    }
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------- #
# Manifest builders                                                           #
# --------------------------------------------------------------------------- #

def _owner_ref(name: str, uid: str) -> dict:
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "name": name,
        "uid": uid,
        "controller": True,
        "blockOwnerDeletion": True,
    }


def _condition(
    ctype: str, status: str, reason: str, message: str
) -> dict:
    return {
        "type": ctype,
        "status": status,
        "reason": reason,
        "message": message,
    }


def _build_secret(
    *,
    name: str,
    namespace: str,
    owner_ref: dict,
    inputs_json: str,
    deployment_yaml: str,
    sentinel_name: str,
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "sentinel-controller",
                "sentinels.cardinalhq.io/sentinel": sentinel_name,
            },
            "ownerReferences": [owner_ref],
        },
        "data": {
            "inputs.json": base64.b64encode(inputs_json.encode("utf-8")).decode("ascii"),
            "deployment.yaml": base64.b64encode(
                deployment_yaml.encode("utf-8")
            ).decode("ascii"),
        },
    }


def _cap_env_slug(cap_id: str) -> str:
    """Turn ``observability.list-services`` into ``OBSERVABILITY_LIST_SERVICES``.

    Any non-alphanumeric run becomes a single ``_`` and the result is
    upper-cased. Leading / trailing underscores from odd inputs are stripped.
    """
    if not isinstance(cap_id, str) or not cap_id:
        raise ValueError("capability id must be a non-empty string")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", cap_id).strip("_").upper()
    if not slug:
        raise ValueError(f"capability id {cap_id!r} has no alphanumerics")
    return slug


def _capability_env(cr_capabilities: list) -> list[dict]:
    env: list[dict] = []
    for cap in cr_capabilities or []:
        if not isinstance(cap, dict):
            continue
        cap_id = cap.get("id")
        if not cap_id:
            continue
        slug = _cap_env_slug(cap_id)
        endpoint_ref = cap.get("endpointSecretRef")
        token_ref = cap.get("tokenSecretRef")
        if endpoint_ref:
            env.append({
                "name": f"CARDINAL_CAP_{slug}_ENDPOINT",
                "valueFrom": {
                    "secretKeyRef": {"name": endpoint_ref, "key": "endpoint"},
                },
            })
        if token_ref:
            env.append({
                "name": f"CARDINAL_CAP_{slug}_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {"name": token_ref, "key": "token"},
                },
            })
    return env


def _build_pod_spec(spec: dict, *, secret_name: str) -> dict:
    source_git = ((spec.get("source") or {}).get("git")) or {}
    git_url = source_git["url"]
    git_ref = source_git["ref"]
    git_path = source_git["path"]
    credentials_ref = source_git.get("credentialsSecretRef")

    runtime = spec.get("runtime") or {}
    image = runtime.get("image") or DEFAULT_EXECUTOR_IMAGE
    resources = runtime.get("resources") or {}

    workdir = f"{SENTINEL_DIR}/{git_path}"

    volumes: list[dict] = [
        {"name": "sentinel-src", "emptyDir": {}},
        {
            "name": "sentinel-config",
            "secret": {"secretName": secret_name},
        },
    ]

    init_env: list[dict] = [
        {"name": "GIT_URL", "value": git_url},
        {"name": "GIT_REF", "value": git_ref},
    ]
    init_volume_mounts: list[dict] = [
        {"name": "sentinel-src", "mountPath": SENTINEL_DIR},
    ]
    if credentials_ref:
        volumes.append({
            "name": "git-credentials",
            "secret": {"secretName": credentials_ref, "defaultMode": 0o400},
        })
        init_volume_mounts.append({
            "name": "git-credentials",
            "mountPath": "/etc/git-secret",
            "readOnly": True,
        })
        init_env.append({"name": "GIT_SSH_COMMAND",
                         "value": "ssh -i /etc/git-secret/ssh -o StrictHostKeyChecking=no"})

    init_container = {
        "name": "git-clone",
        "image": GIT_INIT_IMAGE,
        "command": ["sh", "-c"],
        "args": [
            'set -eux; '
            'git clone "$GIT_URL" ' + SENTINEL_DIR + '; '
            'cd ' + SENTINEL_DIR + '; '
            'git checkout "$GIT_REF"'
        ],
        "env": init_env,
        "volumeMounts": init_volume_mounts,
    }

    main_env = _capability_env(spec.get("capabilities") or [])

    main_container: dict[str, Any] = {
        "name": "sentinel-executor",
        "image": image,
        "workingDir": workdir,
        "command": ["sentinel-executor"],
        "args": [
            "run", ".",
            "--inputs", f"{CONFIG_DIR}/inputs.json",
            "--deployment", f"{CONFIG_DIR}/deployment.yaml",
        ],
        "env": main_env,
        "volumeMounts": [
            {"name": "sentinel-src", "mountPath": SENTINEL_DIR},
            {"name": "sentinel-config", "mountPath": CONFIG_DIR, "readOnly": True},
        ],
    }
    if resources:
        main_container["resources"] = copy.deepcopy(resources)

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "initContainers": [init_container],
        "containers": [main_container],
        "volumes": volumes,
    }
    return pod_spec


def _build_job(
    *,
    name: str,
    namespace: str,
    owner_ref: dict,
    sentinel_name: str,
    pod_spec: dict,
    active_deadline_seconds: int | None,
) -> dict:
    job_spec: dict[str, Any] = {
        "backoffLimit": 0,
        "template": {
            "metadata": {
                "labels": {
                    "app.kubernetes.io/managed-by": "sentinel-controller",
                    "sentinels.cardinalhq.io/sentinel": sentinel_name,
                },
            },
            "spec": pod_spec,
        },
    }
    if active_deadline_seconds is not None:
        job_spec["activeDeadlineSeconds"] = active_deadline_seconds

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "sentinel-controller",
                "sentinels.cardinalhq.io/sentinel": sentinel_name,
            },
            "ownerReferences": [owner_ref],
        },
        "spec": job_spec,
    }


def _build_cronjob(
    *,
    name: str,
    namespace: str,
    owner_ref: dict,
    sentinel_name: str,
    schedule: str,
    job_manifest: dict,
) -> dict:
    # The CronJob's jobTemplate carries the same job spec — but stripped of the
    # top-level Job metadata (the CronJob's controller stamps names on children).
    job_template_spec = copy.deepcopy(job_manifest["spec"])
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "sentinel-controller",
                "sentinels.cardinalhq.io/sentinel": sentinel_name,
            },
            "ownerReferences": [owner_ref],
        },
        "spec": {
            "schedule": schedule,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 5,
            "jobTemplate": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/managed-by": "sentinel-controller",
                        "sentinels.cardinalhq.io/sentinel": sentinel_name,
                    },
                },
                "spec": job_template_spec,
            },
        },
    }
