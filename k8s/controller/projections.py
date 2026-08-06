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

Capability binding contract (the CR -> executor seam)
-----------------------------------------------------
The CR carries ``spec.capabilities`` as a LIST of
``{id, provider, endpointSecretRef?, tokenSecretRef?}``. The executor reads
``capabilityBindings`` as a DICT keyed by capability id
(``spike/executor/deployment.py``). This module is the single place that
translates between the two, and it is also the single source of truth for the
``CARDINAL_CAP_<SLUG>_ENDPOINT`` / ``_TOKEN`` env var names that the pod spec
injects from the referenced Secrets (see ``reconciler._capability_env``).

The projected binding for a CR entry is::

    capabilityBindings:
      observability.list-services:
        provider: mcp
        endpoint_env: CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT   # iff endpointSecretRef
        token_env:    CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN      # iff tokenSecretRef

``endpoint_env``/``token_env`` are emitted under exactly the same predicate the
pod-spec env projection uses, so a binding never names a variable that was not
injected. Providers resolve the *value* at run time from the process env.
"""
from __future__ import annotations

import json
import re
from typing import Any

import yaml


CAP_ENV_PREFIX = "CARDINAL_CAP_"


def cap_env_slug(cap_id: str) -> str:
    """Turn ``observability.list-services`` into ``OBSERVABILITY_LIST_SERVICES``.

    Any non-alphanumeric run becomes a single ``_`` and the result is
    upper-cased. Leading / trailing underscores from odd inputs are stripped.

    NOTE: ``reconciler._cap_env_slug`` must stay byte-identical to this — it
    builds the pod env vars these names refer to. ``reconciler`` imports
    ``projections`` (not the other way round), so the de-dupe direction is
    "reconciler imports from here"; until that lands,
    ``tests/test_projections.py::test_cap_env_slug_matches_reconcilers_implementation``
    pins the two implementations together.
    """
    if not isinstance(cap_id, str) or not cap_id:
        raise ValueError("capability id must be a non-empty string")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", cap_id).strip("_").upper()
    if not slug:
        raise ValueError(f"capability id {cap_id!r} has no alphanumerics")
    return slug


def cap_endpoint_env(cap_id: str) -> str:
    """Env var name carrying the endpoint URL for ``cap_id``."""
    return f"{CAP_ENV_PREFIX}{cap_env_slug(cap_id)}_ENDPOINT"


def cap_token_env(cap_id: str) -> str:
    """Env var name carrying the auth token for ``cap_id``."""
    return f"{CAP_ENV_PREFIX}{cap_env_slug(cap_id)}_TOKEN"


def capability_bindings(cr_capabilities: list | None) -> dict[str, dict[str, Any]]:
    """Translate the CR's ``spec.capabilities`` list into executor bindings.

    Returns a dict keyed by capability id whose values are
    ``{provider, endpoint_env?, token_env?}``. ``endpoint_env`` is present iff
    the CR entry sets ``endpointSecretRef``; ``token_env`` iff it sets
    ``tokenSecretRef`` — the identical predicate ``reconciler._capability_env``
    applies when it injects those variables into the pod.

    Malformed input raises ``ValueError`` rather than being skipped. A
    capability the CR *declares* but does not usably *bind* is exactly the
    silent-drop class of bug this seam exists to prevent, so it fails at
    projection time, naming the capability.

    These raises ARE reachable through the API server: the CRD's
    ``required: [id, provider]`` only checks presence (no ``minLength``, so a
    blank provider is accepted), the array carries no uniqueness constraint
    (duplicate ids are accepted), and no schema can express the env-var slug
    collision. ``reconciler.reconcile_sentinel`` therefore calls this
    defensively and converts the raise into a terminal ``Blocked`` status —
    see the comment there. Do not soften the validation here to compensate.
    """
    if cr_capabilities is None:
        return {}
    if not isinstance(cr_capabilities, list):
        raise TypeError(
            f"cr_capabilities must be a list, got {type(cr_capabilities).__name__}"
        )

    bindings: dict[str, dict[str, Any]] = {}
    slug_owner: dict[str, str] = {}
    id_index: dict[str, int] = {}
    for index, cap in enumerate(cr_capabilities):
        if not isinstance(cap, dict):
            raise ValueError(
                f"spec.capabilities[{index}] must be a mapping, "
                f"got {type(cap).__name__}"
            )
        cap_id = cap.get("id")
        if not isinstance(cap_id, str) or not cap_id.strip():
            raise ValueError(
                f"spec.capabilities[{index}] has no usable 'id' "
                f"(got {cap_id!r}); every capability must be named"
            )
        provider = cap.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError(
                f"capability {cap_id!r} declares no 'provider' (got {provider!r}); "
                f"a declared-but-unbound capability cannot be executed"
            )
        if cap_id in bindings:
            raise ValueError(
                f"capability {cap_id!r} is declared more than once in "
                f"spec.capabilities (entries [{id_index[cap_id]}] and "
                f"[{index}]); bindings are keyed by id so one entry would "
                f"silently win. Merge them into a single entry carrying both "
                f"endpointSecretRef and tokenSecretRef"
            )
        id_index[cap_id] = index

        slug = cap_env_slug(cap_id)
        if slug in slug_owner:
            raise ValueError(
                f"capability ids {slug_owner[slug]!r} and {cap_id!r} both map to "
                f"env var slug {slug!r}; they would share one "
                f"{CAP_ENV_PREFIX}{slug}_ENDPOINT/_TOKEN pair"
            )
        slug_owner[slug] = cap_id

        binding: dict[str, Any] = {"provider": provider}
        if cap.get("endpointSecretRef"):
            binding["endpoint_env"] = cap_endpoint_env(cap_id)
        if cap.get("tokenSecretRef"):
            binding["token_env"] = cap_token_env(cap_id)
        bindings[cap_id] = binding

    return bindings


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

    * ``sinks``: the CR's list replaces the directory's wholesale — sinks are a
      deployment-time decision and a partial merge would produce a
      hard-to-reason-about union.
    * ``capabilities``: the CR's list is translated by
      :func:`capability_bindings` and merged **per capability id** into the
      directory's ``capabilityBindings`` map. The CR's binding replaces the
      directory's binding for the same id wholesale (no key-level union — a
      leftover ``credential_ref`` from a ``fixture`` binding must not survive a
      rebind to a live provider), while directory bindings for ids the CR does
      not mention are preserved. That is what keeps the fixture path working
      for capabilities the operator has not bound yet.

    The CR's ``capabilities`` list is deliberately NOT copied through as a
    top-level ``capabilities:`` key. The executor reads ``capabilityBindings``
    only; a second key carrying the same intent is what let the CR's provider
    choice be silently ignored.

    ``sentinel_dir_deployment`` may be ``None`` (treated as empty). Non-dict
    ``sentinel_dir_deployment`` and non-list ``cr_sinks`` / ``cr_capabilities``
    raise ``TypeError``; malformed capability entries raise ``ValueError``
    (see :func:`capability_bindings`).
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
        derived = capability_bindings(cr_capabilities)
        if derived:
            dir_bindings = merged.get("capabilityBindings") or {}
            if not isinstance(dir_bindings, dict):
                raise TypeError(
                    "sentinel dir deployment.yaml capabilityBindings must be a "
                    f"mapping, got {type(dir_bindings).__name__}"
                )
            merged["capabilityBindings"] = {**dir_bindings, **derived}
    return yaml.safe_dump(merged, sort_keys=True, default_flow_style=False)


__all__ = [
    "CAP_ENV_PREFIX",
    "cap_endpoint_env",
    "cap_env_slug",
    "cap_token_env",
    "capability_bindings",
    "project_deployment",
    "project_inputs",
]
