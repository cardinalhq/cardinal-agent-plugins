# Parses a status-page summary into {indicator, description, level}.
#
# Deterministic, network-free, and therefore hermetically trialable. The
# outbound fetch that produced `payload` is deliberately NOT in this DAG —
# see rationale.md "Why the fetch is an input".
import re

_INDICATOR_TO_LEVEL = {
    "none": "operational",
    "maintenance": "operational",
    "minor": "degraded",
    "major": "incident",
    "critical": "incident",
}


def run(inp):
    payload = inp["payload"]
    indicator = _match_first(payload, r'\*\*Indicator:\*\*\s*"?([A-Za-z_-]+)"?')
    description = _match_first(payload, r'\*\*Overall Status:\*\*\s*([^\n]+)')
    level = _INDICATOR_TO_LEVEL.get(indicator or "", "unknown")
    return {
        "indicator": indicator or "unknown",
        "description": (description or "unknown").strip(),
        "level": level,
    }


def _match_first(text, pattern):
    m = re.search(pattern, text)
    return m.group(1) if m else None
