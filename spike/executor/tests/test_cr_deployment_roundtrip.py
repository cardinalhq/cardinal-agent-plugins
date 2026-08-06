"""CR -> controller projection -> executor load_deployment, in one test.

This is the drift-catcher for the seam that was broken at HEAD: the controller
wrote the CR's capabilities as a top-level LIST under `capabilities:` while the
executor read a DICT under `capabilityBindings:` — both keys validated, so the
CR's provider choice was silently discarded at run time.

Nothing else in the repo exercises both sides of that seam. It lives on the
executor side because the executor owns the heavier dependency (jsonschema);
`projections` / `reconciler` are pure stdlib + PyYAML, so importing them from
here needs no controller dependencies.

If either side changes shape, this fails with a message that says which side.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROLLER_DIR = REPO_ROOT / "k8s" / "controller"
if str(CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_DIR))

# conftest.py already put spike/executor/ on sys.path.
from deployment import CapabilityNotBoundError, load_deployment  # noqa: E402

from projections import project_deployment  # noqa: E402
from reconciler import reconcile_sentinel  # noqa: E402


# The four capabilities mechanize-out/f89df52b/sentinel.yaml declares under
# spec.capabilities.required — the sentinel this seam exists to fire.
REQUIRED_CAPABILITIES = [
    "observability.list-services",
    "observability.error-overview",
    "observability.query-logs",
    "observability.query-metrics",
]


def _cr(capabilities: list | None = None) -> dict:
    """A realistic Sentinel CR, modelled on mechanize-out/f89df52b."""
    if capabilities is None:
        capabilities = [
            {
                "id": cap_id,
                "provider": "mcp",
                "endpointSecretRef": "cardinal-mcp-endpoint",
                "tokenSecretRef": "cardinal-mcp-token",
            }
            for cap_id in REQUIRED_CAPABILITIES
        ]
    return {
        "apiVersion": "sentinels.cardinalhq.io/v1alpha1",
        "kind": "Sentinel",
        "metadata": {
            "name": "service-health-assessment",
            "namespace": "sentinel-system",
            "uid": "c0ffee00-0000-4000-8000-000000000001",
        },
        "spec": {
            "source": {
                "git": {
                    "url": "https://github.com/cardinalhq/cardinal-agent-plugins",
                    "ref": "main",
                    "path": "mechanize-out/f89df52b",
                },
            },
            "schedule": "0 * * * *",
            "inputs": {"instance": "prod", "serviceQuery": "lakerunner"},
            "capabilities": capabilities,
            "sinks": [{"id": "stdout"}],
            # v0.1.3+ is the floor for `provider: mcp` (the provider module
            # first ships there); an older tag here would model a CR that
            # fails at its first tool node.
            "runtime": {"image": "ghcr.io/cardinalhq/sentinel-executor:v0.1.3"},
        },
    }


def _dir_deployment() -> dict:
    """The sentinel directory's own deployment.yaml (fixture-bound today).

    Modelled on mechanize-out/f89df52b-v2/deployment.yaml. The dir side is what
    supplies schemaVersion / kind / runtime — without it the projected file is
    schema-invalid, which is itself worth pinning.
    """
    return {
        "schemaVersion": "mechanize.dev/v1alpha1",
        "kind": "SentinelDeployment",
        "runtime": "k8s-controller",
        "execution": {"allowFixtures": True},
        "capabilityBindings": {
            cap_id: {"provider": "fixture", "side_effect_class": "read-only"}
            for cap_id in REQUIRED_CAPABILITIES
        },
        "inputBindings": {
            "instance": {"source": "dispatch"},
            "serviceQuery": {"source": "dispatch"},
        },
        "findingsRouting": [{"match": {"*": True}, "sink": "stdout"}],
    }


def _project(tmp_path: Path, cr: dict, dir_deployment: dict | None) -> Path:
    spec = cr["spec"]
    text = project_deployment(
        spec.get("sinks"), spec.get("capabilities"), dir_deployment
    )
    path = tmp_path / "deployment.yaml"
    path.write_text(text)
    return path


def _executor_env_names(cr: dict) -> set[str]:
    result = reconcile_sentinel(cr)
    assert result.job_or_cronjob is not None, result.error
    top = result.job_or_cronjob
    pod_spec = (
        top["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        if top["kind"] == "CronJob"
        else top["spec"]["template"]["spec"]
    )
    return {e["name"] for e in pod_spec["containers"][0]["env"]}


# --------------------------------------------------------------------------- #
# The round trip                                                              #
# --------------------------------------------------------------------------- #

def test_projected_deployment_loads_and_binds_every_cr_capability(tmp_path):
    cr = _cr()
    path = _project(tmp_path, cr, _dir_deployment())

    # 1. The projected file is something the executor can actually load.
    dep = load_deployment(path)

    # 2. Every capability the CR bound resolves — to the CR's provider, not the
    #    directory's `fixture` default.
    for cap_id in REQUIRED_CAPABILITIES:
        binding = dep.binding_for(cap_id)
        assert binding["provider"] == "mcp", (
            f"{cap_id}: expected the CR's provider to win over the sentinel "
            f"directory's fixture binding, got {binding!r}"
        )
        slug = cap_id.replace(".", "_").replace("-", "_").upper()
        assert binding["endpoint_env"] == f"CARDINAL_CAP_{slug}_ENDPOINT", binding
        assert binding["token_env"] == f"CARDINAL_CAP_{slug}_TOKEN", binding

    # 3. The dead `capabilities:` key is gone — one key, one meaning.
    assert "capabilities" not in dep.raw, (
        "the projection re-introduced a top-level `capabilities:` key; the "
        "executor reads capabilityBindings only, so a second key means the "
        "CR's intent can be silently ignored again"
    )


def test_every_projected_env_var_is_actually_injected_into_the_pod(tmp_path):
    """The assertion that would have caught the original drift.

    The binding names env vars; the pod spec injects env vars. They come from
    two different functions in two different modules — this ties them together.
    """
    cr = _cr()
    dep = load_deployment(_project(tmp_path, cr, _dir_deployment()))
    env_names = _executor_env_names(cr)

    for cap_id in REQUIRED_CAPABILITIES:
        binding = dep.binding_for(cap_id)
        for key in ("endpoint_env", "token_env"):
            var = binding.get(key)
            assert var in env_names, (
                f"{cap_id}: deployment.yaml binding names {key}={var!r} but the "
                f"pod spec injects {sorted(env_names)} — the binding writer "
                f"(projections.capability_bindings) and the env writer "
                f"(reconciler._capability_env) have drifted apart"
            )


def test_capability_without_token_secret_ref_gets_neither_binding_nor_env(tmp_path):
    """The two writers must apply the identical predicate, negatively too."""
    cr = _cr([
        {
            "id": "observability.list-services",
            "provider": "mcp",
            "endpointSecretRef": "cardinal-mcp-endpoint",
        },
    ])
    dep = load_deployment(_project(tmp_path, cr, _dir_deployment()))
    binding = dep.binding_for("observability.list-services")
    assert "endpoint_env" in binding
    assert "token_env" not in binding, (
        "a binding must not name a token variable the pod never injects"
    )

    env_names = _executor_env_names(cr)
    assert "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT" in env_names
    assert "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN" not in env_names


def test_capabilities_the_cr_omits_keep_their_fixture_binding(tmp_path):
    """Partial rebinding: the CR moves one capability to mcp, the rest stay."""
    cr = _cr([
        {
            "id": "observability.query-logs",
            "provider": "mcp",
            "endpointSecretRef": "cardinal-mcp-endpoint",
            "tokenSecretRef": "cardinal-mcp-token",
        },
    ])
    dep = load_deployment(_project(tmp_path, cr, _dir_deployment()))
    assert dep.binding_for("observability.query-logs")["provider"] == "mcp"
    for cap_id in REQUIRED_CAPABILITIES:
        if cap_id == "observability.query-logs":
            continue
        assert dep.binding_for(cap_id)["provider"] == "fixture"


def test_capability_the_sentinel_needs_but_nobody_bound_fails_loudly(tmp_path):
    """Blocker 3, end to end: no binding => the node's lookup raises."""
    cr = _cr([
        {
            "id": "observability.list-services",
            "provider": "mcp",
            "endpointSecretRef": "cardinal-mcp-endpoint",
            "tokenSecretRef": "cardinal-mcp-token",
        },
    ])
    dir_deployment = _dir_deployment()
    dir_deployment["capabilityBindings"] = {}
    dep = load_deployment(_project(tmp_path, cr, dir_deployment))

    with pytest.raises(CapabilityNotBoundError) as excinfo:
        # Exactly what runtime_serve._run_node does for a tool node whose
        # toolRef is `observability.query-logs`.
        dep.capability_bindings.get("observability.query-logs")
    assert "observability.query-logs" in str(excinfo.value)


def test_projection_without_dir_side_is_schema_invalid_and_says_so(tmp_path):
    """A sentinel dir with no deployment.yaml cannot produce a loadable file.

    schemaVersion / kind / runtime only ever come from the dir side, so the
    executor dies at pod start. Pinned so the failure stays a clear schema
    error rather than becoming a silent default.
    """
    cr = _cr()
    path = _project(tmp_path, cr, None)
    with pytest.raises(Exception) as excinfo:
        load_deployment(path)
    assert "runtime" in str(excinfo.value)


def test_projected_yaml_is_deterministic(tmp_path):
    """Byte-stable output — the Secret must not churn on every reconcile."""
    cr = _cr()
    first = project_deployment(
        cr["spec"]["sinks"], cr["spec"]["capabilities"], _dir_deployment()
    )
    second = project_deployment(
        cr["spec"]["sinks"], cr["spec"]["capabilities"], _dir_deployment()
    )
    assert first == second
    assert yaml.safe_load(first)["capabilityBindings"], "sanity: bindings present"
