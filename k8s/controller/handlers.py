"""kopf handlers for the Sentinel CRD (M4).

Thin adapter layer that wires the pure reconciler behind kopf events:

* ``@kopf.on.create`` + ``@kopf.on.update`` on ``sentinels.cardinalhq.io`` →
  ``reconcile`` clones the referenced Sentinel directory, validates capability
  bindings, and applies the projected ``Secret`` + ``Job``/``CronJob`` returned
  by :func:`reconciler.reconcile_sentinel`.
* ``@kopf.on.delete`` → no-op; ``ownerReferences`` cascade cleans up.
* ``@kopf.on.event`` on ``batch/v1 Job`` matching the controller's label →
  ``on_job_event`` mirrors the child Job's terminal state (Succeeded / Failed)
  back onto the owning Sentinel's ``status``, and best-effort parses pod logs
  for ``FINDING`` prefix lines to populate ``status.findingsCount``.

All k8s work is idempotent: object names are content-hashed by the reconciler,
so re-invoking with the same spec finds an existing object and no-ops instead
of churning. Transient failures (git clone flake, k8s 5xx) raise
``kopf.TemporaryError`` so kopf retries; structural CR problems the CRD schema
should have caught raise ``kopf.PermanentError`` so kopf stops retrying and
surfaces the failure on ``status``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import kopf
import yaml
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from capabilities import capability_bindings_ok
from projections import project_deployment, project_inputs
from reconciler import reconcile_sentinel


log = logging.getLogger("sentinel-controller")

GROUP = "sentinels.cardinalhq.io"
VERSION = "v1alpha1"
PLURAL = "sentinels"
MANAGED_LABEL = "sentinels.cardinalhq.io/sentinel"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
CONTROLLER_NAME = "sentinel-controller"

# Where the controller pod caches per-ref git clones so repeated reconciles of
# the same Sentinel don't re-clone. Overridable so unit tests can point it at
# a tmp path.
CACHE_ROOT = Path(os.environ.get("SENTINEL_CACHE_ROOT", "/var/cache/sentinel-controller"))


# --------------------------------------------------------------------------- #
# Git clone cache                                                             #
# --------------------------------------------------------------------------- #

def _cache_key(url: str, ref: str) -> str:
    return hashlib.sha1(f"{url}@{ref}".encode("utf-8")).hexdigest()[:16]


def _git_clone_cached(url: str, ref: str, *, cache_root: Path = CACHE_ROOT) -> Path:
    """Clone ``url`` at ``ref`` into ``cache_root/<hash>``, reusing an existing
    checkout when the ref already matches. Returns the checkout directory.

    Raises ``kopf.TemporaryError`` on network / git failures — the reconcile is
    retried by kopf rather than parked as Blocked.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    dest = cache_root / _cache_key(url, ref)
    try:
        if dest.exists() and (dest / ".git").is_dir():
            # Best-effort: fetch and checkout the requested ref again. If this
            # fails we wipe and re-clone from scratch below.
            try:
                subprocess.run(
                    ["git", "fetch", "--depth=1", "origin", ref],
                    cwd=dest, check=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "checkout", "FETCH_HEAD"],
                    cwd=dest, check=True, capture_output=True,
                )
                return dest
            except subprocess.CalledProcessError:
                shutil.rmtree(dest, ignore_errors=True)

        subprocess.run(
            ["git", "clone", "--depth=1", "--branch", ref, url, str(dest)],
            check=True, capture_output=True,
        )
        return dest
    except subprocess.CalledProcessError as exc:
        # Ref might be a commit SHA which --branch can't handle; retry the
        # full-clone-then-checkout path once before giving up.
        shutil.rmtree(dest, ignore_errors=True)
        try:
            subprocess.run(
                ["git", "clone", url, str(dest)], check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", ref], cwd=dest, check=True, capture_output=True,
            )
            return dest
        except subprocess.CalledProcessError as exc2:
            raise kopf.TemporaryError(
                f"git clone {url}@{ref} failed: "
                f"{exc2.stderr.decode('utf-8', 'replace') if exc2.stderr else exc}",
                delay=30,
            )


