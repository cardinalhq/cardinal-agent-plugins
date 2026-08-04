"""pick-resolved-service — pick the first service from a list-services response.

Input:
  resolved: {services: [{name: str, ...}, ...], ...}

Output:
  {name: str, candidateCount: int}

Behavior:
- If `services` is empty or absent, returns name="" and candidateCount=0.
  Downstream nodes will then fail schema validation on their arguments,
  which is the honest failure mode: no service to assess.
- If `services` has 1+ entries, picks index 0. This mirrors the source
  investigation's implicit assumption of a unique substring match. When
  candidateCount > 1 the pick is a compilation-time judgment call
  documented in the v1 rationale; leaving the function to fail loud on
  ambiguous matches is a future enhancement.
"""
from __future__ import annotations

from typing import Any


def run(inp: dict[str, Any]) -> dict[str, Any]:
    resolved = inp.get("resolved") or {}
    services = resolved.get("services") or []
    if not isinstance(services, list) or not services:
        return {"name": "", "candidateCount": 0}
    first = services[0]
    if isinstance(first, dict):
        name = str(first.get("name") or "")
    else:
        name = str(first)
    return {"name": name, "candidateCount": int(len(services))}
