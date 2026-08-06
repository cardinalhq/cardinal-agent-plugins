"""Unit tests for the projection helpers.

Covers:

* CR fields override directory fields (shallow merge).
* Missing (None) directory config is treated as empty.
* Non-dict CR / dir arguments raise ``TypeError``.
* Output is valid JSON / YAML respectively.
"""
from __future__ import annotations

import json

import pytest
import yaml

from projections import (
    cap_endpoint_env,
    cap_env_slug,
    cap_token_env,
    capability_bindings,
    project_deployment,
    project_inputs,
)


# --------------------------------------------------------------------------- #
# project_inputs                                                              #
# --------------------------------------------------------------------------- #

def test_project_inputs_cr_overrides_dir():
    cr = {"instance": "prod-us-east-2", "extra": "cr-value"}
    dir_inputs = {"instance": "default", "serviceQuery": "lakerunner"}
    out = json.loads(project_inputs(cr, dir_inputs))
    assert out == {
        "instance": "prod-us-east-2",
        "extra": "cr-value",
        "serviceQuery": "lakerunner",
    }


def test_project_inputs_missing_dir_is_empty():
    cr = {"a": 1}
    assert json.loads(project_inputs(cr, None)) == {"a": 1}


def test_project_inputs_both_empty():
    assert json.loads(project_inputs(None, None)) == {}


def test_project_inputs_output_is_sorted_and_indented():
    out = project_inputs({"b": 2, "a": 1}, {})
    # sort_keys=True => "a" comes before "b"; indent=2 => pretty-printed
    assert out.index('"a"') < out.index('"b"')
    assert "\n" in out


def test_project_inputs_rejects_non_dict_cr():
    with pytest.raises(TypeError):
        project_inputs(["not", "a", "dict"], {})  # type: ignore[arg-type]


def test_project_inputs_rejects_non_dict_dir():
    with pytest.raises(TypeError):
        project_inputs({}, "not a dict")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# project_deployment                                                          #
# --------------------------------------------------------------------------- #

def test_project_deployment_cr_sinks_replace_dir_sinks():
    cr_sinks = [{"id": "stdout"}, {"id": "slack.channel"}]
    dir_dep = {"sinks": [{"id": "log.file"}], "other": "kept"}
    out = yaml.safe_load(project_deployment(cr_sinks, None, dir_dep))
    assert out["sinks"] == cr_sinks
    assert out["other"] == "kept"


def test_project_deployment_cr_capabilities_become_capability_bindings():
    """The CR's capability LIST is translated into the executor's binding MAP.

    The executor only ever reads ``capabilityBindings``; emitting the CR list
    under a second ``capabilities:`` key is what let the CR's provider choice
    be silently ignored, so that key must not appear at all.
    """
    cr_caps = [{"id": "observability.list-services", "provider": "mcp"}]
    dir_dep = {"capabilityBindings": {"observability.list-services": {"provider": "fixture"}}}
    out = yaml.safe_load(project_deployment(None, cr_caps, dir_dep))
    assert out["capabilityBindings"] == {
        "observability.list-services": {"provider": "mcp"},
    }
    assert "capabilities" not in out


def test_project_deployment_cr_binding_replaces_dir_binding_wholesale():
    """A rebind must not leave the previous provider's credential behind."""
    cr_caps = [{
        "id": "observability.query-logs",
        "provider": "mcp",
        "endpointSecretRef": "cardinal-mcp-endpoint",
        "tokenSecretRef": "cardinal-mcp-token",
    }]
    dir_dep = {
        "capabilityBindings": {
            "observability.query-logs": {
                "provider": "fixture",
                "credential_ref": "env://LEFTOVER",
            },
        },
    }
    out = yaml.safe_load(project_deployment(None, cr_caps, dir_dep))
    assert out["capabilityBindings"]["observability.query-logs"] == {
        "provider": "mcp",
        "endpoint_env": "CARDINAL_CAP_OBSERVABILITY_QUERY_LOGS_ENDPOINT",
        "token_env": "CARDINAL_CAP_OBSERVABILITY_QUERY_LOGS_TOKEN",
    }


def test_project_deployment_keeps_dir_bindings_the_cr_does_not_mention():
    """The fixture path stays alive for capabilities the CR leaves alone."""
    cr_caps = [{"id": "observability.query-logs", "provider": "mcp"}]
    dir_dep = {
        "capabilityBindings": {
            "observability.list-services": {
                "provider": "fixture",
                "side_effect_class": "read-only",
            },
        },
    }
    out = yaml.safe_load(project_deployment(None, cr_caps, dir_dep))
    assert out["capabilityBindings"]["observability.list-services"] == {
        "provider": "fixture",
        "side_effect_class": "read-only",
    }
    assert out["capabilityBindings"]["observability.query-logs"] == {"provider": "mcp"}


