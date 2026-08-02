"""compute-error-count-reconciliation — cross-check reported errors vs log body.

Inputs:
  errorOverview:  lakerunner error_overview response
                  {services: [{name, total_errors, top_messages, ...}], ...}
  bodyErrorCount: logs-query response for
                  `sum(count_over_time({service_name=X} |~ error|panic|... [5m]))`

Output:
  {
    mismatchDetected: bool,
    totalErrorsReported: number,
    bodyMatchesFound: number,
    errorMessageBreakdownPresent: bool
  }

Mismatch fires when: totalErrorsReported > 0 AND bodyMatchesFound == 0.
That's the exact scenario the source investigation flagged: the platform's
error count is non-zero, but a body-substring scan finds nothing to
corroborate it.
"""
from __future__ import annotations

import json as _json
from typing import Any


def _sum_body_matches(logs_response: Any) -> float:
    """The `sum(count_over_time(...))` shape yields a single scalar series.

    Walk the response for the total. Zero if nothing found.
    """
    if not isinstance(logs_response, dict):
        return 0.0
    # Prefer a direct total field if present.
    for key in ("total", "value", "count", "series_total"):
        v = logs_response.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    ddsketches = logs_response.get("ddsketches")
    if isinstance(ddsketches, dict):
        total = 0.0
        for stats in ddsketches.values():
            if not isinstance(stats, dict):
                continue
            for k in ("sum", "count", "avg"):
                v = stats.get(k)
                if isinstance(v, (int, float)):
                    total += float(v)
                    break
        return total
    # data_points list?
    dp = logs_response.get("data_points") or logs_response.get("dataPoints") or []
    if isinstance(dp, list):
        total = 0.0
        for row in dp:
            if isinstance(row, dict):
                v = row.get("value")
                if isinstance(v, (int, float)):
                    total += float(v)
        return total
    return 0.0


def _sum_reported_errors(error_overview: Any) -> tuple[float, bool]:
    """Return (total_errors_reported, error_message_breakdown_present)."""
    if not isinstance(error_overview, dict):
        return 0.0, False
    services = error_overview.get("services") or []
    total = 0.0
    breakdown = False
    if isinstance(services, list):
        for svc in services:
            if not isinstance(svc, dict):
                continue
            te = svc.get("total_errors") or svc.get("totalErrors") or 0
            try:
                total += float(te)
            except (TypeError, ValueError):
                pass
            tm = svc.get("top_messages") or svc.get("topMessages") or []
            if isinstance(tm, list) and tm:
                breakdown = True
    return total, breakdown


def run(inp: dict[str, Any]) -> dict[str, Any]:
    total_reported, breakdown_present = _sum_reported_errors(inp.get("errorOverview"))
    body_matches = _sum_body_matches(inp.get("bodyErrorCount"))
    mismatch = bool(total_reported > 0 and body_matches == 0)
    return {
        "mismatchDetected": mismatch,
        "totalErrorsReported": total_reported,
        "bodyMatchesFound": body_matches,
        "errorMessageBreakdownPresent": breakdown_present,
    }
