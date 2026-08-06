"""Unit tests for the pure reconciler.

The reconciler is a pure function: same input dict → same output manifests.
These tests exercise:

* Hash stability under identical inputs and under unrelated metadata edits;
  hash sensitivity to any behavioural spec field.
* Job vs CronJob branching on ``spec.schedule`` presence.
* Projected Secret content: base64 round-trips to the projected inputs.json
  and deployment.yaml.
* Owner references appearing on every returned manifest.
* Capability env-var naming for the hyphen / dot slugification.
"""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest
import yaml

from reconciler import DEFAULT_EXECUTOR_IMAGE, reconcile_sentinel


def _base_cr() -> dict:
    return {
        "apiVersion": "sentinels.cardinalhq.io/v1alpha1",
        "kind": "Sentinel",
        "metadata": {
            "name": "service-health",
            "namespace": "lakerunner",
            "uid": "abc-123-uid",
            "labels": {"team": "platform"},
        },
        "spec": {
            "source": {
                "git": {
                    "url": "https://github.com/cardinalhq/lakerunner",
                    "ref": "main",
                    "path": "sentinels/service-health",
                },
            },
            "inputs": {
                "instance": "prod-us-east-2",
                "serviceQuery": "lakerunner",
            },
            "runtime": {
                "image": "ghcr.io/cardinalhq/sentinel-executor:v0.1.0",
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
                "activeDeadlineSeconds": 900,
            },
            "capabilities": [
                {
                    "id": "observability.list-services",
                    "provider": "mcp",
                    "endpointSecretRef": "cardinal-mcp-endpoint",
                    "tokenSecretRef": "cardinal-mcp-token",
                },
                {
                    "id": "observability.query-metrics",
                    "provider": "mcp",
                    "endpointSecretRef": "cardinal-mcp-endpoint",
                    "tokenSecretRef": "cardinal-mcp-token",
                },
            ],
            "sinks": [
                {"id": "stdout"},
                {"id": "slack.channel", "channelSecretRef": "sentinel-findings-slack"},
            ],
        },
    }


# --------------------------------------------------------------------------- #
# Hash stability                                                              #
# --------------------------------------------------------------------------- #

def test_hash_stable_across_identical_crs():
    cr1 = _base_cr()
    cr2 = _base_cr()
    r1 = reconcile_sentinel(cr1)
    r2 = reconcile_sentinel(cr2)
    assert r1.job_or_cronjob is not None
    assert r1.job_or_cronjob["metadata"]["name"] == r2.job_or_cronjob["metadata"]["name"]


def test_hash_changes_when_inputs_change():
    cr1 = _base_cr()
    cr2 = _base_cr()
    cr2["spec"]["inputs"]["instance"] = "prod-us-west-1"
    r1 = reconcile_sentinel(cr1)
    r2 = reconcile_sentinel(cr2)
    assert r1.job_or_cronjob["metadata"]["name"] != r2.job_or_cronjob["metadata"]["name"]


def test_hash_stable_when_only_metadata_labels_change():
    cr1 = _base_cr()
    cr2 = _base_cr()
    cr2["metadata"]["labels"] = {"team": "sre", "env": "prod", "extra": "value"}
    cr2["metadata"]["annotations"] = {"note": "unrelated churn"}
    r1 = reconcile_sentinel(cr1)
    r2 = reconcile_sentinel(cr2)
    assert r1.job_or_cronjob["metadata"]["name"] == r2.job_or_cronjob["metadata"]["name"]


def test_hash_changes_on_source_ref():
    cr1 = _base_cr()
    cr2 = _base_cr()
    cr2["spec"]["source"]["git"]["ref"] = "v1.2.3"
    r1 = reconcile_sentinel(cr1)
    r2 = reconcile_sentinel(cr2)
    assert r1.job_or_cronjob["metadata"]["name"] != r2.job_or_cronjob["metadata"]["name"]


