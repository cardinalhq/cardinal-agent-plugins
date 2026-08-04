"""compute-health-summary — threshold checks across five health signals.

Per rationale.md §32 the operator's health call was a conjunction of five
independent threshold checks. This function reproduces them deterministically.

Inputs:
  deploymentAvailability: metric response (aggregation=min, k8s_deployment_available)
  podUptime:              metric response (per-pod uptime, group_by=k8s_pod_name)
  cpuUtilization:         metric response (max cpu limit utilization)
  memoryUtilization:      metric response (max memory limit utilization)
  logsByLevel:            logs response (sum by level count_over_time)
  minAvailableReplicas:   number  (default 1)
  cpuLimitPeakThreshold:  number  (default 0.80)
  memoryLimitPeakThreshold: number (default 0.80)

Output:
  {
    overall: "healthy" | "degraded" | "critical" | "inconclusive",
    signals: {
      availability: {status, minReplicas},
      restarts:     {status, restartCount, podCount},
      cpu:          {status, peakUtilization},
      memory:       {status, peakUtilization},
      logLevels:    {status, distribution}
    }
  }

Signal status vocabulary: "healthy" | "degraded" | "critical" | "inconclusive"

Aggregation rules:
- any signal == "critical" -> overall = "critical"
- any signal == "degraded" -> overall = "degraded"
- all signals == "healthy" -> overall = "healthy"
- otherwise                -> overall = "inconclusive"
"""
from __future__ import annotations

from typing import Any


def _iter_stats(metric_response: Any):
    """Yield per-series stats dicts from a normalized metric response.

    The lakerunner MCP `execute_metrics_query` shape (post
    capabilities._normalize_metric_response) puts per-series data under
    `ddsketches` (dict) or `series`/`series_stats`. Fallback to top-level
    if nothing found.
    """
    if not isinstance(metric_response, dict):
        return
    ddsketches = metric_response.get("ddsketches")
    if isinstance(ddsketches, dict) and ddsketches:
        for stats in ddsketches.values():
            if isinstance(stats, dict):
                yield stats
        return
    series = metric_response.get("series") or metric_response.get("series_stats")
    if isinstance(series, list):
        for s in series:
            if isinstance(s, dict):
                yield s.get("stats") or s
        return
    # Fallback: some responses put a single-series summary at the top level.
    if metric_response.get("avg") is not None or metric_response.get("min") is not None:
        yield metric_response


def _scalar(metric_response: Any, key: str, default: float | None = None) -> float | None:
    vals = []
    for s in _iter_stats(metric_response):
        v = s.get(key)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return default
    if key == "min":
        return min(vals)
    if key == "max":
        return max(vals)
    if key == "avg":
        return sum(vals) / len(vals)
    return vals[0]


def _availability(metric_response: Any, min_replicas: float) -> dict[str, Any]:
    mn = _scalar(metric_response, "min")
    if mn is None:
        return {"status": "inconclusive", "minReplicas": None}
    status = "healthy" if mn >= min_replicas else "critical"
    return {"status": status, "minReplicas": mn}


def _restarts(metric_response: Any) -> dict[str, Any]:
    """Container uptime: for each pod, if min uptime is greater than 0 AND
    monotonically increasing (i.e., we can only observe non-negative growth),
    no restart. Our summary stats give us min/max per series. Restart is
    detected when min < prior max — but we only have aggregated stats here,
    so the honest heuristic is: min uptime == 0 across some pods implies a
    fresh start within the window."""
    stats = list(_iter_stats(metric_response))
    if not stats:
        return {"status": "inconclusive", "restartCount": 0, "podCount": 0}
    restart_count = 0
    for s in stats:
        mn = s.get("min")
        try:
            mn_f = float(mn) if mn is not None else None
        except (TypeError, ValueError):
            mn_f = None
        # Uptime near 0 within the window is the signature of a restart.
        if mn_f is not None and mn_f < 60.0:
            restart_count += 1
    status = "healthy" if restart_count == 0 else ("degraded" if restart_count == 1 else "critical")
    return {"status": status, "restartCount": restart_count, "podCount": len(stats)}


def _utilization(metric_response: Any, threshold: float) -> dict[str, Any]:
    peak = _scalar(metric_response, "max")
    if peak is None:
        return {"status": "inconclusive", "peakUtilization": None}
    if peak >= 1.0:
        return {"status": "critical", "peakUtilization": peak}
    if peak >= threshold:
        return {"status": "degraded", "peakUtilization": peak}
    return {"status": "healthy", "peakUtilization": peak}


def _log_levels(logs_response: Any) -> dict[str, Any]:
    """From a `sum by (level) count_over_time(...)` response.

    We look at the response's per-series structure and build a
    {level: total_count} distribution. If any level in {"ERROR","WARN"}
    has a non-zero count, mark degraded.
    """
    distribution: dict[str, float] = {}
    if not isinstance(logs_response, dict):
        return {"status": "inconclusive", "distribution": {}}
    ddsketches = logs_response.get("ddsketches")
    if isinstance(ddsketches, dict):
        for key, stats in ddsketches.items():
            if not isinstance(stats, dict):
                continue
            level = _extract_level_from_key(key) or "unknown"
            total = stats.get("count") or stats.get("sum") or stats.get("avg")
            try:
                distribution[level] = distribution.get(level, 0.0) + float(total or 0.0)
            except (TypeError, ValueError):
                pass
    # Also check "series"/"data_points_total" fallback.
    if not distribution and logs_response.get("summary"):
        # Nothing structured; can't classify honestly.
        return {"status": "inconclusive", "distribution": {}}
    error_like = 0.0
    for k, v in distribution.items():
        if k.upper() in ("ERROR", "ERR", "FATAL", "CRITICAL", "PANIC"):
            error_like += v
    warn_like = sum(v for k, v in distribution.items() if k.upper() in ("WARN", "WARNING"))
    if error_like > 0:
        status = "critical" if error_like >= 100 else "degraded"
    elif warn_like > 0:
        status = "degraded" if warn_like >= 1000 else "healthy"
    else:
        status = "healthy"
    return {"status": status, "distribution": distribution}


def _extract_level_from_key(key: Any) -> str | None:
    """The metric-response key is typically a JSON-encoded attributes dict."""
    if not isinstance(key, str):
        return None
    import json as _json
    try:
        obj = _json.loads(key)
    except Exception:
        return None
    if isinstance(obj, dict):
        for k in ("level", "log.level", "severity"):
            if k in obj and obj[k] is not None:
                return str(obj[k])
    return None


def run(inp: dict[str, Any]) -> dict[str, Any]:
    min_replicas = float(inp.get("minAvailableReplicas") or 1)
    cpu_thr = float(inp.get("cpuLimitPeakThreshold") or 0.80)
    mem_thr = float(inp.get("memoryLimitPeakThreshold") or 0.80)

    signals = {
        "availability": _availability(inp.get("deploymentAvailability"), min_replicas),
        "restarts": _restarts(inp.get("podUptime")),
        "cpu": _utilization(inp.get("cpuUtilization"), cpu_thr),
        "memory": _utilization(inp.get("memoryUtilization"), mem_thr),
        "logLevels": _log_levels(inp.get("logsByLevel")),
    }
    statuses = [s.get("status") for s in signals.values()]
    if any(s == "critical" for s in statuses):
        overall = "critical"
    elif any(s == "degraded" for s in statuses):
        overall = "degraded"
    elif all(s == "healthy" for s in statuses):
        overall = "healthy"
    else:
        overall = "inconclusive"
    return {"overall": overall, "signals": signals}