def _read_dir_side(clone_dir: Path, sentinel_path: str) -> tuple[dict, dict, dict]:
    """Read the sentinel directory's own sentinel.yaml, inputs.json,
    deployment.yaml. Missing inputs/deployment default to ``{}``.
    """
    sroot = clone_dir / sentinel_path
    sentinel_yaml_path = sroot / "sentinel.yaml"
    if not sentinel_yaml_path.is_file():
        raise kopf.PermanentError(
            f"sentinel.yaml not found at {sentinel_path}/sentinel.yaml in source repo"
        )
    with sentinel_yaml_path.open("r", encoding="utf-8") as fh:
        sentinel_yaml = yaml.safe_load(fh) or {}

    inputs_path = sroot / "inputs.json"
    if inputs_path.is_file():
        with inputs_path.open("r", encoding="utf-8") as fh:
            dir_inputs = json.load(fh) or {}
    else:
        dir_inputs = {}

    deployment_path = sroot / "deployment.yaml"
    if deployment_path.is_file():
        with deployment_path.open("r", encoding="utf-8") as fh:
            dir_deployment = yaml.safe_load(fh) or {}
    else:
        dir_deployment = {}

    return sentinel_yaml, dir_inputs, dir_deployment


# --------------------------------------------------------------------------- #
# k8s apply helpers — idempotent create-or-replace                            #
# --------------------------------------------------------------------------- #

def _ensure_secret(secret: dict) -> None:
    """Create the projected Secret; if it already exists at this name (same
    content-hash), patch in place so any dir-side edits at the same ref land.
    """
    api = k8s_client.CoreV1Api()
    ns = secret["metadata"]["namespace"]
    name = secret["metadata"]["name"]
    try:
        api.create_namespaced_secret(namespace=ns, body=secret)
        log.info("created Secret %s/%s", ns, name)
    except ApiException as exc:
        if exc.status == 409:
            api.patch_namespaced_secret(name=name, namespace=ns, body=secret)
            log.info("patched existing Secret %s/%s", ns, name)
        elif 500 <= exc.status < 600:
            raise kopf.TemporaryError(f"k8s 5xx creating Secret: {exc}", delay=30)
        else:
            raise


def _ensure_job_or_cronjob(manifest: dict) -> None:
    """Create the Job or CronJob if it doesn't already exist. When the object
    exists at the expected name (same content-hash → same spec), no-op — a
    Job's spec is immutable after creation and re-creating one would restart
    the pod, so idempotence is essential.
    """
    api = k8s_client.BatchV1Api()
    kind = manifest["kind"]
    ns = manifest["metadata"]["namespace"]
    name = manifest["metadata"]["name"]
    try:
        if kind == "Job":
            api.create_namespaced_job(namespace=ns, body=manifest)
            log.info("created Job %s/%s", ns, name)
        elif kind == "CronJob":
            api.create_namespaced_cron_job(namespace=ns, body=manifest)
            log.info("created CronJob %s/%s", ns, name)
        else:
            raise kopf.PermanentError(f"reconciler returned unexpected kind {kind!r}")
    except ApiException as exc:
        if exc.status == 409:
            # Already exists at the content-hashed name → same spec → no-op.
            # For CronJob we patch to allow schedule/suspend updates without a
            # rename; for Job we deliberately do nothing (Job spec is immutable).
            if kind == "CronJob":
                api.patch_namespaced_cron_job(name=name, namespace=ns, body=manifest)
                log.info("patched existing CronJob %s/%s", ns, name)
            else:
                log.info("Job %s/%s already exists at expected hash — no-op", ns, name)
        elif 500 <= exc.status < 600:
            raise kopf.TemporaryError(f"k8s 5xx creating {kind}: {exc}", delay=30)
        else:
            raise


# --------------------------------------------------------------------------- #
# create / update handler                                                     #
# --------------------------------------------------------------------------- #