def test_job_name_shape():
    cr = _base_cr()
    r = reconcile_sentinel(cr)
    name = r.job_or_cronjob["metadata"]["name"]
    assert name.startswith("sentinel-service-health-")
    suffix = name.rsplit("-", 1)[-1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


# --------------------------------------------------------------------------- #
# Job vs CronJob branching                                                    #
# --------------------------------------------------------------------------- #

def test_no_schedule_yields_job():
    cr = _base_cr()
    r = reconcile_sentinel(cr)
    assert r.job_or_cronjob["kind"] == "Job"
    assert r.job_or_cronjob["apiVersion"] == "batch/v1"
    assert "schedule" not in r.job_or_cronjob["spec"]


def test_schedule_yields_cronjob_with_same_template():
    cr = _base_cr()
    cr["spec"]["schedule"] = "*/5 * * * *"
    r = reconcile_sentinel(cr)
    cj = r.job_or_cronjob
    assert cj["kind"] == "CronJob"
    assert cj["spec"]["schedule"] == "*/5 * * * *"
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    assert cj["spec"]["successfulJobsHistoryLimit"] == 3
    assert cj["spec"]["failedJobsHistoryLimit"] == 5

    # The jobTemplate's pod spec matches the one-shot Job's pod spec.
    cr_noschedule = _base_cr()
    r2 = reconcile_sentinel(cr_noschedule)
    assert (
        cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        == r2.job_or_cronjob["spec"]["template"]["spec"]
    )


def test_schedule_does_not_change_hash():
    cr1 = _base_cr()
    cr2 = _base_cr()
    cr2["spec"]["schedule"] = "*/5 * * * *"
    r1 = reconcile_sentinel(cr1)
    r2 = reconcile_sentinel(cr2)
    # Wrapping in CronJob doesn't change the hash; the CronJob object gets
    # the same base name so redeploys stay stable.
    assert r1.job_or_cronjob["metadata"]["name"] == r2.job_or_cronjob["metadata"]["name"]


# --------------------------------------------------------------------------- #
# Projected Secret                                                            #
# --------------------------------------------------------------------------- #

def test_projected_secret_shape():
    cr = _base_cr()
    r = reconcile_sentinel(cr)
    sec = r.projected_secret
    assert sec is not None
    assert sec["kind"] == "Secret"
    assert sec["type"] == "Opaque"
    assert sec["metadata"]["namespace"] == "lakerunner"

    inputs = json.loads(base64.b64decode(sec["data"]["inputs.json"]).decode("utf-8"))
    assert inputs == {
        "instance": "prod-us-east-2",
        "serviceQuery": "lakerunner",
    }

    deployment = yaml.safe_load(base64.b64decode(sec["data"]["deployment.yaml"]).decode("utf-8"))
    assert deployment["sinks"] == cr["spec"]["sinks"]
    # The CR's capability LIST is projected as the executor's binding MAP,
    # keyed by capability id, naming the env vars the pod spec injects.
    # There is no `capabilities:` key — the executor never read one.
    assert "capabilities" not in deployment
    assert deployment["capabilityBindings"] == {
        "observability.list-services": {
            "provider": "mcp",
            "endpoint_env": "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT",
            "token_env": "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN",
        },
        "observability.query-metrics": {
            "provider": "mcp",
            "endpoint_env": "CARDINAL_CAP_OBSERVABILITY_QUERY_METRICS_ENDPOINT",
            "token_env": "CARDINAL_CAP_OBSERVABILITY_QUERY_METRICS_TOKEN",
        },
    }


def test_projected_secret_name_matches_job_name():
    cr = _base_cr()
    r = reconcile_sentinel(cr)
    assert r.projected_secret["metadata"]["name"] == r.job_or_cronjob["metadata"]["name"]


def test_secret_referenced_by_pod_volume():
    cr = _base_cr()
    r = reconcile_sentinel(cr)
    pod_spec = r.job_or_cronjob["spec"]["template"]["spec"]
    config_volume = next(v for v in pod_spec["volumes"] if v["name"] == "sentinel-config")
    assert config_volume["secret"]["secretName"] == r.projected_secret["metadata"]["name"]


# --------------------------------------------------------------------------- #
# Owner references                                                            #
# --------------------------------------------------------------------------- #

def test_owner_references_present_on_all_manifests():
    cr = _base_cr()
    cr["spec"]["schedule"] = "*/5 * * * *"
    r = reconcile_sentinel(cr)
    for manifest in (r.job_or_cronjob, r.projected_secret):
        refs = manifest["metadata"]["ownerReferences"]
        assert len(refs) == 1
        ref = refs[0]
        assert ref["apiVersion"] == "sentinels.cardinalhq.io/v1alpha1"
        assert ref["kind"] == "Sentinel"
        assert ref["name"] == "service-health"
        assert ref["uid"] == "abc-123-uid"
        assert ref["controller"] is True
        assert ref["blockOwnerDeletion"] is True


def test_owner_reference_on_oneshot_job():
    cr = _base_cr()
    r = reconcile_sentinel(cr)
    ref = r.job_or_cronjob["metadata"]["ownerReferences"][0]
    assert ref["uid"] == "abc-123-uid"


# --------------------------------------------------------------------------- #
# Pod spec — init + main containers                                           #
# --------------------------------------------------------------------------- #

def test_pod_spec_has_init_and_main_containers():
    cr = _base_cr()
    r = reconcile_sentinel(cr)
    pod_spec = r.job_or_cronjob["spec"]["template"]["spec"]

    assert len(pod_spec["initContainers"]) == 1
    init = pod_spec["initContainers"][0]
    assert init["image"] == "alpine/git:latest"
    env_by_name = {e["name"]: e["value"] for e in init["env"]}
    assert env_by_name["GIT_URL"] == "https://github.com/cardinalhq/lakerunner"
    assert env_by_name["GIT_REF"] == "main"

    assert len(pod_spec["containers"]) == 1
    main = pod_spec["containers"][0]
    assert main["image"] == "ghcr.io/cardinalhq/sentinel-executor:v0.1.0"
    assert main["workingDir"] == "/sentinel/sentinels/service-health"
    assert main["command"] == ["sentinel-executor"]
    assert main["args"] == [
        "run", ".",
        "--inputs", "/config/inputs.json",
        "--deployment", "/config/deployment.yaml",
    ]


def test_runtime_image_default_used_when_absent():
    # Literal on purpose: this is the drift-catcher for the compiled-in
    # default. It must name a multi-arch published tag (>= v0.1.1) or every
    # CR without an explicit spec.runtime.image ImagePullBackOffs on the
    # prod arm64 nodes, and it must be >= v0.1.3 or `provider: mcp` raises
    # UnknownProviderError at the first tool node. Bump in lockstep with
    # spike/executor/VERSION.
    cr = _base_cr()
    del cr["spec"]["runtime"]["image"]
    r = reconcile_sentinel(cr)
    main = r.job_or_cronjob["spec"]["template"]["spec"]["containers"][0]
    assert main["image"] == "ghcr.io/cardinalhq/sentinel-executor:v0.1.3"


def test_default_executor_image_tag_matches_executor_version_file():
    """The compiled-in default must name a tag the release workflow publishes.

    `.github/workflows/executor-image.yml` tags `v<spike/executor/VERSION>`
    and is an explicit no-op when the tag already exists in GHCR. So a
    default pointing at anything other than the current VERSION is one of
    two failures: a tag that will never be built (ImagePullBackOff), or a
    stale published tag whose contents predate the source tree — which is
    exactly how `provider: mcp` came to default onto the mcp-less v0.1.2.
    """
    version_file = (
        Path(__file__).resolve().parents[3] / "spike" / "executor" / "VERSION"
    )
    version = version_file.read_text().strip()
    assert DEFAULT_EXECUTOR_IMAGE.endswith(f":v{version}"), (
        f"reconciler.DEFAULT_EXECUTOR_IMAGE is {DEFAULT_EXECUTOR_IMAGE!r} but "
        f"spike/executor/VERSION is {version!r} — the default names a tag "
        f"executor-image.yml does not publish from this tree"
    )


# --------------------------------------------------------------------------- #
# Capability env vars                                                         #
# --------------------------------------------------------------------------- #

def test_capability_env_var_names_for_mixed_ids():
    # `provider` is required by the CRD on every capability entry, and the
    # projection now refuses to bind an entry without one — so the fixtures
    # carry it. The env-var naming assertions below are unchanged.
    cr = _base_cr()
    cr["spec"]["capabilities"] = [
        {
            "id": "observability.list-services",
            "provider": "mcp",
            "endpointSecretRef": "ep",
            "tokenSecretRef": "tok",
        },
        {
            "id": "code.review",
            "provider": "mcp",
            "endpointSecretRef": "code-ep",
            "tokenSecretRef": "code-tok",
        },
        {
            "id": "some-id.with.multiple-hyphens",
            "provider": "mcp",
            "endpointSecretRef": "misc-ep",
            "tokenSecretRef": "misc-tok",
        },
    ]
    r = reconcile_sentinel(cr)
    main = r.job_or_cronjob["spec"]["template"]["spec"]["containers"][0]
    env_names = {e["name"] for e in main["env"]}
    assert "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT" in env_names
    assert "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN" in env_names
    assert "CARDINAL_CAP_CODE_REVIEW_ENDPOINT" in env_names
    assert "CARDINAL_CAP_CODE_REVIEW_TOKEN" in env_names
    assert "CARDINAL_CAP_SOME_ID_WITH_MULTIPLE_HYPHENS_ENDPOINT" in env_names
    assert "CARDINAL_CAP_SOME_ID_WITH_MULTIPLE_HYPHENS_TOKEN" in env_names


def test_capability_env_var_secretkeyref_wiring():
    cr = _base_cr()
    r = reconcile_sentinel(cr)
    main = r.job_or_cronjob["spec"]["template"]["spec"]["containers"][0]
    ep = next(e for e in main["env"]
              if e["name"] == "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT")
    tok = next(e for e in main["env"]
               if e["name"] == "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN")
    assert ep["valueFrom"]["secretKeyRef"]["name"] == "cardinal-mcp-endpoint"
    assert ep["valueFrom"]["secretKeyRef"]["key"] == "endpoint"
    assert tok["valueFrom"]["secretKeyRef"]["name"] == "cardinal-mcp-token"
    assert tok["valueFrom"]["secretKeyRef"]["key"] == "token"


# --------------------------------------------------------------------------- #
# Error paths                                                                 #
# --------------------------------------------------------------------------- #

def test_missing_metadata_uid_blocks_reconcile():
    cr = _base_cr()
    del cr["metadata"]["uid"]
    r = reconcile_sentinel(cr)
    assert r.phase == "Blocked"
    assert r.job_or_cronjob is None
    assert r.projected_secret is None
    assert "metadata.uid" in r.error


def test_missing_git_url_blocks_reconcile():
    cr = _base_cr()
    del cr["spec"]["source"]["git"]["url"]
    r = reconcile_sentinel(cr)
    assert r.phase == "Blocked"
    assert "spec.source.git.url" in r.error


def test_non_dict_input_yields_blocked():
    r = reconcile_sentinel(["not", "a", "dict"])  # type: ignore[arg-type]
    assert r.phase == "Blocked"
    assert r.error is not None


# --------------------------------------------------------------------------- #
# Malformed spec.capabilities the CRD schema cannot reject                     #
#                                                                              #
# ``required: [id, provider]`` only checks presence — no minLength on          #
# provider, no uniqueness on the array, and no way to express the env-var slug #
# collision. All three CRs below are accepted by the API server, so the        #
# reconciler must park them as Blocked rather than let a ValueError escape and #
# spin the Sentinel in a retry loop.                                           #
# --------------------------------------------------------------------------- #

def _capabilities_blocked(caps: list) -> object:
    cr = _base_cr()
    cr["spec"]["capabilities"] = caps
    return reconcile_sentinel(cr)


def test_duplicate_capability_id_blocks_instead_of_raising():
    """The natural operator mistake: endpoint on one entry, token on another."""
    r = _capabilities_blocked([
        {"id": "observability.query-logs", "provider": "mcp",
         "endpointSecretRef": "cardinal-mcp-endpoint"},
        {"id": "observability.query-logs", "provider": "mcp",
         "tokenSecretRef": "cardinal-mcp-token"},
    ])
    assert r.phase == "Blocked"
    assert r.job_or_cronjob is None
    assert r.projected_secret is None
    assert "observability.query-logs" in r.error
    assert "declared more than once" in r.error
    # The message must make the fix obvious, not just name the symptom.
    assert "[0]" in r.error and "[1]" in r.error
    assert "endpointSecretRef" in r.error and "tokenSecretRef" in r.error
    cond = _only_condition(r, "CapabilitiesBound")
    assert cond["status"] == "False"
    assert cond["reason"] == "InvalidCapabilities"
    assert "observability.query-logs" in cond["message"]


def test_blank_provider_blocks_instead_of_raising():
    r = _capabilities_blocked([
        {"id": "observability.query-logs", "provider": "   "},
    ])
    assert r.phase == "Blocked"
    assert r.job_or_cronjob is None
    assert "observability.query-logs" in r.error
    assert "provider" in r.error
    cond = _only_condition(r, "CapabilitiesBound")
    assert cond["status"] == "False"
    assert "observability.query-logs" in cond["message"]


def test_env_slug_collision_blocks_instead_of_raising():
    """``obs.query-logs`` and ``obs-query.logs`` share CARDINAL_CAP_OBS_QUERY_LOGS_*."""
    r = _capabilities_blocked([
        {"id": "obs.query-logs", "provider": "mcp", "tokenSecretRef": "t1"},
        {"id": "obs-query.logs", "provider": "mcp", "tokenSecretRef": "t2"},
    ])
    assert r.phase == "Blocked"
    assert r.job_or_cronjob is None
    assert "obs.query-logs" in r.error
    assert "obs-query.logs" in r.error
    assert "OBS_QUERY_LOGS" in r.error
    cond = _only_condition(r, "CapabilitiesBound")
    assert cond["status"] == "False"


def test_non_mapping_capability_entry_blocks_instead_of_raising():
    r = _capabilities_blocked(["observability.query-logs"])
    assert r.phase == "Blocked"
    assert "capabilities[0]" in r.error
    assert _only_condition(r, "CapabilitiesBound")["status"] == "False"


def test_blank_capability_id_blocks_instead_of_raising():
    r = _capabilities_blocked([{"id": "   ", "provider": "mcp"}])
    assert r.phase == "Blocked"
    assert "id" in r.error
    assert _only_condition(r, "CapabilitiesBound")["status"] == "False"


def test_unprojectable_inputs_block_instead_of_raising():
    """spec.inputs is x-kubernetes-preserve-unknown-fields; a list gets through."""
    cr = _base_cr()
    cr["spec"]["inputs"] = ["not", "a", "mapping"]
    r = reconcile_sentinel(cr)
    assert r.phase == "Blocked"
    assert r.job_or_cronjob is None
    assert _only_condition(r, "SpecInvalid")["status"] == "False"


def test_valid_capabilities_still_reconcile():
    """The guard must not block well-formed capability lists."""
    r = _capabilities_blocked([
        {"id": "obs.query-logs", "provider": "mcp",
         "endpointSecretRef": "ep", "tokenSecretRef": "tok"},
        {"id": "obs.list-services", "provider": "fixture"},
    ])
    assert r.phase == "Reconciling"
    assert r.job_or_cronjob is not None


def _only_condition(result, ctype: str) -> dict:
    matches = [c for c in result.conditions if c.get("type") == ctype]
    assert matches, f"no {ctype} condition in {result.conditions}"
    return matches[0]


# --------------------------------------------------------------------------- #
# Determinism sanity — the reconciler is a pure function                       #
# --------------------------------------------------------------------------- #

def test_reconcile_does_not_mutate_input():
    cr = _base_cr()
    snapshot = copy.deepcopy(cr)
    reconcile_sentinel(cr)
    assert cr == snapshot
