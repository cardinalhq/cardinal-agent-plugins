"""Detect metric degeneracy per §11 (deterministic function node).

Input:
  dimensionedSketches: Map[str, {avg, min, max, p50, p90, p95, p99, count, ...}]
  minSeriesForIntegrity: int
  withinSeriesFlatnessTolerance: float

Output:
  crossDimensionCollapse: bool  (all series' avg identical within tolerance)
  withinSeriesFlat: bool        (every series has min~=max~=p50~=p95~=p99 within tol)
  distinctValueCount: int       (approx count of distinct avg values across series)
  seriesCount: int
  collapseValue: float | None
  sufficientData: bool
"""
from __future__ import annotations

import math
from typing import Any


def _approx_eq(a: float, b: float, tol: float) -> bool:
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom <= tol


def _get_stat(series: dict, key: str) -> float | None:
    v = series.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def run(inp: dict[str, Any]) -> dict[str, Any]:
    sketches = inp.get("dimensionedSketches") or {}
    min_series = int(inp.get("minSeriesForIntegrity", 3))
    tol = float(inp.get("withinSeriesFlatnessTolerance", 0.001))

    series_count = len(sketches)
    sufficient = series_count >= min_series

    # Extract per-series avg
    avgs: list[float] = []
    per_series_flat: list[bool] = []
    for _key, series in sketches.items():
        if not isinstance(series, dict):
            continue
        avg = _get_stat(series, "avg")
        if avg is not None:
            avgs.append(avg)
        # within-series flatness
        mn = _get_stat(series, "min")
        mx = _get_stat(series, "max")
        p50 = _get_stat(series, "p50")
        p95 = _get_stat(series, "p95")
        p99 = _get_stat(series, "p99")
        stats = [s for s in (mn, mx, p50, p95, p99) if s is not None]
        if len(stats) >= 2:
            ref = stats[0]
            per_series_flat.append(all(_approx_eq(s, ref, tol) for s in stats))
        else:
            per_series_flat.append(False)

    # Cross-dim collapse: are all avgs within tol of each other?
    cross_collapse = False
    collapse_value: float | None = None
    if avgs:
        ref = avgs[0]
        if all(_approx_eq(a, ref, tol) for a in avgs):
            cross_collapse = True
            collapse_value = ref

    # Distinct value count: approximate via bucketing at tolerance
    distinct: list[float] = []
    for a in avgs:
        if not any(_approx_eq(a, d, tol) for d in distinct):
            distinct.append(a)

    within_flat = bool(per_series_flat) and all(per_series_flat)

    return {
        "crossDimensionCollapse": bool(cross_collapse),
        "withinSeriesFlat": bool(within_flat),
        "distinctValueCount": len(distinct),
        "seriesCount": int(series_count),
        "collapseValue": collapse_value if collapse_value is not None else 0.0,
        "sufficientData": bool(sufficient),
    }
