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

from projections import project_deployment, project_inputs


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


def test_project_deployment_cr_capabilities_replace_dir_capabilities():
    cr_caps = [{"id": "observability.list-services", "provider": "mcp"}]
    dir_dep = {"capabilities": [{"id": "old", "provider": "old"}]}
    out = yaml.safe_load(project_deployment(None, cr_caps, dir_dep))
    assert out["capabilities"] == cr_caps


def test_project_deployment_missing_dir_is_empty():
    cr_sinks = [{"id": "stdout"}]
    cr_caps = [{"id": "cap.x", "provider": "mcp"}]
    out = yaml.safe_load(project_deployment(cr_sinks, cr_caps, None))
    assert out == {"sinks": cr_sinks, "capabilities": cr_caps}


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