def test_project_deployment_env_names_only_when_secret_refs_present():
    cr_caps = [
        {"id": "cap.endpoint-only", "provider": "mcp", "endpointSecretRef": "ep"},
        {"id": "cap.token-only", "provider": "mcp", "tokenSecretRef": "tok"},
        {"id": "cap.neither", "provider": "fixture"},
    ]
    out = yaml.safe_load(project_deployment(None, cr_caps, None))
    bindings = out["capabilityBindings"]
    assert bindings["cap.endpoint-only"] == {
        "provider": "mcp",
        "endpoint_env": "CARDINAL_CAP_CAP_ENDPOINT_ONLY_ENDPOINT",
    }
    assert bindings["cap.token-only"] == {
        "provider": "mcp",
        "token_env": "CARDINAL_CAP_CAP_TOKEN_ONLY_TOKEN",
    }
    assert bindings["cap.neither"] == {"provider": "fixture"}


def test_project_deployment_missing_dir_is_empty():
    cr_sinks = [{"id": "stdout"}]
    cr_caps = [{"id": "cap.x", "provider": "mcp"}]
    out = yaml.safe_load(project_deployment(cr_sinks, cr_caps, None))
    assert out == {
        "sinks": cr_sinks,
        "capabilityBindings": {"cap.x": {"provider": "mcp"}},
    }


# --------------------------------------------------------------------------- #
# capability_bindings — the CR -> executor translation, failing loudly        #
# --------------------------------------------------------------------------- #

def test_capability_bindings_none_and_empty():
    assert capability_bindings(None) == {}
    assert capability_bindings([]) == {}


def test_capability_bindings_rejects_entry_without_provider():
    with pytest.raises(ValueError, match="observability.list-services"):
        capability_bindings([{"id": "observability.list-services"}])


def test_capability_bindings_rejects_entry_without_id():
    with pytest.raises(ValueError, match="id"):
        capability_bindings([{"provider": "mcp"}])


def test_capability_bindings_rejects_non_mapping_entry():
    with pytest.raises(ValueError, match=r"capabilities\[0\]"):
        capability_bindings(["observability.list-services"])


def test_capability_bindings_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="declared more than once"):
        capability_bindings([
            {"id": "cap.x", "provider": "mcp"},
            {"id": "cap.x", "provider": "fixture"},
        ])


def test_capability_bindings_rejects_env_slug_collision():
    """``a.b`` and ``a-b`` would share one CARDINAL_CAP_A_B_* pair."""
    with pytest.raises(ValueError, match="A_B"):
        capability_bindings([
            {"id": "a.b", "provider": "mcp", "tokenSecretRef": "t1"},
            {"id": "a-b", "provider": "mcp", "tokenSecretRef": "t2"},
        ])


def test_project_deployment_propagates_capability_errors():
    with pytest.raises(ValueError):
        project_deployment(None, [{"id": "cap.x"}], None)


# --------------------------------------------------------------------------- #
# env var naming — one source of truth for pod env and deployment.yaml        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "cap_id,slug",
    [
        ("observability.list-services", "OBSERVABILITY_LIST_SERVICES"),
        ("code.review", "CODE_REVIEW"),
        ("some-id.with.multiple-hyphens", "SOME_ID_WITH_MULTIPLE_HYPHENS"),
    ],
)
def test_cap_env_names(cap_id, slug):
    assert cap_endpoint_env(cap_id) == f"CARDINAL_CAP_{slug}_ENDPOINT"
    assert cap_token_env(cap_id) == f"CARDINAL_CAP_{slug}_TOKEN"


def test_cap_env_slug_rejects_unusable_ids():
    with pytest.raises(ValueError):
        cap_env_slug("")
    with pytest.raises(ValueError):
        cap_env_slug("...")


def test_cap_env_slug_matches_reconcilers_implementation():
    """The two implementations must not drift — they name the same variables.

    ``reconciler`` imports ``projections``, so the de-dupe direction is for
    ``reconciler._cap_env_slug`` to become an import of ``cap_env_slug``;
    until that lands this pins them together.
    """
    import reconciler

    for cap_id in (
        "observability.list-services",
        "code.review",
        "some-id.with.multiple-hyphens",
        "a.b_c-d",
    ):
        assert reconciler._cap_env_slug(cap_id) == cap_env_slug(cap_id)


def test_project_deployment_all_none_returns_empty_yaml():
    text = project_deployment(None, None, None)
    assert yaml.safe_load(text) in ({}, None)


def test_project_deployment_preserves_unrelated_dir_fields():
    dir_dep = {"metadata": {"owner": "sre"}, "sinks": []}
    text = project_deployment(None, None, dir_dep)
    out = yaml.safe_load(text)
    assert out["metadata"] == {"owner": "sre"}


def test_project_deployment_rejects_non_list_sinks():
    with pytest.raises(TypeError):
        project_deployment({"not": "a list"}, None, {})  # type: ignore[arg-type]


def test_project_deployment_rejects_non_list_capabilities():
    with pytest.raises(TypeError):
        project_deployment(None, {"not": "a list"}, {})  # type: ignore[arg-type]


def test_project_deployment_rejects_non_dict_dir():
    with pytest.raises(TypeError):
        project_deployment(None, None, ["not", "a", "dict"])  # type: ignore[arg-type]
