"""Detect one-label-value dominance in a counter's label breakdown.

Input:
  dimensionedSketches: Map[str, {avg, count, ...}]  where keys encode
    the label combination for the series (e.g. "action=claim,level=0,trigger=age")
  dominanceLabel: str   (e.g. "trigger")
  dominanceRatioThreshold: float  (e.g. 0.9)

Output:
  dominantLabelValue: str
  dominanceRatio: float
  dominancePresent: bool
  sampleSufficient: bool
"""
from __future__ import annotations

import re
from typing import Any


_LABEL_KV = re.compile(r"([A-Za-z0-9_./-]+)\s*[:=]\s*\"?([^,\"}]+)\"?")


def _extract_label(key: str, label: str) -> str | None:
    """Extract a specific label's value from a series key.

    Series keys observed in Cardinal ddsketches output take shapes like:
      "action=claim,level=0,trigger=age"
      "{action=\"claim\", level=\"0\", trigger=\"age\"}"
      "action:claim|level:0|trigger:age"
    We handle any of these with a permissive regex.
    """
    for m in _LABEL_KV.finditer(key):
        if m.group(1) == label:
            return m.group(2).strip()
    return None


def _series_weight(series: dict) -> float:
    """How much this series contributes to the total.

    Prefer 'count' (samples). Fall back to 'sum' or 'avg' as a proxy.
    """
    for k in ("count", "sum", "avg"):
        v = series.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def run(inp: dict[str, Any]) -> dict[str, Any]:
    sketches = inp.get("dimensionedSketches") or {}
    label = str(inp.get("dominanceLabel") or "")
    threshold = float(inp.get("dominanceRatioThreshold", 0.9))

    per_value_weight: dict[str, float] = {}
    total_weight = 0.0
    labelled_series = 0

    for key, series in sketches.items():
        if not isinstance(series, dict):
            continue
        val = _extract_label(str(key), label)
        if val is None:
            continue
        w = _series_weight(series)
        if w <= 0:
            continue
        labelled_series += 1
        per_value_weight[val] = per_value_weight.get(val, 0.0) + w
        total_weight += w

    sample_sufficient = labelled_series >= 1 and total_weight > 0

    dominant_value = ""
    dominance_ratio = 0.0
    if sample_sufficient:
        dominant_value, top_w = max(per_value_weight.items(), key=lambda kv: kv[1])
        dominance_ratio = top_w / total_weight if total_weight > 0 else 0.0

    return {
        "dominantLabelValue": dominant_value,
        "dominanceRatio": float(dominance_ratio),
        "dominancePresent": bool(sample_sufficient and dominance_ratio >= threshold),
        "sampleSufficient": bool(sample_sufficient),
    }