@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
def reconcile(spec, meta, status, patch, **_):  # noqa: ARG001 - kopf signature
    """Reconcile a Sentinel CR into a projected Secret + Job / CronJob."""
    cr = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Sentinel",
        "metadata": dict(meta),
        "spec": dict(spec),
    }

    # First pass: pure reconciler produces the base manifests and validates
    # the CR shape.
    #
    # reconcile_sentinel already turns every structural problem it knows about
    # into a Blocked result, but the projection helpers it calls raise on
    # malformed input by design. Anything that escapes as ValueError/TypeError
    # is a defect in the *CR*, not a transient fault: retrying it cannot help,
    # and an unhandled raise here would make kopf spin the Sentinel forever
    # with nothing on status. Park it as Blocked with a PermanentError. Only
    # these two types are caught — kopf.TemporaryError and ApiException pass
    # straight through so genuinely transient failures still retry.
    try:
        result = reconcile_sentinel(cr)
    except (ValueError, TypeError) as exc:
        msg = f"spec rejected by the reconciler: {exc}"
        patch.status["phase"] = "Blocked"
        patch.status["conditions"] = [{
            "type": "SpecInvalid",
            "status": "False",
            "reason": "InvalidSpec",
            "message": msg,
        }]
        patch.status["observedGeneration"] = meta.get("generation")
        raise kopf.PermanentError(msg) from exc

    if result.phase == "Blocked":
        patch.status["phase"] = "Blocked"
        patch.status["conditions"] = result.conditions
        patch.status["observedGeneration"] = meta.get("generation")
        raise kopf.PermanentError(result.error or "spec invalid")

    # Fetch the Sentinel dir contents so we can (a) validate capability
    # bindings against the required list and (b) merge dir-side inputs +
    # deployment into the projected secret.
    git = ((spec.get("source") or {}).get("git")) or {}
    clone_dir = _git_clone_cached(git["url"], git["ref"])
    sentinel_yaml, dir_inputs, dir_deployment = _read_dir_side(clone_dir, git["path"])

    ok, msg = capability_bindings_ok(sentinel_yaml, spec.get("capabilities"))
    if not ok:
        patch.status["phase"] = "Blocked"
        patch.status["conditions"] = [{
            "type": "CapabilitiesBound",
            "status": "False",
            "reason": "MissingCapabilityProvider",
            "message": msg,
        }]
        patch.status["observedGeneration"] = meta.get("generation")
        # PermanentError — kopf will not retry until the CR spec changes.
        raise kopf.PermanentError(f"capability bindings incomplete: {msg}")

    # Re-project inputs / deployment now that we have the dir-side data, and
    # overwrite the corresponding keys in the base secret produced by the pure
    # reconciler (which was hydrated with empty dir sides).
    import base64
    projected_secret = result.projected_secret
    # This second projection merges the *directory* side in, so it can fail on
    # shapes the first pass never saw (e.g. a dir-side ``capabilityBindings``
    # that isn't a mapping). Still an authoring error, still not retryable.
    try:
        inputs_json = project_inputs(spec.get("inputs"), dir_inputs)
        deployment_yaml = project_deployment(
            spec.get("sinks"), spec.get("capabilities"), dir_deployment
        )
    except (ValueError, TypeError) as exc:
        msg = (
            f"projecting config from {git['path']} at {git['url']}@{git['ref']} "
            f"failed: {exc}"
        )
        patch.status["phase"] = "Blocked"
        patch.status["conditions"] = [{
            "type": "SpecInvalid",
            "status": "False",
            "reason": "InvalidProjection",
            "message": msg,
        }]
        patch.status["observedGeneration"] = meta.get("generation")
        raise kopf.PermanentError(msg) from exc
    projected_secret["data"]["inputs.json"] = base64.b64encode(
        inputs_json.encode("utf-8")
    ).decode("ascii")
    projected_secret["data"]["deployment.yaml"] = base64.b64encode(
        deployment_yaml.encode("utf-8")
    ).decode("ascii")

    # Apply.
    _ensure_secret(projected_secret)
    _ensure_job_or_cronjob(result.job_or_cronjob)

    # Phase stays Reconciling until the owned Job's own status turns
    # over — on_job_event promotes to Running / Succeeded / Failed based
    # on real Pod state. Reporting Running immediately would hide
    # ImagePullBackOff and other Pod-scheduling failures behind a
    # cheerful CR status.
    patch.status["phase"] = "Reconciling"
    patch.status["observedGeneration"] = meta.get("generation")
    patch.status["conditions"] = result.conditions + [{
        "type": "SourceResolved",
        "status": "True",
        "reason": "GitCloneOK",
        "message": f"cloned {git['url']}@{git['ref']}",
    }]
    return {"reconciled": result.job_or_cronjob["metadata"]["name"]}


# --------------------------------------------------------------------------- #
# delete handler — ownerReferences cascade, so just log                       #
# --------------------------------------------------------------------------- #

