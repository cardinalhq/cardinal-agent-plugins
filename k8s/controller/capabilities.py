"""Capability binding validation.

The Sentinel directory's ``sentinel.yaml`` declares which abstract capability
ids it needs under ``spec.capabilities.required[].id``. The Sentinel CR
provides the concrete provider bindings under ``spec.capabilities[]``. Every
required id must be bound; extras on the CR side are harmless (they may cover
capabilities used by a sibling Sentinel that shares the same namespace's
secret conventions).
"""
from __future__ import annotations


def capability_bindings_ok(
    sentinel_dir_yaml: dict,
    cr_capabilities: list | None,
) -> tuple[bool, str]:
    """Check every required capability id has a matching CR binding.

    Returns ``(True, "")`` when every required id is bound; otherwise
    ``(False, "missing binding for <id>")`` naming the first missing id in
    declaration order.
    """
    if not isinstance(sentinel_dir_yaml, dict):
        raise TypeError(
            f"sentinel_dir_yaml must be a dict, got {type(sentinel_dir_yaml).__name__}"
        )
    spec = sentinel_dir_yaml.get("spec") or {}
    caps = spec.get("capabilities") or {}
    required = caps.get("required") or []
    if not isinstance(required, list):
        raise TypeError("spec.capabilities.required must be a list")

    bound_ids: set[str] = set()
    for c in cr_capabilities or []:
        if isinstance(c, dict) and c.get("id"):
            bound_ids.add(c["id"])

    for entry in required:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("id")
        if not rid:
            continue
        if rid not in bound_ids:
            return False, f"missing binding for {rid}"
    return True, ""
