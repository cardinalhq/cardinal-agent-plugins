"""Projection helpers — merge CR-supplied fields OVER the Sentinel directory's own configs.

Two pure functions, both returning strings ready to drop into a projected Secret:

* ``project_inputs`` merges the CR's ``spec.inputs`` on top of the sentinel
  directory's ``inputs.json`` and returns a canonical JSON string.
* ``project_deployment`` merges the CR's ``spec.sinks`` + ``spec.capabilities``
  on top of the sentinel directory's ``deployment.yaml`` and returns a YAML
  string.

The merge semantics are intentionally shallow — the Sentinel spec models these
as flat top-level lists / maps, not deeply nested trees — and CR fields win on
conflict. The controller mounts these two strings at ``/config/inputs.json``
and ``/config/deployment.yaml`` in the executor pod.
"""
from __future__ import annotations

import json
from typing import Any

import yaml


def project_inputs(cr_inputs: dict | None, sentinel_dir_inputs: dict | None) -> str:
    """Return a canonical JSON string of the merged inputs.

    ``cr_inputs`` fields win over ``sentinel_dir_inputs`` fields on conflict.
    Either side may be ``None`` (treated as empty). Anything non-dict on
    either side raises ``TypeError`` — merging structurally different shapes
    is a programmer error, not a runtime coercion.
    """
    if cr_inputs is None:
        cr_inputs = {}
    if sentinel_dir_inputs is None:
        sentinel_dir_inputs = {}
    if not isinstance(cr_inputs, dict):
        raise TypeError(f"cr_inputs must be a dict, got {type(cr_inputs).__name__}")
    if not isinstance(sentinel_dir_inputs, dict):
        raise TypeError(
            f"sentinel_dir_inputs must be a dict, got {type(sentinel_dir_inputs).__name__}"
        )
    merged: dict[str, Any] = {**sentinel_dir_inputs, **cr_inputs}
    return json.dumps(merged, indent=2, sort_keys=True)


def project_deployment(
    cr_sinks: list | None,
    cr_capabilities: list | None,
    sentinel_dir_deployment: dict | None,
) -> str:
    """Return a YAML string of the merged deployment.

    CR-supplied ``sinks`` and ``capabilities`` replace the directory-supplied
    lists wholesale — sinks and capability bindings are inherently
    deployment-time decisions, and a partial merge would produce a
    hard-to-reason-about union. Other top-level fields from the directory's
    ``deployment.yaml`` (if any) are preserved as-is.

    ``sentinel_dir_deployment`` may be ``None`` (treated as empty). Non-dict
    ``sentinel_dir_deployment`` and non-list ``cr_sinks`` / ``cr_capabilities``
    raise ``TypeError``.
    """
    if sentinel_dir_deployment is None:
        sentinel_dir_deployment = {}
    if not isinstance(sentinel_dir_deployment, dict):
        raise TypeError(
            f"sentinel_dir_deployment must be a dict, "
            f"got {type(sentinel_dir_deployment).__name__}"
        )
    if cr_sinks is not None and not isinstance(cr_sinks, list):
        raise TypeError(f"cr_sinks must be a list, got {type(cr_sinks).__name__}")
    if cr_capabilities is not None and not isinstance(cr_capabilities, list):
        raise TypeError(
            f"cr_capabilities must be a list, got {type(cr_capabilities).__name__}"
        )

    merged: dict[str, Any] = dict(sentinel_dir_deployment)
    if cr_sinks is not None:
        merged["sinks"] = cr_sinks
    if cr_capabilities is not None:
        merged["capabilities"] = cr_capabilities
    return yaml.safe_dump(merged, sort_keys=True, default_flow_style=False)