@kopf.on.delete(GROUP, VERSION, PLURAL)
def on_delete(meta, **_):  # noqa: ARG001
    log.info(
        "Sentinel %s/%s deleted — owned Secret + Job/CronJob will cascade",
        meta.get("namespace"), meta.get("name"),
    )


# --------------------------------------------------------------------------- #
# Job event handler — mirror terminal state back onto the parent Sentinel     #
# --------------------------------------------------------------------------- #

@kopf.on.event(
    "batch", "v1", "jobs",
    labels={MANAGED_BY_LABEL: CONTROLLER_NAME},
)
def on_job_event(event, **_):  # noqa: ARG001
    """When a controller-owned Job reaches a terminal state, mirror the result
    onto the owning Sentinel's status. Best-effort: log and return on any
    missing-parent / logs-gone condition rather than blowing up kopf.
    """
    body = event.get("object") or {}
    metadata = body.get("metadata") or {}
    status = body.get("status") or {}
    succeeded = int(status.get("succeeded") or 0)
    failed = int(status.get("failed") or 0)
    if succeeded == 0 and failed == 0:
        return  # still running

    sentinel_name, sentinel_ns = _owning_sentinel(metadata)
    if not sentinel_name:
        return

    phase = "Succeeded" if succeeded > 0 else "Failed"
    job_name = metadata.get("name")
    ns = metadata.get("namespace")
    completion_ts = status.get("completionTime") or status.get("startTime")

    status_patch: dict[str, Any] = {
        "phase": phase,
        "lastRunResult": phase,
        "lastRunJobName": job_name,
    }
    if completion_ts:
        status_patch["lastRunAt"] = completion_ts

    if phase == "Succeeded":
        findings = _count_findings_from_pod_logs(ns=ns, job_name=job_name)
        if findings is not None:
            status_patch["findingsCount"] = findings

    _patch_sentinel_status(
        name=sentinel_name, namespace=sentinel_ns, status_patch=status_patch,
    )


def _owning_sentinel(job_metadata: dict) -> tuple[str | None, str | None]:
    """Find the (name, namespace) of the parent Sentinel for an owned Job."""
    ns = job_metadata.get("namespace")
    for owner in job_metadata.get("ownerReferences") or []:
        if owner.get("kind") == "Sentinel" and owner.get("apiVersion", "").startswith(
            GROUP + "/"
        ):
            return owner.get("name"), ns
    # CronJob-owned Jobs have the CronJob (not the Sentinel) as owner; fall
    # back to the label the reconciler stamps on every managed object.
    label = (job_metadata.get("labels") or {}).get(MANAGED_LABEL)
    if label:
        return label, ns
    return None, ns


def _count_findings_from_pod_logs(*, ns: str, job_name: str) -> int | None:
    """Return the count of lines beginning with ``FINDING `` in the pod logs
    for the completed Job's pod. Returns ``None`` if the pod is gone
    (retention) — a non-fatal condition.
    """
    core = k8s_client.CoreV1Api()
    try:
        pods = core.list_namespaced_pod(
            namespace=ns, label_selector=f"job-name={job_name}",
        )
    except ApiException as exc:
        log.info("finding-count: list pods for %s/%s failed: %s", ns, job_name, exc)
        return None
    if not pods.items:
        log.info("finding-count: no pod for job %s/%s (retention?)", ns, job_name)
        return None
    pod = pods.items[0]
    try:
        logs = core.read_namespaced_pod_log(
            name=pod.metadata.name, namespace=ns, container="sentinel-executor",
        )
    except ApiException as exc:
        log.info("finding-count: read pod logs %s/%s failed: %s",
                 ns, pod.metadata.name, exc)
        return None
    if not isinstance(logs, str):
        return None
    return sum(1 for line in logs.splitlines() if line.startswith("FINDING "))


def _patch_sentinel_status(*, name: str, namespace: str, status_patch: dict) -> None:
    api = k8s_client.CustomObjectsApi()
    try:
        api.patch_namespaced_custom_object_status(
            group=GROUP, version=VERSION, namespace=namespace, plural=PLURAL,
            name=name, body={"status": status_patch},
        )
    except ApiException as exc:
        if exc.status == 404:
            log.info(
                "parent Sentinel %s/%s not found — deleted mid-flight, skipping status patch",
                namespace, name,
            )
            return
        log.warning("patching Sentinel %s/%s status failed: %s", namespace, name, exc)
