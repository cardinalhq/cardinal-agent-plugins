"""Unit tests for capability binding validation."""
from __future__ import annotations

import pytest

from capabilities import capability_bindings_ok


def _dir_yaml_with_required(*ids: str) -> dict:
    return {
        "apiVersion": "sentinels.cardinalhq.io/v1alpha1",
        "kind": "Sentinel",
        "spec": {
            "capabilities": {
                "required": [{"id": i} for i in ids],
            },
        },
    }


def test_all_bound_returns_ok():
    dir_yaml = _dir_yaml_with_required(
        "observability.list-services", "observability.query-metrics"
    )
    cr_caps = [
        {"id": "observability.list-services", "provider": "mcp"},
        {"id": "observability.query-metrics", "provider": "mcp"},
    ]
    ok, msg = capability_bindings_ok(dir_yaml, cr_caps)
    assert ok is True
    assert msg == ""


def test_missing_one_binding_names_it():
    dir_yaml = _dir_yaml_with_required(
        "observability.list-services", "observability.query-metrics"
    )
    cr_caps = [{"id": "observability.list-services", "provider": "mcp"}]
    ok, msg = capability_bindings_ok(dir_yaml, cr_caps)
    assert ok is False
    assert "observability.query-metrics" in msg
    assert msg == "missing binding for observability.query-metrics"


def test_extra_binding_not_declared_is_harmless():
    dir_yaml = _dir_yaml_with_required("observability.list-services")
    cr_caps = [
        {"id": "observability.list-services", "provider": "mcp"},
        {"id": "observability.query-metrics", "provider": "mcp"},   # extra
        {"id": "code.review", "provider": "mcp"},                    # extra
    ]
    ok, msg = capability_bindings_ok(dir_yaml, cr_caps)
    assert ok is True
    assert msg == ""


def test_empty_required_always_ok():
    dir_yaml = {"spec": {}}
    assert capability_bindings_ok(dir_yaml, []) == (True, "")
    assert capability_bindings_ok(dir_yaml, None) == (True, "")


def test_missing_all_bindings_reports_first_in_declaration_order():
    dir_yaml = _dir_yaml_with_required("first.cap", "second.cap")
    ok, msg = capability_bindings_ok(dir_yaml, [])
    assert ok is False
    assert msg == "missing binding for first.cap"


def test_none_cr_capabilities_treated_as_empty():
    dir_yaml = _dir_yaml_with_required("must.have")
    ok, msg = capability_bindings_ok(dir_yaml, None)
    assert ok is False
    assert "must.have" in msg


def test_non_dict_dir_yaml_raises():
    with pytest.raises(TypeError):
        capability_bindings_ok(["not", "a", "dict"], [])  # type: ignore[arg-type]


def test_malformed_required_entry_is_skipped():
    # A required entry with no id shouldn't blow up — treat it as no-op.
    dir_yaml = {
        "spec": {
            "capabilities": {
                "required": [{"id": "real.cap"}, {"no_id": "here"}, "not-a-dict"],
            },
        },
    }
    ok, msg = capability_bindings_ok(dir_yaml, [{"id": "real.cap"}])
    assert ok is True
    assert msg == ""
