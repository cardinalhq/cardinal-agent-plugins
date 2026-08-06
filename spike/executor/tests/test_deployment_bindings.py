"""Capability-binding contract on the executor side.

Two things are pinned here:

1. The binding SHAPE the controller writes (`provider` + `endpoint_env` /
   `token_env`) validates, and the pre-existing `credential_ref` shape keeps
   validating alongside it.
2. Looking up an unbound capability FAILS LOUDLY. Before this, a missing
   binding made `capability_bindings.get(id)` return None and
   `runtime_serve._run_node` fell through to the legacy spike tool-cache
   path — the CR's `provider: mcp` was silently discarded and the run went
   looking for hand-written JSON files instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# conftest.py already put spike/executor/ on sys.path.
import deployment as deployment_mod  # noqa: E402
from deployment import (  # noqa: E402
    CapabilityBindings,
    CapabilityNotBoundError,
    DeploymentValidationError,
    load_deployment,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


def _write(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "deployment.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    return p


def _base_doc(bindings: dict) -> dict:
    return {
        "schemaVersion": "mechanize.dev/v1alpha1",
        "kind": "SentinelDeployment",
        "runtime": "k8s-controller",
        "capabilityBindings": bindings,
        "findingsRouting": [{"match": {"*": True}, "sink": "stdout"}],
    }


# --------------------------------------------------------------------------- #
# Shape                                                                       #
# --------------------------------------------------------------------------- #

def test_env_var_binding_shape_validates(tmp_path):
    path = _write(tmp_path, _base_doc({
        "observability.list-services": {
            "provider": "mcp",
            "endpoint_env": "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT",
            "token_env": "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN",
        },
    }))
    dep = load_deployment(path)
    binding = dep.binding_for("observability.list-services")
    assert binding["provider"] == "mcp"
    assert binding["endpoint_env"] == "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT"
    assert binding["token_env"] == "CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN"


def test_credential_ref_binding_still_validates(tmp_path):
    """The local/dev credential path must survive the additive schema change."""
    path = _write(tmp_path, _base_doc({
        "observability.query-metrics": {
            "provider": "lakerunner",
            "credential_ref": "env://LAKERUNNER_TOKEN",
            "side_effect_class": "read-only",
        },
    }))
    dep = load_deployment(path)
    assert dep.binding_for("observability.query-metrics")["credential_ref"] == (
        "env://LAKERUNNER_TOKEN"
    )


def test_fixture_deployment_from_mechanize_out_still_loads():
    """The checked-in fixture-provider deployment keeps working unchanged."""
    path = REPO_ROOT / "mechanize-out" / "f89df52b-v2" / "deployment.yaml"
    if not path.is_file():  # pragma: no cover - repo layout guard
        pytest.skip(f"{path} not present")
    dep = load_deployment(path)
    assert dep.binding_for("observability.list-services")["provider"] == "fixture"


def test_binding_with_bad_env_var_name_is_rejected(tmp_path):
    path = _write(tmp_path, _base_doc({
        "observability.query-logs": {
            "provider": "mcp",
            "endpoint_env": "not-an-env-var",
        },
    }))
    with pytest.raises(DeploymentValidationError, match="endpoint_env|pattern"):
        load_deployment(path)


def test_binding_with_blank_provider_is_rejected(tmp_path):
    path = _write(tmp_path, _base_doc({
        "observability.query-logs": {"provider": ""},
    }))
    with pytest.raises(DeploymentValidationError):
        load_deployment(path)


# --------------------------------------------------------------------------- #
# Fail-loud lookup                                                            #
# --------------------------------------------------------------------------- #

def test_unbound_capability_lookup_raises_naming_the_capability(tmp_path):
    path = _write(tmp_path, _base_doc({
        "observability.list-services": {"provider": "fixture"},
    }))
    dep = load_deployment(path)

    with pytest.raises(CapabilityNotBoundError) as excinfo:
        # This is the exact call runtime_serve._run_node makes for a tool node.
        dep.capability_bindings.get("observability.query-logs")

    msg = str(excinfo.value)
    assert "observability.query-logs" in msg, msg
    assert "observability.list-services" in msg, "message should list what IS bound"
    assert str(path) in msg, "message should say which deployment.yaml"


def test_unbound_capability_getitem_raises(tmp_path):
    path = _write(tmp_path, _base_doc({"cap.bound": {"provider": "fixture"}}))
    dep = load_deployment(path)
    with pytest.raises(CapabilityNotBoundError, match="cap.missing"):
        dep.binding_for("cap.missing")


def test_no_bindings_at_all_still_raises_rather_than_falling_through(tmp_path):
    """A deployment with zero bindings must not silently enable the legacy path."""
    doc = _base_doc({})
    doc.pop("capabilityBindings")
    dep = load_deployment(_write(tmp_path, doc))
    assert isinstance(dep.capability_bindings, CapabilityBindings)
    with pytest.raises(CapabilityNotBoundError):
        dep.capability_bindings.get("observability.list-services")


def test_explicit_default_is_still_honoured():
    """An escape hatch is fine as long as the caller has to type it out."""
    bindings = CapabilityBindings({"cap.bound": {"provider": "fixture"}})
    assert bindings.get("cap.missing", None) is None
    assert bindings.get("cap.missing", {"provider": "x"}) == {"provider": "x"}
    assert bindings.get("cap.bound") == {"provider": "fixture"}


def test_runtime_serve_reads_bindings_through_the_strict_map():
    """Guard the assumption this whole file rests on.

    If `runtime_serve` ever stops going through `capability_bindings.get(...)`
    — e.g. someone reintroduces a `or {}` / `.get(ref, None)` — the strict map
    stops being a guarantee and this test says so.
    """
    src = (Path(deployment_mod.__file__).parent / "runtime_serve.py").read_text()
    assert "deployment.capability_bindings.get(tool_ref)" in src, (
        "runtime_serve no longer looks capability bindings up through the "
        "strict CapabilityBindings.get(); re-check that an unbound capability "
        "still fails loudly instead of falling back to the tool-cache path"
    )
